"""匹配分：用 agy CLI（Antigravity，默认模型 neon medium）按简历给职位打 0-100 分。

- 简历和打分规则在 ~/job-match/AGENTS.md（不在这个仓库里，不进 git）。agy 在那个目录里运行时会自动读它，
  所以每次调用只需要传 JD。你也可以 cd 过去直接和 agy 聊某个 JD。
- 只给通过规则、待处理（new / shortlisted）、还没打过分的职位打分。每个职位只打一次。
- 结果：jobs.llm_score / llm_track（ai / swe / other_story / cross_stack）/ llm_detail（JSON：理由、缺口、风险）。
- 排序分 = llm_score；track 是 swe 且不能一键投的，排序时减 SWE_PENALTY（RANK_SQL）。
- 实测（2026-11-26）：单个 12-20 秒，3 个并发；30 次约用 Gemini 5 小时额度的 4%、每周额度的 1%。

  python3 score.py            # 给待处理里没打分的职位打分
  python3 score.py --all      # 通过规则的、没打分的职位全部打分（包括已处理的）
  python3 score.py --rescore  # 待处理的全部重打（改了 AGENTS.md 之后用）
"""
import argparse, json, os, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from db import connect, now

WORKDIR = os.environ.get("AUTO_JOB_MATCH_DIR", os.path.expanduser("~/job-match"))
MODEL = os.environ.get("AUTO_JOB_AGY_MODEL", "gemini-4-neon-medium")
AGY = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
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
        SELECT j.id, j.title, j.company, j.location,
               (SELECT description FROM postings p WHERE p.job_id=j.id AND description IS NOT NULL
                ORDER BY length(description) DESC LIMIT 1) jd,
               (SELECT group_concat(DISTINCT coalesce(work_arrangement, '')) FROM postings p WHERE p.job_id=j.id) wa,
               (SELECT group_concat(DISTINCT salary) FROM postings p WHERE p.job_id=j.id) salary
        FROM jobs j WHERE {where} ORDER BY j.id DESC""").fetchall()


def score_one(r):
    """返回 (job_id, dict | None, 错误信息)。"""
    jd = (r["jd"] or "")[:JD_CHARS]
    prompt = (f"Score this job against the candidate in AGENTS.md.\n\nTitle: {r['title']}\nCompany: {r['company']}\n"
              f"Location: {r['location']}\nWork arrangement: {r['wa'] or 'unknown'}\nSalary: {r['salary'] or 'not stated'}\n\n"
              f"Job description:\n{jd or '(no description available, judge from the title)'}")
    try:
        p = subprocess.run([AGY, "-p", prompt, "--model", MODEL, "--output-format", "json",
                            "--json-schema", json.dumps(SCHEMA), "--disable-slash-commands",
                            "--print-timeout", f"{TIMEOUT}s"],
                           capture_output=True, text=True, cwd=WORKDIR, timeout=TIMEOUT + 30)
        d = json.loads(p.stdout)
        if d.get("status") != "SUCCESS":
            return r["id"], None, f"agy status {d.get('status')}: {str(d.get('response'))[:200]}"
        out = json.loads(d["response"])
        if out.get("track") not in TRACKS or not isinstance(out.get("score"), int):
            return r["id"], None, f"返回格式不对: {d['response'][:200]}"
        out["score"] = max(0, min(100, out["score"]))
        return r["id"], out, None
    except subprocess.TimeoutExpired:
        return r["id"], None, "超时"
    except (json.JSONDecodeError, KeyError) as e:
        return r["id"], None, f"解析失败 {e}: {(p.stdout or p.stderr)[:200]}"


def run(conn, which="pending", verbose=True):
    """打分。返回 {"scored": n, "failed": n}。agy 不存在 / 规则文件不存在时抛 Unavailable。"""
    if not os.path.exists(AGY):
        raise Unavailable("找不到 agy")
    if not os.path.exists(os.path.join(WORKDIR, "AGENTS.md")):
        raise Unavailable(f"{WORKDIR}/AGENTS.md 不存在")
    rows = _rows(conn, which)
    ok = bad = 0
    t0 = time.time()
    with ThreadPoolExecutor(PARALLEL) as ex:
        for jid, out, err in ex.map(score_one, rows):
            if out is None:
                bad += 1
                print(f"[score] {jid} 失败：{err}", file=sys.stderr)
                continue
            detail = {k: out[k] for k in ("reason", "gaps_minor", "gaps_major", "red_flags")} | {"model": MODEL, "at": now()}
            # 只更新还在库里的职位（打分期间可能被合并删掉）
            conn.execute("UPDATE jobs SET llm_score=?, llm_track=?, llm_detail=? WHERE id=?",
                         (out["score"], out["track"], json.dumps(detail, ensure_ascii=False), jid))
            conn.commit()
            ok += 1
            if verbose:
                print(f"[score] {ok + bad}/{len(rows)} #{jid} {out['score']:3d} {out['track']:<11} {out['reason']}  ({time.time() - t0:.0f}s)",
                      file=sys.stderr)
    return {"scored": ok, "failed": bad}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true")
    g.add_argument("--rescore", action="store_true")
    a = ap.parse_args()
    print(run(connect(), "all" if a.all else "rescore" if a.rescore else "pending"))
