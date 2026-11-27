"""匹配分：用 agy CLI（Antigravity，默认模型 neon medium）按简历给职位打 0-100 分。

- 简历和打分规则在 ~/job-match/AGENTS.md（不在这个仓库里，不进 git），每次调用读出来和 JD 一起发。
  用精简 agent scorer（同目录 .agents/agents/scorer/agent.md）跑，不带 agy 自带的系统提示词和工具。
  你也可以 cd 过去直接和 agy 聊某个 JD（默认 agent 会自动读 AGENTS.md）。
- 只给通过规则、待处理（new / shortlisted）、还没打过分的职位打分。每个职位只打一次。
- 结果：jobs.llm_score / llm_track（ai / swe / other_story / cross_stack）/ llm_detail（JSON：理由、缺口、风险）。
- 排序分 = llm_score；track 是 swe 且不能一键投的，排序时减 SWE_PENALTY（RANK_SQL）。
- 实测（2026-11-26）：单个 12-20 秒，3 个并发；30 次约用 Gemini 5 小时额度的 4%、每周额度的 1%。

  python3 score.py            # 给待处理里没打分的职位打分
  python3 score.py --all      # 通过规则的、没打分的职位全部打分（包括已处理的）
  python3 score.py --rescore  # 待处理的全部重打（改了 AGENTS.md 之后用）
"""
import argparse, json, os, shutil, sqlite3, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from db import DB_PATH, connect, now

WORKDIR = os.environ.get("AUTO_JOB_MATCH_DIR", os.path.expanduser("~/job-match"))
MODEL = os.environ.get("AUTO_JOB_AGY_MODEL", "gemini-4-neon-medium")
AGY = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
# 精简 agent（~/job-match/.agents/agents/scorer/agent.md）：excludeDefaultComponents 去掉 agy 自带的系统提示词和工具，
# 每次调用的固定开销从约 1 万 token 降到几百。它也不会自动读 AGENTS.md，所以规则和简历由这里读出来放进消息里。
AGENT = "scorer"
PARALLEL = 3
TIMEOUT = 240          # 秒，单个职位
JD_CHARS = 12000
TRACKS = ["ai", "swe", "other_story", "cross_stack"]
SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "track": {"type": "string", "enum": TRACKS},
        "reason": {"type": "string"},
        "gaps_minor": {"type": "array", "items": {"type": "string"}},
        "gaps_major": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "track", "reason", "gaps_minor", "gaps_major", "red_flags"],
}
# 排序分（网页列表、手机推送共用）：不怎么提 AI 的普通 SWE 职位，只有能一键投的才按原分排，否则往后放。
# 用 SQL 表达式算，改这里的规则不用重新打分。表别名必须是 j。
SWE_PENALTY = 25
QUICK_SQL = "EXISTS (SELECT 1 FROM postings x WHERE x.job_id=j.id AND json_extract(x.extra, '$.quick_apply'))"
RANK_SQL = f"(j.llm_score - CASE WHEN j.llm_track='swe' AND NOT {QUICK_SQL} THEN {SWE_PENALTY} ELSE 0 END)"


class Unavailable(Exception):
    pass


def _rows(conn, which):
    where = {"pending": "j.filtered=0 AND j.status IN ('new','shortlisted') AND j.llm_score IS NULL",
             "all": "j.filtered=0 AND j.llm_score IS NULL",
             "rescore": "j.filtered=0 AND j.status IN ('new','shortlisted')"}[which]
    return conn.execute(f"""
        SELECT j.id, j.created_at, j.title, j.company, j.location,
               (SELECT description FROM postings p WHERE p.job_id=j.id AND description IS NOT NULL
                ORDER BY length(description) DESC LIMIT 1) jd,
               (SELECT group_concat(DISTINCT coalesce(work_arrangement, '')) FROM postings p WHERE p.job_id=j.id) wa,
               (SELECT group_concat(DISTINCT salary) FROM postings p WHERE p.job_id=j.id) salary
        FROM jobs j WHERE {where} ORDER BY j.id DESC""").fetchall()


def _rules():
    return open(os.path.join(WORKDIR, "AGENTS.md")).read()


def _parse(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(t[t.index("{"):t.rindex("}") + 1])


def score_one(r):
    """返回 (job_id, dict | None, 错误信息)。"""
    jd = (r["jd"] or "")[:JD_CHARS]
    prompt = (f"{_rules()}\n\n---\n\nScore this job against the candidate above.\n\nTitle: {r['title']}\nCompany: {r['company']}\n"
              f"Location: {r['location']}\nWork arrangement: {r['wa'] or 'unknown'}\nSalary: {r['salary'] or 'not stated'}\n\n"
              f"Job description:\n{jd or '(no description available, judge from the title)'}\n\n---\n\n"
              f"Reply with only one JSON object (no code fence, no other text) matching this JSON Schema:\n{json.dumps(SCHEMA)}")
    try:
        # 不用 --json-schema：它靠 agy 的默认组件（结构化输出工具），精简 agent 没有，会反复重试好几轮。
        # 格式写在提示里，这里自己解析（容忍 ```json 代码块）。
        p = subprocess.run([AGY, "-p", prompt, "--agent", AGENT, "--model", MODEL, "--output-format", "json",
                            "--disable-slash-commands",
                            "--print-timeout", f"{TIMEOUT}s"],
                           capture_output=True, text=True, cwd=WORKDIR, timeout=TIMEOUT + 30)
        d = json.loads(p.stdout)
        if d.get("status") != "SUCCESS":
            return r["id"], None, f"agy status {d.get('status')}: {str(d.get('response'))[:200]}"
        out = _parse(d["response"])
        if out.get("track") not in TRACKS or not isinstance(out.get("score"), int):
            return r["id"], None, f"返回格式不对: {d['response'][:200]}"
        out["score"] = max(0, min(100, out["score"]))
        return r["id"], out, None
    except subprocess.TimeoutExpired:
        return r["id"], None, "超时"
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return r["id"], None, f"解析失败 {e}: {(p.stdout or p.stderr)[:200]}"


def check():
    """agy / 规则文件不在就抛 Unavailable。"""
    if not os.path.exists(AGY):
        raise Unavailable("找不到 agy")
    for f in ("AGENTS.md", f".agents/agents/{AGENT}/agent.md"):
        if not os.path.exists(os.path.join(WORKDIR, f)):
            raise Unavailable(f"{WORKDIR}/{f} 不存在")


def _save(conn, r, out, err, tag=""):
    """写结果，返回 True = 打上了分。r 是打分时读到的那一行（id + created_at）。
    只更新还是同一个职位的行：打分期间它可能被合并删掉，而 SQLite 会把最大的 id 重新分配给下一个新职位，
    只按 id 更新的话分数会写到别的职位上。"""
    jid = r["id"]
    if out is None:
        print(f"[score] #{jid} 失败：{err}", file=sys.stderr)
        return False
    detail = {k: out[k] for k in ("reason", "gaps_minor", "gaps_major", "red_flags")} | {"model": MODEL, "at": now()}
    n = conn.execute("UPDATE jobs SET llm_score=?, llm_track=?, llm_detail=? WHERE id=? AND created_at=?",
                     (out["score"], out["track"], json.dumps(detail, ensure_ascii=False), jid, r["created_at"])).rowcount
    conn.commit()
    if not n:
        print(f"[score] #{jid} 打分期间被合并或删除了，丢弃这个分数", file=sys.stderr)
        return False
    print(f"[score] {tag}#{jid} {out['score']:3d} {out['track']:<11} {out['reason']}", file=sys.stderr)
    return True


def run(conn, which="pending", verbose=True):
    """一次性打分（命令行、补漏用）。返回 {"scored": n, "failed": n}。"""
    check()
    rows = _rows(conn, which)
    ok = bad = 0
    with ThreadPoolExecutor(PARALLEL) as ex:
        for n, (r, (_, out, err)) in enumerate(zip(rows, ex.map(score_one, rows)), 1):
            if _save(conn, r, out, err, f"{n}/{len(rows)} "):
                ok += 1
            else:
                bad += 1
    return {"scored": ok, "failed": bad}


class Background:
    """抓取期间边抓边打分：每 POLL 秒查一次新入库、还没打分的待处理职位，交给线程池（最多 PARALLEL 个同时跑）。
    新职位入库后十几秒就有分数，不用等整轮抓取（LinkedIn 登录模式一轮要 7-10 分钟）结束。
    每个职位这一轮只试一次；失败的留给下一轮。用自己的数据库连接（sqlite 连接不能跨线程）。

        bg = Background().start()   # agy 不可用时抛 Unavailable
        ...抓取...
        stats = bg.finish()         # 停止找新职位，等已提交的打完
    """
    POLL = 5

    def __init__(self):
        self._stop = threading.Event()
        self._seen = set()
        self._futs = []
        self._pool = ThreadPoolExecutor(PARALLEL)
        self._db_lock = threading.Lock()
        self._db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        check()
        self._thread.start()
        return self

    def _loop(self):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            while True:
                stopping = self._stop.is_set()
                try:
                    for r in _rows(conn, "pending"):
                        key = (r["id"], r["created_at"])   # id 可能被合并后复用，带上创建时间
                        if key not in self._seen:
                            self._seen.add(key)
                            self._futs.append(self._pool.submit(self._one, r))
                except sqlite3.Error as e:   # 抓取那边正在写：下一次再查
                    print(f"[score] 查新职位出错：{e}", file=sys.stderr)
                if stopping:   # 抓取结束后再查最后一遍：最后几秒入库的职位也要打（之前会漏到下一轮）
                    break
                self._stop.wait(self.POLL)
        finally:
            conn.close()

    def _one(self, r):
        _, out, err = score_one(r)
        with self._db_lock:
            return _save(self._db, r, out, err)

    def finish(self):
        self._stop.set()
        self._thread.join()
        ok = sum(1 for f in self._futs if f.result())
        self._pool.shutdown()
        self._db.close()
        return {"scored": ok, "failed": len(self._futs) - ok}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true")
    g.add_argument("--rescore", action="store_true")
    a = ap.parse_args()
    print(run(connect(), "all" if a.all else "rescore" if a.rescore else "pending"))
