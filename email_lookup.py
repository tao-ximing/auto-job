"""邮件补抓：求职邮件匹配不上库里的职位时，按邮件里的公司名 + 职位名去 SEEK / LinkedIn 搜这个职位，找到就入库并关联邮件。

- 只认公司名对得上（规范化后相等或完整词前缀）且职位名足够像的结果，宁可搜不到也不要关联错。
- SEEK 走公开 JSON 接口；LinkedIn 走不登录的公开接口（linkedin_guest），不点开你账号下的职位，不会留下 Viewed。
- 发件人是 SEEK 就先搜 SEEK，是 LinkedIn 就先搜 LinkedIn，找到就不再搜另一个；其他发件人两边都搜。
- 两边都搜不到、但邮件里有公司 + 职位名：用邮件本身建一个职位（source=email，链接指向 Gmail 里这封邮件），照样记录状态。
- 每封邮件只补抓一次（emails.lookup_at）；LinkedIn 限流时不记 lookup_at，下一轮再试。

单独运行（只搜不写库）:  python3 email_lookup.py "公司" "职位名" [seek|linkedin]
"""
import re, sys, urllib.parse
import linkedin_guest as lg
import rules, seek
from db import now
from gmail_sync import _co_match, apply_email, match_job

SEARCH_NAME = "邮件补抓"
TITLE_MIN = 0.5      # 职位名词集合 Jaccard 下限
AU_GEO = "101452733"


def _title_sim(a, b):
    ta, tb = set(rules.norm_title(a).split()), set(rules.norm_title(b).split())
    return rules.jaccard(ta, tb)


def _pick(cards, company, title):
    """cards: [(company, title, payload)]。返回最像的那个 payload（公司必须对上；有职位名时职位名也要够像）。"""
    cn = rules.norm_company(company)
    best, best_s = None, -1
    for co, t, payload in cards:
        if not _co_match(cn, rules.norm_company(co)):
            continue
        s = _title_sim(title, t) if title else 1.0
        if s >= (TITLE_MIN if title else 0) and s > best_s:
            best, best_s = payload, s
    return best


def find_seek(company, title):
    q = {"siteKey": "AU-Main", "where": "All Australia", "keywords": f"{title or ''} {company}".strip(), "page": 1, "pageSize": 50}
    data = seek.http("https://au.seek.com/api/jobsearch/v5/search?" + urllib.parse.urlencode(q))["data"] or []
    cards = []
    for j in data:
        for co in {j.get("companyName"), (j.get("advertiser") or {}).get("description")} - {None}:
            cards.append((co, j["title"], j))
    j = _pick(cards, company, title)
    if not j:
        return None
    return {"source": "seek", "source_id": j["id"], "search": SEARCH_NAME, "title": j["title"],
            "company": j.get("companyName") or (j.get("advertiser") or {}).get("description"),
            "location": ", ".join(l["label"] for l in j.get("locations") or []) or None,
            "url": f"https://au.seek.com/job/{j['id']}", "posted_at": j.get("listingDate"),
            "salary": j.get("salaryLabel") or None, "work_type": ", ".join(j.get("workTypes") or []) or None,
            "work_arrangement": ((j.get("workArrangements") or {}).get("displayText")) or None,
            "description": seek.details(j["id"]),
            "extra": {"teaser": j.get("teaser"), "via": "email_lookup"}}


def find_linkedin(company, title):
    q = {"keywords": f"{title or ''} {company}".strip(), "geoId": AU_GEO, "f_TPR": "r2592000", "start": 0}
    h = lg.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode(q))
    cards = []
    for c in h.split("<li>")[1:]:
        m = re.search(r'urn:li:jobPosting:(\d+)', c)
        if m:
            cards.append((lg.first(r'base-search-card__subtitle">(.*?)</h4>', c), lg.first(r'base-search-card__title">(.*?)</h3>', c),
                          {"id": m[1], "location": lg.first(r'job-search-card__location">(.*?)<', c)}))
    hit = _pick(cards, company, title)
    if not hit:
        return None
    co, t = next((co, t) for co, t, p in cards if p is hit)
    lg.pause(1.5, 4)
    d = lg.details(hit["id"])
    return {"source": "linkedin", "source_id": hit["id"], "search": SEARCH_NAME, "url": f"https://www.linkedin.com/jobs/view/{hit['id']}/",
            "title": t, "company": co, "location": hit["location"], "description": d["description"],
            "work_type": d["criteria"].get("Employment type"),
            "extra": {"mode": "guest", "via": "email_lookup", "posted_text": d["posted_text"], "applicants": d["applicants"],
                      "seniority": d["criteria"].get("Seniority level"), "industries": d["criteria"].get("Industries")}}


def find(company, title, hint=None):
    """按 hint 决定先搜哪边，找到一个就返回 posting dict；都没有返回 None。LinkedIn 限流会抛 lg.RateLimited。"""
    order = ["linkedin", "seek"] if hint == "linkedin" else ["seek", "linkedin"]
    for src in order:
        p = (find_seek if src == "seek" else find_linkedin)(company, title)
        if p:   # 发件平台没搜到（比如帖子已下架），也还是去另一个平台看看
            return p
    return None


def _hint(addr):
    addr = (addr or "").lower()
    return "seek" if "seek.com" in addr else "linkedin" if "linkedin.com" in addr else None


def run(conn, ing, verbose=True):
    """处理所有还没补抓过的未归类邮件。返回 dict(tried, added, matched)。"""
    stats = {"tried": 0, "added": 0, "matched": 0}
    rows = conn.execute("""SELECT id, thread_id, from_addr, subject, category, company, title, body FROM emails
                           WHERE state='unmatched' AND lookup_at IS NULL AND coalesce(company,'') != ''""").fetchall()
    cache = {}   # 同一轮里同一个公司 + 职位只搜一次（中介 / 招聘方常常连发好几封）
    for e in rows:
        stats["tried"] += 1
        key = (rules.norm_company(e["company"]), rules.norm_title(e["title"] or ""))
        try:
            if key not in cache:
                cache[key] = find(e["company"], e["title"], _hint(e["from_addr"]))
            p = cache[key]
        except lg.RateLimited as ex:
            print(f"[email-lookup] LinkedIn 限流（{ex}），剩下的下一轮再试", file=sys.stderr)
            break
        except Exception as ex:   # 单封出错不影响其他邮件，也不再重试
            p, err = None, f"出错: {ex}"
        else:
            err = None
        result = None
        if not p and not err and e["title"]:
            # 两边都搜不到（职位已下架、没公开发布、或者是招聘方直接联系的）：用邮件本身建一个职位，链接指向这封邮件
            p = {"source": "email", "source_id": e["id"], "search": SEARCH_NAME, "title": e["title"], "company": e["company"],
                 "url": f"https://mail.google.com/mail/u/0/#all/{e['thread_id']}", "description": None,
                 "extra": {"via": "email_lookup", "note": "SEEK / LinkedIn 都没搜到，按邮件建的职位"}}
        if p and (p.get("description") or p["source"] == "email"):
            r = ing.add(p)
            conn.commit()
            stats["added"] += r == "new"
            job_id, reason = match_job(conn, e["company"], e["title"], e["category"] or "other")
            if job_id:
                apply_email(conn, e["id"], job_id, e["category"] or "other", e["subject"])
                src = "邮件本身（SEEK / LinkedIn 都没搜到）" if p["source"] == "email" else p["source"].upper()
                conn.execute("UPDATE emails SET match_reason=? WHERE id=?", (f"补抓自 {src}；{reason}", e["id"]))
                stats["matched"] += 1
                result = f"{p['source']}:{p['source_id']} → #{job_id}"
            else:
                result = f"{p['source']}:{p['source_id']} 已入库，但{reason}"
        else:
            result = err or "SEEK / LinkedIn 都没搜到"
            conn.execute("UPDATE emails SET match_reason=? WHERE id=?",
                         (f"{err or 'SEEK / LinkedIn 上都没搜到'} {e['company']} · {e['title'] or ''}".strip(), e["id"]))
        conn.execute("UPDATE emails SET lookup_at=?, lookup_result=? WHERE id=?", (now(), result, e["id"]))
        conn.commit()
        if verbose:
            print(f"[email-lookup] {e['company']} · {e['title']}: {result}", file=sys.stderr)
    return stats


if __name__ == "__main__":
    company, title = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""
    hint = sys.argv[3] if len(sys.argv) > 3 else None
    p = find(company, title, hint)
    print({k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in p.items()} if p else "没找到")
