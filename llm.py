"""Gemini：规则判断不了的求职邮件才交给它分类，带月度预算上限。

- 模型：gemini-flash-lite-latest（最便宜的 lite 档，自动跟随最新版本）
- 预算：每次调用按返回的 token 数累计到 llm_usage 表，当月估算花费超过 MONTHLY_BUDGET 就停止调用（邮件留在"未归类"）。
  单价按偏高的估计算（宁可早停，也不要超支）。GCP 自带的预算告警只发邮件、不会真的停止扣费，所以上限做在这里。
- key：GEMINI_API_KEY 环境变量；没有就从 ~/.bashrc 或项目 .env 里读。
"""
import json, os, re, urllib.error, urllib.request
from datetime import datetime

MODEL = os.environ.get("AUTO_JOB_GEMINI_MODEL", "gemini-flash-lite-latest")
MONTHLY_BUDGET = float(os.environ.get("AUTO_JOB_GEMINI_BUDGET", "8"))   # 美元
PRICE_IN, PRICE_OUT = 0.50 / 1e6, 3.00 / 1e6                           # 每 token，偏高估计
from db import EMAIL_STATUSES


def _key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), os.path.expanduser("~/.bashrc")):
        try:
            m = re.search(r'^\s*(?:export\s+)?GEMINI_API_KEY=["\']?([^"\'\s]+)', open(p).read(), re.M)
            if m:
                return m.group(1)
        except OSError:
            pass
    return None


def month_cost(conn):
    r = conn.execute("SELECT cost FROM llm_usage WHERE month=?", (datetime.now().strftime("%Y-%m"),)).fetchone()
    return r[0] if r else 0.0


def _record(conn, tin, tout):
    cost = tin * PRICE_IN + tout * PRICE_OUT
    conn.execute("""INSERT INTO llm_usage (month, calls, tokens_in, tokens_out, cost) VALUES (?,1,?,?,?)
                    ON CONFLICT (month) DO UPDATE SET calls=calls+1, tokens_in=tokens_in+excluded.tokens_in,
                    tokens_out=tokens_out+excluded.tokens_out, cost=cost+excluded.cost""",
                 (datetime.now().strftime("%Y-%m"), tin, tout, cost))
    conn.commit()


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "job_related": {"type": "BOOLEAN", "description": "是否是关于某次具体求职申请的邮件（职位推荐、营销、newsletter 都算 false）"},
        "status": {"type": "STRING", "enum": EMAIL_STATUSES, "description": "这封邮件说明这次申请处于什么状态"},
        "company": {"type": "STRING", "description": "招聘公司名，没有就空字符串"},
        "title": {"type": "STRING", "description": "职位名，没有就空字符串"},
    },
    "required": ["job_related", "status", "company", "title"],
}
PROMPT = """You classify emails a job seeker received about their job applications.
For a job-related email, return the status of that application it shows:
applied = the application was received, or the employer viewed it, or anything else that only shows the person applied
(e.g. asking for more information); assessment = online test, coding challenge or take-home task;
interview = invitation to interview / phone screen / scheduling; offer = job offer;
rejected = not progressing / unsuccessful; closed = the job ad closed or expired.
Job alerts, recommendations, newsletters and marketing are job_related=false.
Return company and title exactly as written in the email.

From: {sender}
Subject: {subject}

{body}"""


def classify_email(conn, sender, subject, body):
    """返回 dict(job_related, status, company, title)；超预算 / 没 key / 出错返回 None。"""
    key = _key()
    if not key or month_cost(conn) >= MONTHLY_BUDGET:
        return None
    req = {"contents": [{"parts": [{"text": PROMPT.format(sender=sender, subject=subject, body=(body or "")[:3000])}]}],
           "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0}}
    r = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                               data=json.dumps(req).encode(), headers={"content-type": "application/json", "x-goog-api-key": key})
    try:
        d = json.load(urllib.request.urlopen(r, timeout=30))
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[gemini] {e}")
        return None
    u = d.get("usageMetadata", {})
    _record(conn, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0))
    try:
        out = json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, ValueError):
        return None
    if out.get("status") not in EMAIL_STATUSES:
        out["status"] = None
    return out
