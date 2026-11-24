"""LinkedIn 不登录模式：调公开的 jobs-guest 接口（JobSpy 的做法），不碰你的账号。
登录模式（linkedin.mjs）失效时由 run.py 自动切换过来。

限制：每页 10 条；按 IP 限流（429 就停）；没有可用的 remote 筛选，地点也不写 "(Remote)"，
所以 require_remote 的搜索只保留地点是 "Australia"（全国、不是具体城市）的职位，比较保守，可能漏掉少数。

单独运行（输出 JSONL，不写库）:  python3 linkedin_guest.py ["搜索名"]
"""
import html, json, random, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

UA = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
      "accept-language": "en-AU,en;q=0.9"}
UNIT = {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200}


class RateLimited(Exception):
    pass


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 999):
            raise RateLimited(f"HTTP {e.code}")
        raise


def text(s):
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div|ul|ol)>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return "\n".join(l for l in (" ".join(x.split()) for x in s.split("\n")) if l).strip()


def first(pattern, s):
    m = re.search(pattern, s, re.S)
    return text(m.group(1)) if m else None


def age_minutes(s):
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month)", s or "")
    return int(m[1]) * UNIT[m[2]] if m else None


def pause(a=2.5, b=6.0):
    time.sleep(random.uniform(a, b) + (random.uniform(8, 20) if random.random() < 0.1 else 0))


def details(job_id):
    d = get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")
    crit = {text(k): text(v) for k, v in re.findall(
        r'description__job-criteria-subheader">(.*?)</h3>\s*<span[^>]*>(.*?)</span>', d, re.S)}
    return {"description": first(r'show-more-less-html__markup[^>]*>(.*?)</div>', d),
            "posted_text": first(r'posted-time-ago__text[^>]*>(.*?)<', d),
            "applicants": first(r'num-applicants__caption[^>]*>(.*?)<', d) or first(r'num-applicants__figure[^>]*>(.*?)<', d),
            "criteria": crit}


def search(s, known=frozenset(), tpr=86400, max_pages=5, max_new=40):
    """s: searches.toml 里的一条 [[linkedin]]。逐条 yield posting dict（已知的只带 id + 搜索名）。"""
    opened = 0
    for page in range(max_pages):
        q = {"keywords": s["keywords"], "geoId": s["geoId"], "f_TPR": f"r{tpr}", "sortBy": "DD", "start": page * 10}
        h = get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode(q))
        cards = h.split("<li>")[1:]
        if not cards:
            return
        for c in cards:
            m = re.search(r'urn:li:jobPosting:(\d+)', c)
            if not m:
                continue
            jid = m[1]
            loc = first(r'job-search-card__location">(.*?)<', c)
            if s.get("require_remote") and (loc or "").strip() != "Australia":
                continue
            base = {"source": "linkedin", "source_id": jid, "search": s["name"], "url": f"https://www.linkedin.com/jobs/view/{jid}/",
                    "title": first(r'base-search-card__title">(.*?)</h3>', c),
                    "company": first(r'base-search-card__subtitle">(.*?)</h4>', c), "location": loc}
            if jid in known:
                yield base
                continue
            if opened >= max_new:
                continue
            pause()
            d = details(jid)
            if not d["description"]:
                continue
            opened += 1
            age = age_minutes(d["posted_text"] or first(r'<time[^>]*>(.*?)</time>', c))
            yield base | {
                "description": d["description"],
                "posted_at": (datetime.now(timezone.utc) - timedelta(minutes=age)).strftime("%Y-%m-%dT%H:%M:%SZ") if age is not None else None,
                "work_type": d["criteria"].get("Employment type"),
                "extra": {"mode": "guest", "posted_text": d["posted_text"], "applicants": d["applicants"],
                          "seniority": d["criteria"].get("Seniority level"), "industries": d["criteria"].get("Industries")},
            }
        pause()


if __name__ == "__main__":
    import os, tomllib
    conf = tomllib.load(open(__import__("rules").config_path("searches.toml"), "rb"))
    names = {s["name"]: s for s in conf["linkedin"]}
    s = names[sys.argv[1]] if len(sys.argv) > 1 else next(iter(names.values()))
    for p in search(s, max_pages=1, max_new=2):
        print(json.dumps(p, ensure_ascii=False))
