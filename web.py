"""本地网页：python3 web.py [端口=8765]，然后打开 http://127.0.0.1:8765

只监听 127.0.0.1，不对外。只用 Python 标准库。
"""
import json, os, re, sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from db import STATUSES, connect, now
from ingest import merge_jobs, refilter_company
from score import RANK_SQL
import push, gmail_sync, rules

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWS = {
    "active": "j.filtered=0 AND j.status IN ('new','shortlisted')",
    # 已经投了的不管有没有被过滤规则命中都放进行中（比如邮件补抓回来的职位 JD 要求公民身份，但你确实投了）
    "pipeline": "j.status IN ('applied','assessment','interview','offer')",
    "closed": "j.filtered=0 AND j.status IN ('skipped','rejected','closed')",
    "filtered": "j.filtered=1 AND j.status NOT IN ('applied','assessment','interview','offer')",
    "all": "1",
}
GHOST_DAYS = 21
STATIC = {  # 路径 → (static/ 下的文件, content-type)
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/static/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("icon-192.png", "image/png"),
}


# 「我的」页第一次打开时的内容（之后以库里 kv.profile 为准，网页上可增删改）。
# 个人信息不写在代码里（代码会推到 GitHub）：从不进 git 的 profile.json 读，没有就是空模板。
def _default_profile():
    p = os.path.join(HERE, "profile.json")
    if os.path.exists(p):
        return json.load(open(p))
    return [{"group": "基本信息", "label": "姓名", "value": ""}, {"group": "基本信息", "label": "邮箱", "value": ""},
            {"group": "基本信息", "label": "电话", "value": ""}, {"group": "链接", "label": "LinkedIn", "value": ""}]


DEFAULT_PROFILE = _default_profile()


def days_since(ts):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days if ts else None


def _rank_search(c, rows, q):
    """SQL 的 LIKE 只是粗筛（子串）。这里改成整词匹配（"rag" 不再命中 storage / leverage），
    标题 / 公司命中的排前面，只在 JD 里出现的排后面。返回 (rows, {job_id: 'title' | 'jd'})。"""
    if q.isascii():
        rx = re.compile(r"(?<!\w)" + re.escape(q) + r"(?!\w)", re.I)
    else:  # 中文没有词边界，用子串
        rx = re.compile(re.escape(q), re.I)
    ids = [r["id"] for r in rows]
    jd = {}
    for r in c.execute(f"SELECT job_id, description FROM postings WHERE job_id IN ({','.join('?' * len(ids))})", ids):
        jd[r[0]] = jd.get(r[0], "") + "\n" + (r[1] or "")
    top, rest, match = [], [], {}
    for r in rows:
        if rx.search(f"{r['title']} | {r['company']}"):
            top.append(r); match[r["id"]] = "title"
        elif rx.search(jd.get(r["id"], "")):
            rest.append(r); match[r["id"]] = "jd"
    return top + rest, match


def list_jobs(c, view, q, source):
    where, args = [VIEWS.get(view, VIEWS["active"])], []
    if q:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR EXISTS (SELECT 1 FROM postings x WHERE x.job_id=j.id AND x.description LIKE ?))")
        args += [f"%{q}%"] * 3
    if source:
        where.append("EXISTS (SELECT 1 FROM postings x WHERE x.job_id=j.id AND x.source=?)")
        args.append(source)
    rows = c.execute(f"""
        SELECT j.*, {RANK_SQL} rank_score, (SELECT max(posted_at) FROM postings x WHERE x.job_id=j.id) posted_at,
               (SELECT min(first_seen) FROM postings x WHERE x.job_id=j.id) first_seen,
               (SELECT max(at) FROM events e WHERE e.job_id=j.id AND e.source!='auto') last_activity,
               (SELECT min(at) FROM events e WHERE e.job_id=j.id AND e.status='applied') applied_at
        FROM jobs j WHERE {' AND '.join(where)}
        -- 待处理：按排序分从高到低（还没打分的排最前，不埋掉），同分按时间；其他视图按时间
        ORDER BY CASE WHEN j.status IN ('new','shortlisted') THEN {RANK_SQL} END DESC NULLS FIRST,
                 CASE WHEN j.status IN ('new','shortlisted') THEN coalesce(posted_at, first_seen) ELSE coalesce(last_activity, first_seen) END DESC
        LIMIT 1000""", args).fetchall()
    match = {}
    if q and rows:
        rows, match = _rank_search(c, rows, q)
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    posts, tags, hits = {}, {}, {}
    for p in c.execute(f"SELECT job_id, id, source, url, is_agency, work_arrangement, salary, extra FROM postings WHERE job_id IN ({ph})", ids):
        extra = json.loads(p["extra"]) if p["extra"] else {}
        posts.setdefault(p["job_id"], []).append({"source": p["source"], "url": p["url"], "agency": bool(p["is_agency"]),
                                                  "work_arrangement": p["work_arrangement"], "salary": p["salary"],
                                                  "quick_apply": extra.get("quick_apply"), "apply_url": extra.get("apply_url")})
    for t in c.execute(f"SELECT DISTINCT p.job_id, f.rule, f.action, f.reason FROM filter_results f JOIN postings p ON p.id=f.posting_id WHERE p.job_id IN ({ph})", ids):
        tags.setdefault(t["job_id"], []).append({"rule": t["rule"], "action": t["action"], "reason": t["reason"]})
    for h in c.execute(f"SELECT DISTINCT p.job_id, h.search FROM search_hits h JOIN postings p ON p.id=h.posting_id WHERE p.job_id IN ({ph})", ids):
        hits.setdefault(h["job_id"], []).append(h["search"])
    out = []
    for r in rows:
        d = dict(r)
        d.update(postings=posts.get(r["id"], []), tags=tags.get(r["id"], []), searches=sorted(hits.get(r["id"], [])))
        if r["id"] in match:
            d["match"] = match[r["id"]]
        if r["status"] in ("applied", "assessment", "interview"):
            quiet = days_since(r["last_activity"] or r["applied_at"])
            d["quiet_days"] = quiet if quiet is not None and quiet >= GHOST_DAYS else None
        out.append(d)
    return out


def job_detail(c, jid):
    j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j:
        return None
    postings = [dict(p) | {"extra": json.loads(p["extra"]) if p["extra"] else None}
                for p in c.execute("SELECT * FROM postings WHERE job_id=? ORDER BY id != ?, is_agency, first_seen", (jid, j["primary_posting_id"]))]  # 主帖排第一
    for p in postings:
        p["searches"] = [r[0] for r in c.execute("SELECT search FROM search_hits WHERE posting_id=?", (p["id"],))]
        p["filters"] = [dict(r) for r in c.execute("SELECT rule, action, reason FROM filter_results WHERE posting_id=?", (p["id"],))]
    dups = [dict(r) for r in c.execute("""
        SELECT d.*, o.id other_id, o.title other_title, o.company other_company, o.status other_status
        FROM dup_candidates d JOIN jobs o ON o.id = CASE WHEN d.job_a=? THEN d.job_b ELSE d.job_a END
        WHERE (d.job_a=? OR d.job_b=?) AND d.status='pending'""", (jid, jid, jid))]
    emails = [dict(r) for r in c.execute(
        "SELECT id, thread_id, received_at, from_name, from_addr, subject, snippet, category FROM emails WHERE job_id=? ORDER BY received_at DESC", (jid,))]
    return {"job": dict(j), "postings": postings, "dups": dups, "emails": emails,
            "events": [dict(r) for r in c.execute("SELECT * FROM events WHERE job_id=? ORDER BY at DESC, id DESC", (jid,))],
            "qa": [dict(r) for r in c.execute("SELECT * FROM application_qa WHERE job_id=? ORDER BY id", (jid,))]}


def summary(c):
    counts = {r["status"]: r["n"] for r in c.execute("SELECT status, count(*) n FROM jobs WHERE filtered=0 GROUP BY status")}
    views = {v: c.execute(f"SELECT count(*) FROM jobs j WHERE {w}").fetchone()[0] for v, w in VIEWS.items()}
    runs = [dict(r) for r in c.execute("""SELECT r.* FROM runs r JOIN (SELECT source, max(id) id FROM runs GROUP BY source) m ON m.id=r.id""")]
    dups = c.execute("SELECT count(*) FROM dup_candidates WHERE status='pending'").fetchone()[0]
    unmatched = c.execute("SELECT count(*) FROM emails WHERE state='unmatched'").fetchone()[0]
    return {"counts": counts, "views": views, "runs": runs, "pending_dups": dups, "unmatched_emails": unmatched, "statuses": STATUSES}


class BadRequest(Exception):
    pass


class H(BaseHTTPRequestHandler):
    timeout = 30            # 读请求的超时：连上了不发数据的连接不会一直占着线程
    MAX_BODY = 256 * 1024   # 接口只收小 JSON

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = self.headers.get("content-length") or "0"
        if not n.isdigit() or int(n) > self.MAX_BODY:
            raise BadRequest("请求体太大或长度不对")
        return json.loads(self.rfile.read(int(n)) or b"{}")

    def route(self, method):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if method == "GET" and path == "/":
            return self.send(200, open(os.path.join(HERE, "static", "index.html"), "rb").read(), "text/html; charset=utf-8")
        # PWA：service worker 必须放在根路径，才能管整个站点
        if method == "GET" and path in STATIC:
            name, ctype = STATIC[path]
            return self.send(200, open(os.path.join(HERE, "static", name), "rb").read(), ctype)
        # 只接受同源的写请求（本机 http://127.0.0.1:8765，或经 tailscale serve 的 https://…ts.net:8443）
        # 浏览器发 POST 一定带 Origin；没有 Origin 或者不同源的一律拒绝（防别的网页借你的浏览器改数据）
        origin = self.headers.get("origin")
        if method != "GET" and (not origin or urlparse(origin).netloc != self.headers.get("host")
                                or urlparse(origin).scheme not in ("http", "https")):
            return self.send(403, {"error": "cross-origin"})
        try:
            c = connect()
        except Exception as e:
            return self.send(503, {"error": f"数据库打不开：{e}"})
        try:
            if method == "GET" and path == "/api/summary":
                return self.send(200, summary(c))
            if method == "GET" and path == "/api/jobs":
                return self.send(200, list_jobs(c, qs.get("view", "active"), qs.get("q"), qs.get("source")))
            if m := re.fullmatch(r"/api/jobs/(\d+)", path):
                d = job_detail(c, int(m[1]))
                return self.send(200 if d else 404, d or {"error": "not found"})
            if method == "POST" and (m := re.fullmatch(r"/api/jobs/(\d+)/(\w+)", path)):
                jid, action, b = int(m[1]), m[2], self.body()
                t = now()
                if action == "status":
                    if b.get("status") not in STATUSES:
                        return self.send(400, {"error": "bad status"})
                    # 撤销等操作带上"期望的当前状态"：期间被邮件同步改过了就不覆盖
                    if b.get("expect") and (c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone() or [None])[0] != b["expect"]:
                        return self.send(409, {"error": "状态已经被别处改过了", "job": job_detail(c, jid)})
                    c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (b["status"], t, jid))
                    c.execute("INSERT INTO events (job_id, at, status, source, detail) VALUES (?,?,?,?,?)",
                              (jid, t, b["status"], "manual", b.get("detail") or None))
                elif action == "note":  # 时间线上加一条备注，不改状态
                    c.execute("INSERT INTO events (job_id, at, source, detail) VALUES (?,?,?,?)", (jid, t, "manual", b["detail"]))
                elif action == "notes":
                    c.execute("UPDATE jobs SET notes=?, updated_at=? WHERE id=?", (b.get("notes", ""), t, jid))
                elif action == "qa":
                    c.execute("INSERT INTO application_qa (job_id, question, answer, source, created_at) VALUES (?,?,?,?,?)",
                              (jid, (b.get("question") or "").strip() or None, (b.get("answer") or "").strip(), b.get("source", "paste"), t))
                elif action == "override":
                    v = int(bool(b.get("value")))
                    c.execute("UPDATE jobs SET filter_override=?, filtered=CASE WHEN ? THEN 0 ELSE "
                              "(SELECT count(*)>0 FROM filter_results f JOIN postings p ON p.id=f.posting_id WHERE p.job_id=jobs.id AND f.action='reject') END, "
                              "updated_at=? WHERE id=?", (v, v, t, jid))
                    c.execute("INSERT INTO events (job_id, at, source, detail) VALUES (?,?,?,?)",
                              (jid, t, "manual", "手动放行（忽略过滤规则）" if v else "取消手动放行"))
                else:
                    return self.send(404, {"error": "unknown action"})
                c.commit()
                return self.send(200, job_detail(c, jid))
            if method == "POST" and (m := re.fullmatch(r"/api/qa/(\d+)/delete", path)):
                c.execute("DELETE FROM application_qa WHERE id=?", (int(m[1]),))
                c.commit()
                return self.send(200, {"ok": True})
            if method == "GET" and path == "/api/emails":
                rows = c.execute("""SELECT id, thread_id, received_at, from_name, from_addr, subject, snippet, category, company, title, match_reason
                                    FROM emails WHERE state=? ORDER BY received_at DESC LIMIT 200""", (qs.get("state", "unmatched"),))
                return self.send(200, [dict(r) for r in rows])
            if method == "POST" and (m := re.fullmatch(r"/api/emails/(\w+)/(link|ignore)", path)):
                e = c.execute("SELECT * FROM emails WHERE id=?", (m[1],)).fetchone()
                if not e:
                    return self.send(404, {"error": "not found"})
                if m[2] == "ignore":
                    c.execute("UPDATE emails SET state='ignored' WHERE id=?", (m[1],))
                    c.commit()
                    return self.send(200, {"ok": True})
                jid = int(self.body()["job_id"])
                if e["state"] == "matched" and e["job_id"] == jid:   # 重复点击：不重复写时间线
                    return self.send(200, {"ok": True, "status": None, "job": jid})
                new = gmail_sync.apply_email(c, m[1], jid, e["category"] or "other", e["subject"], source="manual")
                c.execute("UPDATE emails SET match_reason='手动关联' WHERE id=?", (m[1],))
                c.commit()
                return self.send(200, {"ok": True, "status": new, "job": jid})
            if method == "GET" and path == "/api/blocked":   # 手动拉黑的公司
                return self.send(200, rules.blocked_list())
            if method == "POST" and path == "/api/blocked":
                b = self.body(); name = (b.get("company") or "").strip()
                if not rules.norm_company(name):
                    return self.send(400, {"error": "company required"})
                rules.set_blocked(name, bool(b.get("blocked", True)), now())
                n = refilter_company(c, name)
                return self.send(200, {"ok": True, "jobs": n, "list": rules.blocked_list()})
            if method == "GET" and path == "/api/profile":   # 「我的」页：投递时要填的个人信息，存在本地库里（不进 git）
                r = c.execute("SELECT value FROM kv WHERE key='profile'").fetchone()
                return self.send(200, json.loads(r[0]) if r else DEFAULT_PROFILE)
            if method == "POST" and path == "/api/profile":
                items = [{"group": str(i.get("group") or "其他")[:40], "label": str(i.get("label") or "")[:80], "value": str(i.get("value") or "")[:2000]}
                         for i in self.body().get("items", []) if i.get("label")]
                c.execute("INSERT INTO kv (key, value) VALUES ('profile', ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                          (json.dumps(items, ensure_ascii=False),))
                c.commit()
                return self.send(200, items)
            if method == "GET" and path == "/api/push/key":
                return self.send(200, {"key": push.keys()[1],
                                       "count": c.execute("SELECT count(*) FROM push_subscriptions").fetchone()[0]})
            if method == "POST" and path == "/api/push/subscribe":
                b = self.body()
                if not (push.valid_endpoint(b.get("endpoint")) and b.get("keys", {}).get("p256dh") and b["keys"].get("auth")):
                    return self.send(400, {"error": "bad subscription"})
                c.execute("""INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at) VALUES (?,?,?,?,?)
                             ON CONFLICT (endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth""",
                          (b["endpoint"], b["keys"]["p256dh"], b["keys"]["auth"], self.headers.get("user-agent"), now()))
                c.commit()
                return self.send(200, {"ok": True})
            if method == "POST" and path == "/api/push/unsubscribe":
                c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (self.body().get("endpoint"),))
                c.commit()
                return self.send(200, {"ok": True})
            if method == "POST" and path == "/api/push/test":
                ok, total = push.send_all(c, "测试通知", "推送正常，之后有新职位会这样提醒你。")
                return self.send(200, {"ok": ok, "total": total})
            if method == "POST" and path == "/api/dups":
                b = self.body()
                a, bb = sorted((int(b["a"]), int(b["b"])))
                if a == bb or b.get("action") not in ("merge", "distinct"):
                    return self.send(400, {"error": "bad request"})
                if not c.execute("SELECT 1 FROM dup_candidates WHERE job_a=? AND job_b=? AND status='pending'", (a, bb)).fetchone():
                    return self.send(409, {"error": "这对疑似重复已经处理过了"})
                if b["action"] == "merge":
                    keep = int(b.get("keep") or a)
                    if keep not in (a, bb):
                        return self.send(400, {"error": "keep 必须是这两个职位之一"})
                    merge_jobs(c, keep, bb if keep == a else a)
                    c.commit()
                    return self.send(200, {"ok": True, "job": keep})
                c.execute("UPDATE dup_candidates SET status='distinct' WHERE job_a=? AND job_b=?", (a, bb))
                c.commit()
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except BadRequest as e:
            c.rollback()
            return self.send(400, {"error": str(e)})
        except Exception as e:
            c.rollback()
            return self.send(500, {"error": str(e)})
        finally:
            c.close()

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
