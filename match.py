"""匹配分：本地 embedding（Ollama 上的 Qwen3-Embedding-0.6B，只用 CPU）+ 按你的「已投 / 不投」训练的逻辑回归。

- 每个职位算两个向量：职位名 + JD（截前 6000 字）、职位名（带任务说明），存在 job_vectors；简历不出本机。
- 特征：两个向量 + JD 和简历的相似度 + 职位名和目标方向的相似度。离线验证（2026-11-25）：AUC ≈ 0.82，
  前 20 名里平均 11 个是你投过的（随机约 3 个）。按句算"要求覆盖度"、去掉 JD 套话都没有提升，所以不做。
- 分数线：交叉验证（折外分数）下，让 KEEP_POS 比例的「投过的」落在线以上。线以下的职位不隐藏，
  只是在待处理里排到后面一组（两组内部都按时间排）。
- 标注：正 = 投过 / 进了流程；负 = 被规则放行、地点在维州或远程、你手动点了不投的。签证、地点这类原因已被规则过滤，不会混进来。
- 每天重新训练一次（模型和分数线存在 kv 表 match_model）；Ollama 没开时跳过，不影响抓取。

  python3 match.py              # 补算缺的向量 + 需要时重新训练 + 给所有职位打分
  python3 match.py --retrain    # 强制重新训练
"""
import hashlib, json, os, re, sqlite3, sys, time, urllib.request
from datetime import datetime, timezone
import numpy as np
from db import connect, now

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("AUTO_JOB_EMBED_MODEL", "qwen3-embedding:0.6b")
URL = os.environ.get("AUTO_JOB_OLLAMA", "http://127.0.0.1:11434") + "/api/embed"
KEEP_POS = 0.9          # 分数线：90% 投过的职位落在线以上
RETRAIN_HOURS = 24
L2 = 30.0
POS = ("applied", "assessment", "interview", "offer", "rejected", "shortlisted")
TITLE_TASK = "Given a job title, retrieve the matching type of role"
TARGETS = ["AI Engineer", "Applied AI Engineer", "LLM / Generative AI Engineer", "Forward Deployed Engineer",
           "AI Automation Engineer", "Machine Learning Engineer", "Copilot Studio / Power Platform consultant",
           "Document AI / OCR engineer", "Backend engineer (Python, C#/.NET) building AI products"]
LOC_OK = re.compile(r"melbourne|vic\b|victoria|remote|australia$", re.I)


class Unavailable(Exception):
    pass


def embed(texts):
    out = []
    for i in range(0, len(texts), 16):
        req = urllib.request.Request(URL, data=json.dumps({"model": MODEL, "input": texts[i:i + 16]}).encode(),
                                     headers={"content-type": "application/json"})
        try:
            out += json.load(urllib.request.urlopen(req, timeout=600))["embeddings"]
        except Exception as e:
            raise Unavailable(f"Ollama 不可用：{e}")
    v = np.array(out, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def q(task, text):   # Qwen3-Embedding：查询端带任务说明，文档端不带
    return f"Instruct: {task}\nQuery: {text}"


def job_texts(title, jd):
    return f"{title}\n{(jd or '')[:6000]}", q(TITLE_TASK, title or "")


def _resume():
    p = os.path.join(HERE, "resume.txt")
    return open(p).read() if os.path.exists(p) else ""


# ── 向量 ───────────────────────────────────────────────────────────────────
def _jd_rows(conn, where="1"):
    return conn.execute(f"""
        SELECT j.id, j.title, j.status, j.filtered, j.location,
               (SELECT description FROM postings p WHERE p.job_id=j.id AND description IS NOT NULL
                ORDER BY length(description) DESC LIMIT 1) jd
        FROM jobs j WHERE {where}""").fetchall()


def ensure_vectors(conn, verbose=True, seed_cache=None):
    """给还没有向量、且有 JD 的职位算向量。seed_cache：{文本: 向量} 字典（离线验证留下的缓存），命中就不重算。"""
    rows = [r for r in _jd_rows(conn, f"j.id NOT IN (SELECT job_id FROM job_vectors WHERE model='{MODEL}')")
            if r["jd"] and len(r["jd"]) > 200]
    t0 = time.time()
    for n, r in enumerate(rows, 1):
        doc_t, title_t = job_texts(r["title"], r["jd"])
        if seed_cache and doc_t in seed_cache and title_t in seed_cache:
            v = np.array([seed_cache[doc_t], seed_cache[title_t]], dtype=np.float32)
            v /= np.linalg.norm(v, axis=1, keepdims=True)
        else:
            v = embed([doc_t, title_t])
        conn.execute("INSERT OR REPLACE INTO job_vectors (job_id, model, doc, title, created_at) VALUES (?,?,?,?,?)",
                     (r["id"], MODEL, v[0].tobytes(), v[1].tobytes(), now()))
        conn.commit()
        if verbose and (n % 25 == 0 or n == len(rows)):
            print(f"[match] 向量 {n}/{len(rows)}  {time.time() - t0:.0f}s", file=sys.stderr)
    return len(rows)


def _features(conn, ids=None):
    """返回 (job_ids, X)。X = [JD 向量, 职位名向量, JD·简历, max(职位名·目标方向)]。"""
    R = embed([_resume()[:8000]])[0]
    T = embed(TARGETS)
    sql = f"SELECT job_id, doc, title FROM job_vectors WHERE model='{MODEL}'"
    rows = conn.execute(sql).fetchall()
    if ids is not None:
        keep = set(ids); rows = [r for r in rows if r["job_id"] in keep]
    if not rows:
        return [], np.zeros((0, 2050), np.float32)
    D = np.stack([np.frombuffer(r["doc"], np.float32) for r in rows])
    Tt = np.stack([np.frombuffer(r["title"], np.float32) for r in rows])
    return [r["job_id"] for r in rows], np.c_[D, Tt, D @ R, (Tt @ T.T).max(1)]


# ── 训练 ───────────────────────────────────────────────────────────────────
def labels(conn):
    rows = _jd_rows(conn, f"""j.status IN {POS} OR (j.status='skipped' AND j.filtered=0
        AND EXISTS (SELECT 1 FROM events e WHERE e.job_id=j.id AND e.status='skipped' AND e.source='manual'))""")
    return {r["id"]: int(r["status"] in POS) for r in rows
            if r["jd"] and len(r["jd"]) > 200 and (r["status"] in POS or LOC_OK.search(r["location"] or ""))}


def _fit(X, y):
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = (X - mu) / sd
    w, b = np.zeros(X.shape[1]), 0.0
    sw = np.where(y == 1, (1 - y.mean()) / max(y.mean(), 1e-6), 1.0)   # 正样本少，加权
    for _ in range(500):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = sw * (p - y)
        w -= 0.1 * (Z.T @ g / len(g) + L2 * w / len(g)); b -= 0.1 * g.mean()
    return {"mu": mu, "sd": sd, "w": w, "b": b}


def _score(m, X):
    return ((X - m["mu"]) / m["sd"]) @ m["w"] + m["b"]


def _auc(s, y):
    order = np.argsort(s); ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    p = y == 1
    return float((ranks[p].sum() - p.sum() * (p.sum() + 1) / 2) / (p.sum() * (~p).sum()))


def train(conn, verbose=True):
    lab = labels(conn)
    ids, X = _features(conn, lab.keys())
    y = np.array([lab[i] for i in ids])
    if y.sum() < 10 or (1 - y).sum() < 20:
        raise RuntimeError(f"标注太少（正 {y.sum()}，负 {(1 - y).sum()}），先不训练")
    # 5 折折外分数：估计效果、定分数线（模拟没见过的新职位）
    rng = np.random.default_rng(0); fold = np.empty(len(y), int)
    for cls in (0, 1):
        ii = rng.permutation(np.where(y == cls)[0]); fold[ii] = np.arange(len(ii)) % 5
    oof = np.zeros(len(y))
    for f in range(5):
        m = _fit(X[fold != f], y[fold != f]); oof[fold == f] = _score(m, X[fold == f])
    th = float(np.quantile(oof[y == 1], 1 - KEEP_POS))
    m = _fit(X, y)
    model = {k: v.tolist() if hasattr(v, "tolist") else v for k, v in m.items()} | {
        "threshold": th, "trained_at": now(), "n_pos": int(y.sum()), "n_neg": int((1 - y).sum()),
        "auc": _auc(oof, y), "below_neg": float((oof[y == 0] < th).mean()), "embed_model": MODEL,
        "resume_hash": hashlib.sha1(_resume().encode()).hexdigest()[:12]}
    conn.execute("INSERT INTO kv (key, value) VALUES ('match_model', ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                 (json.dumps(model),))
    conn.commit()
    if verbose:
        print(f"[match] 训练完成：正 {model['n_pos']} 负 {model['n_neg']}，交叉验证 AUC {model['auc']:.3f}，"
              f"分数线以下：{1 - KEEP_POS:.0%} 投过的 / {model['below_neg']:.0%} 不投的", file=sys.stderr)
    return model


def load_model(conn):
    r = conn.execute("SELECT value FROM kv WHERE key='match_model'").fetchone()
    if not r:
        return None
    m = json.loads(r[0])
    return {k: np.array(v) if isinstance(v, list) else v for k, v in m.items()}


def score_all(conn, m):
    ids, X = _features(conn)
    if not ids:
        return 0
    s = _score(m, X)
    conn.executemany("UPDATE jobs SET match_score=?, match_high=? WHERE id=?",
                     [(float(v), int(v >= m["threshold"]), i) for i, v in zip(ids, s)])
    conn.commit()
    return len(ids)


def run(conn, force_retrain=False, verbose=True, seed_cache=None):
    """抓取后调用：补向量 → 需要时重新训练 → 全部重新打分。Ollama 不可用时抛 Unavailable。"""
    n_new = ensure_vectors(conn, verbose, seed_cache)
    m = load_model(conn)
    stale = m is None or m.get("embed_model") != MODEL or m.get("resume_hash") != hashlib.sha1(_resume().encode()).hexdigest()[:12] \
        or (datetime.now(timezone.utc) - datetime.fromisoformat(m["trained_at"].replace("Z", "+00:00"))).total_seconds() > RETRAIN_HOURS * 3600
    if force_retrain or stale:
        try:
            train(conn, verbose)
        except RuntimeError as e:
            print(f"[match] {e}", file=sys.stderr)
        m = load_model(conn)
    if m is None:
        return {"vectors": n_new, "scored": 0}
    return {"vectors": n_new, "scored": score_all(conn, m)}


if __name__ == "__main__":
    seed = None
    if "--seed-cache" in sys.argv:   # 离线验证留下的向量缓存，首次回填时用
        seed = json.load(open(sys.argv[sys.argv.index("--seed-cache") + 1]))
    print(run(connect(), force_retrain="--retrain" in sys.argv, seed_cache=seed))
