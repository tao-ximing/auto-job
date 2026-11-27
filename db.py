"""SQLite 存储。一个文件 jobs.db，抓取和网页共用（WAL 模式，可以同时读写）。

job 是"一个真实的职位"，posting 是某个平台上的一条帖子。同一个职位在 SEEK 和 LinkedIn 上各有一条，
或者被多家中介转发，就是一个 job 挂着好几个 posting。
"""
import os, sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("AUTO_JOB_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db"))

# 职位状态：全项目只有这一套。邮件分类的结果也直接是其中之一（EMAIL_STATUSES），网页、推送用同一套中文名。
STATUSES = ["new", "shortlisted", "skipped", "applied", "assessment", "interview", "rejected", "offer", "closed"]
STATUS_LABEL = {"new": "新", "shortlisted": "想投", "skipped": "不投", "applied": "已投", "assessment": "笔试/测评",
                "interview": "面试", "rejected": "被拒", "offer": "Offer", "closed": "已关闭"}
# 一封求职邮件能说明的状态。"对方已查看"之类说明你投过了，算 applied。认不出来的存 NULL，不改职位状态。
EMAIL_STATUSES = ["applied", "assessment", "interview", "offer", "rejected", "closed"]
_RANK = {"new": 0, "shortlisted": 1, "skipped": 1, "closed": 1, "applied": 2, "assessment": 3, "interview": 4, "rejected": 5, "offer": 6}


def next_status(cur, target):
    """职位现在是 cur，收到一封说明 target 的邮件后应该变成什么；不变返回 None。状态怎么变只写在这里。
    - rejected：除了 offer 都改（拒信可能跳过中间步骤）
    - closed（职位已关闭）：还没进入测评 / 面试的申请算结束；之后再来面试邀请照样往前推
    - 其他：只往前走，offer / rejected 之后不再变"""
    if target not in EMAIL_STATUSES or target == cur:
        return None
    if target == "rejected":
        return None if cur == "offer" else target
    if target == "closed":
        return target if cur in ("new", "shortlisted", "skipped", "applied") else None
    if cur in ("offer", "rejected"):
        return None
    return target if _RANK[target] > _RANK.get(cur, 0) else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  company TEXT, title TEXT, location TEXT,          -- 取自主帖（优先非中介、最早看到的那条）
  company_norm TEXT, title_norm TEXT,
  primary_posting_id INTEGER,
  status TEXT NOT NULL DEFAULT 'new',
  filtered INTEGER NOT NULL DEFAULT 0,               -- 1 = 被 reject 规则命中（且没有手动放行）
  filter_override INTEGER NOT NULL DEFAULT 0,        -- 1 = 手动放行，忽略 reject
  soft_score INTEGER NOT NULL DEFAULT 0,             -- soft 命中次数减 boost 命中次数，越大排越后
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postings (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, source_id TEXT NOT NULL,
  job_id INTEGER REFERENCES jobs(id),
  url TEXT, title TEXT, company TEXT, location TEXT,
  work_type TEXT, work_arrangement TEXT, salary TEXT,
  posted_at TEXT,                                    -- UTC ISO；LinkedIn 的 "3 hours ago" 在抓取时换算
  description TEXT, jd_hash TEXT,
  is_agency INTEGER NOT NULL DEFAULT 0,
  extra TEXT,                                        -- 平台特有字段（JSON）
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS postings_job ON postings(job_id);
CREATE TABLE IF NOT EXISTS search_hits (
  posting_id INTEGER NOT NULL REFERENCES postings(id),
  search TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  PRIMARY KEY (posting_id, search)
);
CREATE TABLE IF NOT EXISTS filter_results (
  posting_id INTEGER NOT NULL REFERENCES postings(id),
  rule TEXT NOT NULL, action TEXT NOT NULL, reason TEXT,
  PRIMARY KEY (posting_id, rule, reason)
);
CREATE TABLE IF NOT EXISTS events (                  -- 状态时间线；jobs.status 只是最新一条
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  at TEXT NOT NULL,
  status TEXT,                                       -- 状态变化时填
  source TEXT NOT NULL,                              -- auto / manual / email
  detail TEXT
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE TABLE IF NOT EXISTS application_qa (          -- 申请时被问到的问题和你的回答
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  question TEXT,                                     -- 为空 = 整段粘贴的原文，放在 answer 里
  answer TEXT,
  source TEXT NOT NULL DEFAULT 'paste',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dup_candidates (          -- 疑似重复，等网页上确认
  job_a INTEGER NOT NULL, job_b INTEGER NOT NULL,    -- job_a < job_b
  score REAL, reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending',            -- pending / merged / distinct
  PRIMARY KEY (job_a, job_b)
);
CREATE TABLE IF NOT EXISTS push_subscriptions (    -- 手机上开了通知的浏览器 / 主屏幕 PWA
  endpoint TEXT PRIMARY KEY,
  p256dh TEXT NOT NULL, auth TEXT NOT NULL,
  user_agent TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (                -- 和求职相关的邮件（不相关的不存）
  id TEXT PRIMARY KEY,                               -- Gmail message id
  thread_id TEXT, received_at TEXT,
  from_addr TEXT, from_name TEXT, subject TEXT, snippet TEXT,
  body TEXT,                                         -- 去掉链接、截断后的正文
  status TEXT,                                       -- 邮件说明的职位状态（EMAIL_STATUSES 之一），NULL = 没认出来
  company TEXT, title TEXT,                          -- 从邮件里提取的
  job_id INTEGER REFERENCES jobs(id),
  match_reason TEXT,
  state TEXT NOT NULL DEFAULT 'unmatched',           -- matched / unmatched / ignored
  classified_by TEXT,                                -- rule / gemini / manual
  lookup_at TEXT, lookup_result TEXT,                -- 邮件补抓（email_lookup.py）：什么时候去 SEEK / LinkedIn 搜过、结果
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS emails_job ON emails(job_id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS llm_usage (               -- Gemini 用量，按月记，用来卡预算
  month TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT,
  seen INTEGER DEFAULT 0, new INTEGER DEFAULT 0,
  error TEXT
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # 老库补列（CREATE TABLE IF NOT EXISTS 不会给已有的表加列）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    if "category" in cols:   # 2026-11-27：邮件分类改成直接用职位状态（原来是单独一套 8 类）
        conn.execute("ALTER TABLE emails RENAME COLUMN category TO status")
        conn.execute("UPDATE emails SET status='applied' WHERE status IN ('viewed', 'other')")
    for col in ("lookup_at", "lookup_result"):
        if col not in cols:
            conn.execute(f"ALTER TABLE emails ADD COLUMN {col} TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    # score.py（agy 打分）：llm_score 0-100，llm_track ai/swe/other_story/cross_stack，llm_detail JSON。NULL = 没打分
    for col, typ in (("llm_score", "INTEGER"), ("llm_track", "TEXT"), ("llm_detail", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    # 2026-11-26 删掉了本地 embedding 匹配分（match.py）：清掉它留下的列、表和模型
    for col in ("match_score", "match_high"):
        if col in cols:
            conn.execute(f"ALTER TABLE jobs DROP COLUMN {col}")
    conn.execute("DROP TABLE IF EXISTS job_vectors")
    conn.execute("DELETE FROM kv WHERE key='match_model'")
    # LinkedIn 的 Easy Apply 和 SEEK 的 Quick Apply 统一叫 quick_apply（网页上叫「一键投」）
    conn.execute("""UPDATE postings SET extra=json_set(json_remove(extra, '$.easy_apply'), '$.quick_apply',
                    json(CASE WHEN json_extract(extra, '$.easy_apply') THEN 'true' ELSE 'false' END))
                    WHERE extra IS NOT NULL AND json_valid(extra) AND json_type(extra, '$.easy_apply') IS NOT NULL""")
    conn.commit()
    return conn
