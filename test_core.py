"""核心测试（进 git，不依赖个人配置：有本机 filters.toml 时会读它，没有时退回 filters.example.toml）。

  python3 -m unittest test_core

覆盖 ARCHITECTURE.md 联动清单里要求的"预期结果"。每个测试都要在对应的 bug 回来时失败（写测试时逐个验证过）。
"""
import os, shutil, sqlite3, tempfile, threading, time, unittest

_TMP = tempfile.mkdtemp()
os.environ["AUTO_JOB_DB"] = os.path.join(_TMP, "core.db")          # 必须在 import db 之前
os.environ["AUTO_JOB_BLOCKED"] = os.path.join(_TMP, "blocked.json")

import db, gmail_sync, score
from db import connect, next_status
from ingest import Ingestor, merge_jobs


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def post(**kw):
    p = {"source": "seek", "source_id": "1", "search": "t", "url": "https://au.seek.com/job/1", "title": "AI Engineer",
         "company": "Acme Pty Ltd", "location": "Melbourne VIC", "description": "Build LLM agents in Python. " * 20,
         "posted_at": "2026-11-27T00:00:00Z"}
    p.update(kw)
    return p


def other_post(n):
    return post(source_id=str(n), url=f"https://au.seek.com/job/{n}", company=f"Other{n} Co", title=f"Role {n}",
                description=f"Totally different job number {n}. " * 30)


class StatusRules(unittest.TestCase):
    """db.next_status 和 db.EMAIL_STATUSES：ARCHITECTURE.md 5.1 / 5.2。"""
    CASES = [  # (当前状态, 邮件说明的状态, 预期新状态；None = 不变)
        ("new", "applied", "applied"), ("skipped", "applied", "applied"), ("applied", "applied", None),
        ("applied", "assessment", "assessment"), ("assessment", "interview", "interview"),
        ("new", "closed", "closed"), ("applied", "closed", "closed"), ("interview", "closed", None),
        ("assessment", "closed", None), ("closed", "interview", "interview"), ("closed", "applied", "applied"),
        ("interview", "rejected", "rejected"), ("new", "rejected", "rejected"), ("offer", "rejected", None),
        ("rejected", "rejected", None), ("rejected", "interview", None), ("rejected", "applied", None),
        ("applied", "offer", "offer"), ("interview", "assessment", None), ("new", None, None), ("new", "viewed", None),
    ]

    def test_table(self):
        for cur, target, want in self.CASES:
            self.assertEqual(next_status(cur, target), want, (cur, target))

    def test_email_statuses(self):
        self.assertEqual(set(db.EMAIL_STATUSES), {"applied", "assessment", "interview", "offer", "rejected", "closed"})
        self.assertEqual(set(db.STATUS_LABEL), set(db.STATUSES))

    def test_apply_email_moves_status_and_links(self):
        """apply_email（自动同步、邮件补抓、网页手动关联都调用它）：状态按 next_status 变，邮件挂上，时间线记一条。"""
        c = connect(":memory:"); Ingestor(c).add(post())
        for source in ("email", "manual"):
            c.execute("UPDATE jobs SET status='applied'")
            c.execute("INSERT OR REPLACE INTO emails (id, state, created_at) VALUES ('m', 'unmatched', 't')")
            self.assertEqual(gmail_sync.apply_email(c, "m", 1, "closed", "subj", source=source), "closed")
            self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()[0], "closed")
            self.assertEqual(tuple(c.execute("SELECT job_id, state FROM emails").fetchone()), (1, "matched"))
            ev = c.execute("SELECT status, source FROM events ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(tuple(ev), ("closed", source))
            c.execute("UPDATE jobs SET status='interview'")
            self.assertIsNone(gmail_sync.apply_email(c, "m", 1, "closed", "subj", source=source))
            self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()[0], "interview")
            self.assertIsNone(c.execute("SELECT status FROM events ORDER BY id DESC LIMIT 1").fetchone()[0])


class BackgroundScorer(unittest.TestCase):
    """score.Background。不调用真的 agy。"""

    def setUp(self):
        self.c = connect()
        for t in ("search_hits", "filter_results", "postings", "events", "dup_candidates", "jobs"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()
        self._orig = score.score_one, score.check, score.Background.POLL
        score.score_one = lambda r: (r["id"], {"score": 70, "track": "ai", "reason": "r", "gaps_minor": [], "gaps_major": [],
                                               "red_flags": []}, None)
        score.check = lambda: None

    def tearDown(self):
        score.score_one, score.check, score.Background.POLL = self._orig

    def unscored(self):
        return self.c.execute("SELECT count(*) FROM jobs WHERE llm_score IS NULL").fetchone()[0]

    def test_scores_while_scraping(self):
        """抓取还没结束时进来的职位，在 finish() 之前就有分数。"""
        score.Background.POLL = 0.2
        bg = score.Background().start()
        Ingestor(self.c).add(post()); self.c.commit()
        for _ in range(50):
            if not self.unscored():
                break
            time.sleep(0.1)
        self.assertEqual(self.unscored(), 0, "抓取期间没有打分")
        self.assertEqual(bg.finish(), {"scored": 1, "failed": 0})

    def test_final_scan_after_stop(self):
        """最后一刻进来的职位（后台最后一次检查之后）：finish() 时还要再查一遍。"""
        score.Background.POLL = 3600   # 保证中间不会有常规检查
        bg = score.Background().start()
        time.sleep(0.3)                 # 第一次检查已经做完
        Ingestor(self.c).add(post()); self.c.commit()
        self.assertEqual(bg.finish(), {"scored": 1, "failed": 0})
        self.assertEqual(self.unscored(), 0)

    def test_reused_job_id_not_overwritten(self):
        """打分期间职位被合并删掉、id 被新职位复用：旧分数不能写到新职位上。"""
        ing = Ingestor(self.c)
        ing.add(post()); ing.add(other_post(2)); self.c.commit()          # job 1、2
        old = self.c.execute("SELECT * FROM jobs WHERE id=2").fetchone()   # 打分开始时读到的 job 2
        merge_jobs(self.c, 1, 2); self.c.commit()
        time.sleep(1.1)                                                     # created_at 精确到秒
        ing.add(other_post(3)); self.c.commit()
        self.assertEqual(self.c.execute("SELECT max(id) FROM jobs").fetchone()[0], 2)   # SQLite 复用了 id 2
        _, out, err = score.score_one(old)
        self.assertFalse(score._save(self.c, old, out, err))
        self.assertIsNone(self.c.execute("SELECT llm_score FROM jobs WHERE id=2").fetchone()[0])


class ConcurrentWrite(unittest.TestCase):
    """Ingestor.add：另一个连接正占着写锁时要等它释放，而不是立刻报 database is locked。"""

    def test_add_waits_for_other_writer(self):
        path = os.path.join(_TMP, "concurrent.db")
        c = connect(path); ing = Ingestor(c)
        ing.add(post()); c.commit()
        before = c.execute("SELECT last_seen FROM postings").fetchone()[0]
        other = sqlite3.connect(path, timeout=30, check_same_thread=False)
        other.execute("BEGIN IMMEDIATE"); other.execute("UPDATE kv SET value=value")   # 现在别人占着写锁
        released = threading.Event()
        threading.Thread(target=lambda: (time.sleep(1.6), released.set(), other.commit())).start()
        time.sleep(1.1)                                       # last_seen 精确到秒，保证能看出更新；此时锁还被占着
        self.assertFalse(released.is_set())
        self.assertEqual(ing.add(post()), "seen")             # 已见帖子：先读再写 last_seen。以前这里立刻抛 locked
        c.commit()
        self.assertTrue(released.is_set())                    # 确实是等对方放锁之后才写进去的
        self.assertNotEqual(c.execute("SELECT last_seen FROM postings").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
