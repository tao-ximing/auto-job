"""把抓到的帖子写进 SQLite：同平台去重（source + source_id）→ 套用过滤规则 → 跨平台 / 跨帖子去重挂到 job 上。

抓取脚本每条帖子输出一个 dict（JSONL 的一行）：
  必填: source, source_id, search
  新帖子还应该带: url, title, company, location, description, posted_at(UTC ISO)，可选 work_type, work_arrangement, salary, extra
  已知帖子可以只带必填字段（LinkedIn 增量抓取时不再点开），只更新 last_seen。

也可以单独用:  python3 ingest.py < postings.jsonl
改了 filters.toml 之后:  python3 ingest.py --refilter   （只重算过滤，保留手动状态）
"""
import json, sys
import rules
from db import connect, now


class Ingestor:
    def __init__(self, conn):
        self.conn = conn
        self._cache = None  # job_id -> [dict(company_norm, title_norm, sh)]，每条 posting 一项

    def _load_cache(self):
        if self._cache is None:
            self._cache = {}
            for r in self.conn.execute("SELECT job_id, company, title, description FROM postings WHERE job_id IS NOT NULL"):
                self._cache.setdefault(r["job_id"], []).append(self._sig(r["company"], r["title"], r["description"]))
        return self._cache

    @staticmethod
    def _sig(company, title, description):
        return {"company_norm": rules.norm_company(company), "title_norm": rules.norm_title(title), "sh": rules.shingles(description)}

    def known_ids(self, source):
        return {r[0] for r in self.conn.execute("SELECT source_id FROM postings WHERE source=?", (source,))}

    # ── 主入口 ──
    def add(self, p):
        """返回 'new' 或 'seen'。"""
        t = now()
        c = self.conn
        row = c.execute("SELECT * FROM postings WHERE source=? AND source_id=?", (p["source"], str(p["source_id"]))).fetchone()
        if row:
            pid = row["id"]
            fields = {k: p[k] for k in ("url", "title", "company", "location", "work_type", "work_arrangement", "salary", "posted_at") if p.get(k)}
            changed_jd = p.get("description") and rules.jd_hash(p["description"]) != row["jd_hash"]
            if changed_jd:
                fields |= {"description": p["description"], "jd_hash": rules.jd_hash(p["description"])}
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE postings SET last_seen=?{', ' + sets if sets else ''} WHERE id=?", (t, *fields.values(), pid))
            if changed_jd:
                self._apply_filters(pid, {**dict(row), **fields})
                self._refresh_job(row["job_id"])
            self._hit(pid, p["search"], t)
            return "seen"

        results, is_agency = rules.evaluate(p)
        cur = c.execute(
            """INSERT INTO postings (source, source_id, url, title, company, location, work_type, work_arrangement, salary,
                                     posted_at, description, jd_hash, is_agency, extra, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["source"], str(p["source_id"]), p.get("url"), p.get("title"), p.get("company"), p.get("location"),
             p.get("work_type"), p.get("work_arrangement"), p.get("salary"), p.get("posted_at"), p.get("description"),
             rules.jd_hash(p.get("description")), int(is_agency), json.dumps(p.get("extra")) if p.get("extra") else None, t, t))
        pid = cur.lastrowid
        self._write_filters(pid, results)
        self._hit(pid, p["search"], t)
        self._attach(pid, p)
        return "new"

    def _hit(self, pid, search, t):
        self.conn.execute("""INSERT INTO search_hits (posting_id, search, first_seen, last_seen) VALUES (?,?,?,?)
                             ON CONFLICT (posting_id, search) DO UPDATE SET last_seen=excluded.last_seen""", (pid, search, t, t))

    def _write_filters(self, pid, results):
        self.conn.execute("DELETE FROM filter_results WHERE posting_id=?", (pid,))
        self.conn.executemany("INSERT OR IGNORE INTO filter_results (posting_id, rule, action, reason) VALUES (?,?,?,?)",
                              [(pid, *r) for r in results])

    def _apply_filters(self, pid, p):
        results, is_agency = rules.evaluate(p)
        self._write_filters(pid, results)
        self.conn.execute("UPDATE postings SET is_agency=? WHERE id=?", (int(is_agency), pid))

    def _attach(self, pid, p):
        """找已有的 job：能确定是同一个就挂上去，拿不准就新建 job 并记一条疑似重复。"""
        cache = self._load_cache()
        sig = self._sig(p.get("company"), p.get("title"), p.get("description"))
        best, candidates = None, []
        for job_id, sigs in cache.items():
            for other in sigs:
                kind, score, reason = rules.match(sig, other)
                if kind == "auto" and (best is None or score > best[1]):
                    best = (job_id, score)
                elif kind == "candidate":
                    candidates.append((job_id, score, reason))
        t = now()
        if best:
            job_id = best[0]
            self.conn.execute("UPDATE postings SET job_id=? WHERE id=?", (job_id, pid))
            self.conn.execute("INSERT INTO events (job_id, at, source, detail) VALUES (?,?,?,?)",
                              (job_id, t, "auto", f"合并了一条新帖子: {p['source']} {p.get('company') or ''}"))
        else:
            job_id = self.conn.execute("INSERT INTO jobs (created_at, updated_at) VALUES (?,?)", (t, t)).lastrowid
            self.conn.execute("UPDATE postings SET job_id=? WHERE id=?", (job_id, pid))
            self.conn.execute("INSERT INTO events (job_id, at, status, source, detail) VALUES (?,?,?,?,?)",
                              (job_id, t, "new", "auto", f"首次发现: {p.get('search')}"))
            seen = set()
            for other, score, reason in sorted(candidates, key=lambda x: -x[1]):
                if other in seen:
                    continue
                seen.add(other)
                a, b = sorted((other, job_id))
                self.conn.execute("INSERT OR IGNORE INTO dup_candidates (job_a, job_b, score, reason) VALUES (?,?,?,?)", (a, b, score, reason))
        cache.setdefault(job_id, []).append(sig)
        self._refresh_job(job_id)

    def _refresh_job(self, job_id):
        refresh_job(self.conn, job_id)


def refresh_job(conn, job_id):
    """根据挂着的 posting 重新算 job 的展示字段和过滤状态。"""
    if job_id is None:
        return
    prim = conn.execute("SELECT * FROM postings WHERE job_id=? ORDER BY is_agency, first_seen, id LIMIT 1", (job_id,)).fetchone()
    if not prim:
        return
    acts = [r[0] for r in conn.execute(
        "SELECT f.action FROM filter_results f JOIN postings p ON p.id=f.posting_id WHERE p.job_id=?", (job_id,))]
    conn.execute(
        """UPDATE jobs SET company=?, title=?, location=?, company_norm=?, title_norm=?, primary_posting_id=?,
                           filtered=CASE WHEN filter_override THEN 0 ELSE ? END, soft_score=?, updated_at=? WHERE id=?""",
        (prim["company"], prim["title"], prim["location"], rules.norm_company(prim["company"]), rules.norm_title(prim["title"]),
         prim["id"], int("reject" in acts), acts.count("soft") - acts.count("boost"), now(), job_id))


def merge_jobs(conn, keep, drop):
    """把 drop 的帖子、时间线、问答都并到 keep 上，删掉 drop。"""
    if keep == drop:
        return
    t = now()
    for table in ("postings", "events", "application_qa"):
        conn.execute(f"UPDATE {table} SET job_id=? WHERE job_id=?", (keep, drop))
    k, d = conn.execute("SELECT * FROM jobs WHERE id=?", (keep,)).fetchone(), conn.execute("SELECT * FROM jobs WHERE id=?", (drop,)).fetchone()
    # 状态保留更"往前走"的那个（比如一个已经 applied，另一个还是 new）
    order = {s: i for i, s in enumerate(["new", "shortlisted", "skipped", "closed", "applied", "assessment", "interview", "rejected", "offer"])}
    status = max((k["status"], d["status"]), key=lambda s: order.get(s, 0))
    notes = "\n".join(x for x in (k["notes"], d["notes"]) if x)
    conn.execute("UPDATE jobs SET status=?, notes=?, filter_override=? WHERE id=?", (status, notes, k["filter_override"] or d["filter_override"], keep))
    conn.execute("UPDATE dup_candidates SET status='merged' WHERE (job_a=? AND job_b=?) OR (job_a=? AND job_b=?)", (*sorted((keep, drop)), *sorted((keep, drop))))
    # drop 的其他疑似重复改挂到 keep 上
    for r in conn.execute("SELECT * FROM dup_candidates WHERE (job_a=? OR job_b=?) AND status='pending'", (drop, drop)).fetchall():
        other = r["job_b"] if r["job_a"] == drop else r["job_a"]
        conn.execute("DELETE FROM dup_candidates WHERE job_a=? AND job_b=?", (r["job_a"], r["job_b"]))
        if other != keep:
            a, b = sorted((keep, other))
            conn.execute("INSERT OR IGNORE INTO dup_candidates (job_a, job_b, score, reason) VALUES (?,?,?,?)", (a, b, r["score"], r["reason"]))
    conn.execute("DELETE FROM dup_candidates WHERE job_a=? OR job_b=?", (drop, drop))
    conn.execute("DELETE FROM jobs WHERE id=?", (drop,))
    conn.execute("INSERT INTO events (job_id, at, source, detail) VALUES (?,?,?,?)", (keep, t, "manual", f"合并了重复职位 #{drop}"))
    refresh_job(conn, keep)


def refilter(conn):
    """改了 filters.toml 之后用：只重算每条 posting 的过滤结果和 job 的过滤状态，不动去重归属、状态、问答、备注。"""
    ing = Ingestor(conn)
    for r in conn.execute("SELECT * FROM postings").fetchall():
        ing._apply_filters(r["id"], dict(r))
    for (jid,) in conn.execute("SELECT id FROM jobs").fetchall():
        refresh_job(conn, jid)
    conn.commit()


def refilter_company(conn, company):
    """拉黑 / 取消拉黑后：只重算这家公司的帖子。返回受影响的 job 数。"""
    cn = rules.norm_company(company)
    ing, jobs = Ingestor(conn), set()
    for r in conn.execute("SELECT * FROM postings").fetchall():
        if rules.norm_company(r["company"]) == cn:
            ing._apply_filters(r["id"], dict(r)); jobs.add(r["job_id"])
    for jid in jobs:
        refresh_job(conn, jid)
    conn.commit()
    return len(jobs)


def rebuild(conn):
    """改了过滤规则或去重阈值之后，从头重算所有 posting 的过滤结果和归属。有手动操作过的记录时拒绝执行，免得丢状态。"""
    if conn.execute("SELECT 1 FROM events WHERE source != 'auto' LIMIT 1").fetchone() or \
       conn.execute("SELECT 1 FROM application_qa LIMIT 1").fetchone() or \
       conn.execute("SELECT 1 FROM jobs WHERE status != 'new' OR notes != '' OR filter_override LIMIT 1").fetchone():
        raise SystemExit("库里已经有手动改过的状态/问答/备注，不能整体重建")
    rows = [dict(r) for r in conn.execute("SELECT * FROM postings ORDER BY first_seen, id")]
    conn.executescript("UPDATE postings SET job_id=NULL; DELETE FROM dup_candidates; DELETE FROM events; DELETE FROM jobs;")
    ing = Ingestor(conn)
    ing._cache = {}
    for r in rows:
        ing._apply_filters(r["id"], r)
        h = conn.execute("SELECT search FROM search_hits WHERE posting_id=? ORDER BY first_seen LIMIT 1", (r["id"],)).fetchone()
        r["search"] = h[0] if h else None
        ing._attach(r["id"], r)
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    if sys.argv[1:] in (["--rebuild"], ["--refilter"]):
        (rebuild if sys.argv[1] == "--rebuild" else refilter)(conn)
        print(dict(conn.execute("SELECT count(*) jobs, sum(filtered) filtered FROM jobs").fetchone()))
        raise SystemExit
    ing = Ingestor(conn)
    counts = {"new": 0, "seen": 0}
    for line in sys.stdin:
        if line.strip():
            counts[ing.add(json.loads(line))] += 1
    conn.commit()
    print(counts)
