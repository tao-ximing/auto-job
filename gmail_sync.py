"""Gmail 同步：拉新邮件 → 预筛（只留求职相关）→ 规则分类（不行再问 Gemini）→ 匹配职位 → 推进状态、写时间线。

- 第一次只拉当天的邮件，之后按 Gmail historyId 增量。
- 不相关的邮件不存、不发给 Gemini。
- 状态只往前推：确认邮件把 新 / 想投 改成 已投；拒信改成 被拒；笔试 / 面试 / offer 同理。被拒和 offer 不会被覆盖。
- 匹配不上的求职邮件留在"未归类"，网页上手动关联或忽略。

  python3 gmail_sync.py          # 同步一次
"""
import base64, html, re, sys, time, urllib.error
from datetime import datetime, timezone
import gmail, llm, rules
from db import STATUS_LABEL, connect, next_status, now

NOTABLE = {"rejected", "assessment", "interview", "offer"}   # 邮件说明的状态是这几个时推送到手机

# ── 预筛：只看发件人和标题 ───────────────────────────────────────────────────
IGNORE_FROM = re.compile(r"@(email\.seek\.com\.au|em\.linkedin\.com|e\.linkedin\.com|news\.|marketing\.)", re.I)
IGNORE_SUBJECT = re.compile(
    r"is hiring|\bjob at\b.*(actively|recruiting)|see who else|thanks for being|don.?t forget to complete|complete your application|"
    r"still accepting applications|verify your account|jobs? (for|you might|recommended)|new jobs?|job alert|what could you be earning|"
    r"higher salary|stand out on jobs|people (are|who) |profile views|weekly digest|newsletter|webinar|i want to connect|"
    r"accepted your invitation|you have an invitation|viewed your profile|profile is being discovered|apply now to|"
    r"jobs you may be interested|new activity in jobs|incomplete (job )?application"
    + "".join("|" + p for p in __import__("tomllib").load(open(rules.config_path("filters.toml"), "rb"))
              .get("gmail", {}).get("extra_ignore_subject", [])), re.I)
JOB_FROM = re.compile(
    r"@(s\.seek\.com\.au|seek\.com\.au|linkedin\.com|.*myworkday(jobs)?\.com|.*greenhouse(-mail)?\.io|.*lever\.co|.*smartrecruiters\.com|"
    r".*ashbyhq\.com|.*workable(mail)?\.com|.*bamboohr\.com|.*teamtailor(-mail)?\.com|.*pageuppeople\.com|.*successfactors\.(com|eu)|"
    r".*icims\.com|.*jobvite\.com|.*recruitee\.com|.*livehire\.com|.*jobadder\.com|.*employmenthero\.com|.*applytojob\.com|.*hire\.lever\.co)", re.I)
JOB_SUBJECT = re.compile(r"applica|applying|applied|candida|interview|assessment|coding (test|challenge)|position|\brole\b|"
                         r"opportunit|unfortunately|outcome|offer|recruit|shortlist|next steps|thank you for your interest", re.I)


# 招聘方本人从自己邮箱发的信（面试后的拒信、约面试），标题常常只是职位名（"Senior Engineer - Acme"），
# 发件人也不是招聘系统，只看发件人 + 标题会漏掉。这类"真人来信"再看一眼正文。
NOREPLY = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer|bounce", re.I)
JOB_BODY = re.compile(r"your application|\bapplied\b|interview|\bthe (\w+ )?(role|position)\b|candida|recruit|hiring (process|team|manager)|"
                      r"moving forward|next steps|opportunit|offer", re.I)


def is_personal(sender, headers, labels=()):
    """像真人来信：Gmail 把它放在"主要"标签页（CATEGORY_PERSONAL），或者没有群发 / 自动邮件的特征。
    只看退订头不够：招聘方用的外联工具会给本人写的信也加 List-Unsubscribe。"""
    if NOREPLY.search(sender):
        return False
    if "CATEGORY_PERSONAL" in labels:
        return True
    h = {k.lower(): v for k, v in headers.items()}
    if "list-unsubscribe" in h or "list-id" in h or re.search(r"bulk|list|junk", h.get("precedence", ""), re.I):
        return False
    return h.get("auto-submitted", "no").lower() == "no"


def is_candidate(sender, subject, headers=None, labels=()):
    """True / False；"body" 表示标题和发件人看不出来，但像真人来信，要看正文再定。"""
    if IGNORE_FROM.search(sender) or IGNORE_SUBJECT.search(subject):
        return False
    if "linkedin.com" in sender.lower() and "jobs-noreply@linkedin.com" not in sender.lower():
        return False  # LinkedIn 只有 jobs-noreply 发的是申请相关；其余是职位推荐、邀请、InMail、动态
    if JOB_FROM.search(sender) or JOB_SUBJECT.search(subject):
        return True
    return "body" if headers is not None and is_personal(sender, headers, labels) else False


# ── 正文 ────────────────────────────────────────────────────────────────
def _part(p, mime):
    if p.get("mimeType") == mime and p.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
    for c in p.get("parts") or []:
        if (t := _part(c, mime)):
            return t
    return None


def body_text(msg):
    plain = _part(msg["payload"], "text/plain") or ""
    h = _part(msg["payload"], "text/html") or ""
    h = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script|head).*?</\1>", " ", h)))
    t = max((plain, h), key=lambda s: len(" ".join(s.split())))   # LinkedIn 有的纯文本几乎是空的，取更长的那份
    t = re.sub(r"https?://\S+|%%\w+%%", " ", t)
    return " ".join(t.split())[:4000]


# ── 规则分类 ─────────────────────────────────────────────────────────────
# 拒信措辞要具体：确认邮件里常有 "unsuccessful applicants will be notified" 之类的话，不能用单个 "unsuccessful" 判断
REJECT = re.compile(r"unlikely to progress|not (to )?progress(ing)? (with )?your application|decided not to (progress|proceed|move forward)|"
                    r"will not be (progressing|moving forward|proceeding) with your|won.?t be (progressing|moving forward)|"
                    r"(have|has) (been )?unsuccessful|were unsuccessful|application (was|has been|is) unsuccessful|regret to (inform|advise)|"
                    r"(move|moving|proceed|proceeding) forward with other candidates|candidates whose (experience|skills|background)|"
                    r"position has been filled|not been shortlisted|not (been )?selected (for|to)", re.I)
OFFER = re.compile(r"pleased to offer|offer of employment|letter of offer|employment offer|we.?d like to offer you", re.I)
INTERVIEW = re.compile(r"invit\w* (you )?to (an |a )?(interview|chat|call|meet)|schedule (an |a |your )?(interview|call|chat)|"
                       r"interview (invitation|request)|phone screen|book (a|your) (time|slot|interview)|your availability", re.I)
ASSESS = re.compile(r"assessment|coding (test|challenge|exercise)|take.?home|hackerrank|codility|codesignal|testgorilla|online test|"
                    r"technical (test|exercise|task)", re.I)
APPLIED = re.compile(r"successfully submitted|application (was )?sent|received your application|application (has been )?received|"
                     r"thank(s| you) for (your |submitting an )?appl(ying|ication)|we('ve)? got your application|acknowledg|"
                     r"application confirmation|confirm\w* (receipt|we.ve received)", re.I)
SUBJ_REJECT = re.compile(r"unsuccessful|not successful|regret", re.I)
SUBJ_INTERVIEW = re.compile(r"interview (invitation|request)|invitation to (an )?interview|invit\w* .{0,20}interview", re.I)
SUBJ_ASSESS = re.compile(r"assessment|coding (test|challenge)|hackerrank|codility|codesignal|testgorilla", re.I)
SUBJ_OFFER = re.compile(r"letter of offer|offer of employment|job offer|employment offer", re.I)
SUBJ_UNSURE = re.compile(r"update|outcome|next step|responded|status|one more step", re.I)   # 标题说明不了结果，要看正文 / 问 Gemini

TEMPLATES = [  # (邮件说明的状态 或 None=看正文, 搜 subject/body, 正则)。"对方已查看"说明投过了，算 applied
    ("applied", "body", r"your application for (?P<title>.+?) was successfully submitted to (?P<company>[^.\[]+?)(?=\s+seek\b|\.|\s\[|\s+Each|$)"),
    ("rejected", "body", r"your application for (?P<title>.+?) advertised by (?P<company>.+?) is unlikely to progress"),
    ("applied", "subject", r"^(?P<company>.+?) has viewed your application for (?P<title>.+)$"),
    ("applied", "subject", r"your application was viewed by (?P<company>.+)$"),
    ("closed", "subject", r"the (?P<title>.+?) job with (?P<company>.+?) has closed"),
    ("applied", "subject", r"your application was sent to (?P<company>.+)$"),
    (None, "subject", r"^your application to (?P<title>.+?) at (?P<company>.+)$"),
    (None, "subject", r"application update for (?P<title>.+?) at (?P<company>.+)$"),
    (None, "subject", r"your application for (?:the )?(?P<title>.+?) (?:role|position) (?:with|at) (?P<company>.+?)$"),
    (None, "body", r"application for (?:the |our )?(?P<title>[^.,]{3,80}?) (?:role|position|job) (?:with|at) (?P<company>[A-Z][^.,]{1,60}?)[.,]"),
    (None, "subject", r"(?:application|applying) (?:for|to) (?P<title>.+?) (?:at|with) (?P<company>.+?)$"),
]


def classify_rules(sender, subject, body):
    """返回 (邮件说明的状态 或 None, company, title, confident)。confident=False 时交给 Gemini 确认。"""
    status, company, title, confident = None, None, None, False
    for cat, where, pat in TEMPLATES:
        m = re.search(pat, subject if where == "subject" else body, re.I)
        if m:
            status, confident = cat, cat is not None
            company = m.groupdict().get("company")
            title = m.groupdict().get("title")
            break
    if company and not title and "linkedin" in sender.lower():   # LinkedIn "sent to X"：正文开头是 "职位 公司 地点"
        m = re.search(r"Your application was sent to " + re.escape(company) + r"\s+(?P<title>.+?)\s+" + re.escape(company), body)
        title = m.group("title") if m else None
    if status is None:
        unsure = bool(SUBJ_UNSURE.search(subject))
        for cat, rx in (("rejected", SUBJ_REJECT), ("offer", SUBJ_OFFER), ("interview", SUBJ_INTERVIEW), ("assessment", SUBJ_ASSESS)):
            if rx.search(subject):
                status, confident = cat, True
                break
        if status is None and REJECT.search(body):            # 具体的拒信措辞：可信
            status, confident = "rejected", True
        if status is None and APPLIED.search(subject) and not unsure:
            status, confident = "applied", True
        if status is None:                                    # 只在正文里出现的线索：先记下，交给 Gemini 确认
            text = f"{subject}\n{body}"
            for cat, rx in (("offer", OFFER), ("interview", INTERVIEW), ("assessment", ASSESS), ("applied", APPLIED)):
                if rx.search(text):
                    status = cat
                    break
    clean = lambda s: (s or "").strip(" .,-–|:").strip() or None
    return status, clean(company), clean(title), confident


# ── 匹配职位 ─────────────────────────────────────────────────────────────
def _co_match(a, b):
    """规范化后的公司名：相等，或一个是另一个的完整词前缀（"commbank" vs "commbank group"）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and (long_ + " ").startswith(short + " ")


def match_job(conn, company, title, status):
    cn, tn = rules.norm_company(company), rules.norm_title(title)
    if not cn:
        return None, "邮件里没找到公司名"
    cands = {}
    for r in conn.execute("SELECT j.id, j.status, p.company, p.title FROM jobs j JOIN postings p ON p.job_id=j.id"):
        if _co_match(cn, rules.norm_company(r["company"])):
            tj = rules.jaccard(set(tn.split()), set(rules.norm_title(r["title"]).split())) if tn else 0
            cands[r["id"]] = max(cands.get(r["id"], (0, r["status"]))[0], tj), r["status"]
    if not cands:
        return None, f"库里没有 {company} 的职位"
    # 结果类邮件优先匹配已投的，确认类优先匹配还没投的
    prefer = {"applied": ("new", "shortlisted", "skipped", "closed", "applied")}.get(
        status, ("applied", "assessment", "interview", "offer", "rejected"))
    ranked = sorted(cands.items(), key=lambda kv: (kv[1][0], kv[1][1] in prefer), reverse=True)
    best_id, (best_t, _) = ranked[0]
    if len(cands) == 1 and (best_t >= 0.3 or not tn):
        return best_id, f"公司 {company} 只有这一个职位"
    if best_t >= 0.6 and (len(ranked) == 1 or ranked[1][1][0] < best_t):
        return best_id, f"公司 + 职位名匹配（{best_t:.2f}）"
    in_pref = [k for k, v in cands.items() if v[1] in prefer]
    if not tn and len(in_pref) == 1:
        return in_pref[0], f"{company} 只有一个处于相应状态的职位"
    return None, f"{company} 有 {len(cands)} 个职位，判断不了是哪个"


# ── 推进状态 ─────────────────────────────────────────────────────────────
def apply_email(conn, email_id, job_id, status, subject, source="email", at=None):
    """把邮件挂到职位上，按邮件说明的状态改职位状态（规则见 db.next_status）。返回新状态或 None。
    status：邮件说明的状态，None = 没认出来（只挂上，不改状态）。at：事件时间（默认现在；同步时传收信时间）。"""
    t = at or now()
    job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return None
    new = next_status(job["status"], status)
    conn.execute("UPDATE emails SET job_id=?, state='matched' WHERE id=?", (job_id, email_id))
    conn.execute("INSERT INTO events (job_id, at, status, source, detail) VALUES (?,?,?,?,?)",
                 (job_id, t, new, source, f"邮件 · {STATUS_LABEL.get(status, '未识别')}：{subject}"))
    if new:
        conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (new, t, job_id))
    return new


# ── 拉取 ────────────────────────────────────────────────────────────────
def _kv(conn, key, value=None):
    if value is None:
        r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
    conn.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _list_query(q):
    ids, page = [], None
    while True:
        r = gmail.get("messages", q=q, maxResults=500, **({"pageToken": page} if page else {}))
        ids += [m["id"] for m in r.get("messages", [])]
        page = r.get("nextPageToken")
        if not page:
            return ids


def new_message_ids(conn):
    """返回 (ids, 新的 historyId)。第一次只拉当天。"""
    hist = _kv(conn, "gmail_history_id")
    if not hist:
        hid = gmail.get("profile")["historyId"]
        midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return _list_query(f"after:{midnight}"), hid
    ids, page, latest = [], None, hist
    try:
        while True:
            r = gmail.get("history", startHistoryId=hist, historyTypes="messageAdded", **({"pageToken": page} if page else {}))
            latest = r.get("historyId", latest)
            for h in r.get("history", []):
                ids += [m["message"]["id"] for m in h.get("messagesAdded", [])]
            page = r.get("nextPageToken")
            if not page:
                break
    except urllib.error.HTTPError as e:  # 只有 historyId 太旧（404）才退回按时间拉；超时、限流等直接抛出，游标不动，下次重试
        if e.code != 404:
            raise
        hid = gmail.get("profile")["historyId"]   # 先取游标再列表：两步之间到的信下次还会出现在 history 里
        last = _kv(conn, "gmail_last_ok")
        days = 2 if not last else max(2, int((time.time() - float(last)) / 86400) + 2)
        print(f"[gmail] history 失效，改拉最近 {days} 天", file=sys.stderr)
        return _list_query(f"newer_than:{days}d"), hid
    return list(dict.fromkeys(ids)), latest


def process_message(conn, mid, stats, verbose=True):
    """处理一封邮件：预筛 → 分类 → 存库 → 匹配职位 → 推进状态。"""
    try:
        meta = gmail.get(f"messages/{mid}", format="metadata",
                         metadataHeaders=["From", "Subject", "List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return  # 已删除的邮件
        raise       # 超时 / 限流 / 授权问题：抛出去，这轮不推进 historyId，下次重试
    if "SENT" in meta.get("labelIds", []) or "DRAFT" in meta.get("labelIds", []):
        return
    h = {x["name"]: x["value"] for x in meta["payload"]["headers"]}
    sender, subject = h.get("From", ""), h.get("Subject", "")
    cand = is_candidate(sender, subject, h, meta.get("labelIds", []))
    if not cand:
        return
    msg = gmail.get(f"messages/{mid}", format="full")
    body = body_text(msg)
    if cand == "body" and not JOB_BODY.search(body):
        return
    status, company, title, confident = classify_rules(sender, subject, body)
    by, ignore = "rule", False
    if not confident or not company:
        g = llm.classify_email(conn, sender, subject, body)
        if g is not None:
            by = "gemini"
            if not g.get("job_related"):
                ignore = True
            else:
                status = status if confident else g["status"]
                company = company or g.get("company") or None
                title = title or g.get("title") or None
    name, addr = (re.match(r'\s*"?([^"<]*?)"?\s*<([^>]+)>', sender) or re.match(r"()(.*)", sender)).groups()
    received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000).astimezone().isoformat(timespec="seconds")
    received_utc = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = "ignored" if ignore else "unmatched"
    if state == "ignored":   # 和求职无关：只留 ID 防止重复处理，正文、摘要、发件人都不存
        name = addr = subject = body = None
        msg = {**msg, "snippet": ""}
    conn.execute("""INSERT OR IGNORE INTO emails (id, thread_id, received_at, from_addr, from_name, subject, snippet, body, status,
                    company, title, state, classified_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (mid, msg["threadId"], received, addr, name, subject, html.unescape(msg.get("snippet", "")), body,
                  None if ignore else status, company, title, state, by, now()))
    stats["stored"] += 1
    if state == "ignored":
        conn.commit()   # 跳过前也要提交，否则后面请求 Gmail / Gemini 时一直占着写锁
        return
    job_id, reason = match_job(conn, company, title, status)
    conn.execute("UPDATE emails SET match_reason=? WHERE id=?", (reason, mid))
    if job_id:
        apply_email(conn, mid, job_id, status, subject, at=received_utc)
        stats["matched"] += 1
    else:
        stats["unmatched"] += 1
    if status in NOTABLE:
        stats["notable"].append((status, subject, job_id))
    if verbose:
        print(f"[gmail] {STATUS_LABEL.get(status, '未识别')} | {company or '?'} | {title or '?'} | {'→ #%s' % job_id if job_id else '未归类：' + reason}")
    conn.commit()


def sync(conn, verbose=True):
    """返回 dict(seen, stored, matched, unmatched, notable=[(status, subject, job_id)])。"""
    ids, latest = new_message_ids(conn)
    known = {r[0] for r in conn.execute("SELECT id FROM emails")}
    stats = {"seen": 0, "stored": 0, "matched": 0, "unmatched": 0, "notable": []}
    for mid in ids:
        if mid in known:
            continue
        stats["seen"] += 1
        process_message(conn, mid, stats, verbose)
    _kv(conn, "gmail_history_id", latest)
    _kv(conn, "gmail_last_ok", time.time())
    conn.commit()
    return stats


if __name__ == "__main__":
    c = connect()
    if sys.argv[1:2] == ["--reprocess"]:   # python3 gmail_sync.py --reprocess "Gmail 搜索语句"：重新处理漏掉的邮件
        st = {"seen": 0, "stored": 0, "matched": 0, "unmatched": 0, "notable": []}
        for mid in _list_query(sys.argv[2]):
            if not c.execute("SELECT 1 FROM emails WHERE id=?", (mid,)).fetchone():
                st["seen"] += 1
                process_message(c, mid, st)
        print(st)
        raise SystemExit
    s = sync(c)
    print(f"新邮件 {s['seen']}，求职相关 {s['stored']}，匹配 {s['matched']}，未归类 {s['unmatched']}；Gemini 本月约 ${llm.month_cost(c):.4f}")
