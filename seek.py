"""SEEK：直接调前端用的 JSON 接口（不开浏览器、不需要登录），按发布时间倒序翻页，只给新帖子拉 JD。

单独运行（输出 JSONL，不写库）:  python3 seek.py ["搜索名"] [天数=3]
"""
import html, json, random, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

UA = {"user-agent": "Mozilla/5.0", "content-type": "application/json"}


def http(url, body=None):
    req = urllib.request.Request(url, data=body and json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div|ul|ol)>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = [" ".join(l.split()) for l in s.split("\n")]
    return "\n".join(l for l in lines if l).strip()


def details(job_id):
    q = 'query { d: jobDetails(id: "%s") { job { content(platform: WEB) } } }' % job_id
    return html_to_text(http("https://au.seek.com/graphql", {"query": q})["data"]["d"]["job"]["content"])


def search(name, params, days=3, known=frozenset(), page_size=100, max_pages=10):
    """逐条 yield posting dict。listingDate 早于 days 天前就停止翻页。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    for page in range(1, max_pages + 1):
        q = {"siteKey": "AU-Main", "sortmode": "ListedDate", "page": page, "pageSize": page_size, **params}
        data = http("https://au.seek.com/api/jobsearch/v5/search?" + urllib.parse.urlencode(q))["data"]
        if not data:
            return
        for j in data:
            listed = j.get("listingDate")
            # 置顶的推广帖可能不按时间排，所以过期的只跳过，不立刻停；整页都过期才停
            if listed and datetime.fromisoformat(listed.replace("Z", "+00:00")) < since:
                continue
            p = {"source": "seek", "source_id": j["id"], "search": name,
                 "title": j["title"], "company": j.get("companyName") or (j.get("advertiser") or {}).get("description"),
                 "location": ", ".join(l["label"] for l in j.get("locations") or []) or None,
                 "url": f"https://au.seek.com/job/{j['id']}", "posted_at": listed,
                 "salary": j.get("salaryLabel") or None,
                 "work_type": ", ".join(j.get("workTypes") or []) or None,
                 "work_arrangement": ((j.get("workArrangements") or {}).get("displayText")) or None}
            if j["id"] not in known:
                try:
                    p["description"] = details(j["id"])
                except Exception as e:  # 单条 JD 拉不到不影响整体
                    print(f"[seek] {j['id']} JD 获取失败: {e}", file=sys.stderr)
                    p["description"] = j.get("teaser")
                p["extra"] = {"teaser": j.get("teaser"), "classification": [c["subclassification"]["description"] for c in j.get("classifications") or []]}
                time.sleep(random.uniform(1, 3))  # 别连续猛拉 JD
            yield p
        if all(j.get("listingDate") and datetime.fromisoformat(j["listingDate"].replace("Z", "+00:00")) < since for j in data):
            return


if __name__ == "__main__":
    import tomllib, os
    conf = tomllib.load(open(__import__("rules").config_path("searches.toml"), "rb"))
    names = {s["name"]: s for s in conf["seek"]}
    name = sys.argv[1] if len(sys.argv) > 1 else next(iter(names))
    days = float(sys.argv[2]) if len(sys.argv) > 2 else 3
    for p in search(name, names[name]["params"], days):
        print(json.dumps(p, ensure_ascii=False))
