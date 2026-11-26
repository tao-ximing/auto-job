"""跑一遍所有搜索并写进 jobs.db。

用法:
  python3 run.py                    # SEEK + LinkedIn，SEEK 看最近 3 天，LinkedIn 看最近 24 小时
  python3 run.py --only seek
  python3 run.py --seek-days 7 --linkedin-window week --linkedin-max-new 40

每个平台一条 runs 记录。抓到 0 条也算异常（多半是登录过期或页面改版），网页顶部会显示警告。
"""
import argparse, json, os, subprocess, sys, tempfile, tomllib, traceback
import linkedin_guest, rules, seek
from db import connect, now
from ingest import Ingestor

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = {"24h": 86400, "week": 604800, "month": 2592000}


def start_run(conn, source):
    rid = conn.execute("INSERT INTO runs (source, started_at) VALUES (?,?)", (source, now())).lastrowid
    conn.commit()
    return rid


def finish_run(conn, rid, seen, new, error=None, zero_ok=False):
    if not error and seen == 0 and not zero_ok:
        error = "所有搜索都返回 0 条，可能是登录过期或页面改版"
    conn.execute("UPDATE runs SET finished_at=?, seen=?, new=?, error=? WHERE id=?", (now(), seen, new, error, rid))
    conn.commit()


def run_seek(conn, ing, conf, days):
    rid, seen, new, err = start_run(conn, "seek"), 0, 0, None
    known = ing.known_ids("seek")
    try:
        for s in conf["seek"]:
            n0 = new
            for p in seek.search(s["name"], s["params"], days=days, known=known):
                r = ing.add(p)
                conn.commit()   # 每条都提交：拉 JD 很慢，攒着不提交会一直占着写锁，网页上点按钮会报 database is locked
                seen += 1
                if r == "new":
                    new += 1
                    known.add(str(p["source_id"]))
            print(f"[seek] {s['name']}: 新 {new - n0}", file=sys.stderr)
    except Exception:
        err = traceback.format_exc(limit=3)
        print(err, file=sys.stderr)
    finish_run(conn, rid, seen, new, err)
    return seen, new


SEEK_LINKS_PER_RUN = 20


def resolve_seek_links(conn):
    """SEEK 外部投递职位的官网链接（seek_links.mjs，要登录）。只查规则放行、待处理、还没查过的；每个只查一次。
    出错只打日志，不算抓取出错（按钮退回打开 SEEK 职位页）。"""
    rows = conn.execute("""SELECT p.id, p.source_id, p.extra FROM postings p JOIN jobs j ON j.id=p.job_id
        WHERE p.source='seek' AND j.filtered=0 AND j.status IN ('new','shortlisted')
          AND json_extract(p.extra, '$.quick_apply') = 0
          AND json_extract(p.extra, '$.apply_url') IS NULL AND json_extract(p.extra, '$.apply_url_tried') IS NULL
        ORDER BY p.id DESC LIMIT ?""", (SEEK_LINKS_PER_RUN,)).fetchall()
    if not rows:
        return 0
    try:
        p = subprocess.run(["node", os.path.join(HERE, "seek_links.mjs"), *[r["source_id"] for r in rows]],
                           capture_output=True, text=True, cwd=HERE, timeout=60 + 40 * len(rows))
        if p.returncode != 0:
            print(f"[seek-links] 退出码 {p.returncode}：{p.stderr.strip()[-300:]}", file=sys.stderr)
            return 0
        found = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"[seek-links] {e}", file=sys.stderr)
        return 0
    n = 0
    for r in rows:
        if r["source_id"] not in found:
            continue
        ex = json.loads(r["extra"] or "{}") | {"apply_url_tried": now()}
        if found[r["source_id"]]:
            ex["apply_url"] = found[r["source_id"]]; n += 1
        conn.execute("UPDATE postings SET extra=? WHERE id=?", (json.dumps(ex, ensure_ascii=False), r["id"]))
    conn.commit()
    return n


def run_linkedin(conn, ing, conf, window, max_pages, max_new):
    rid, seen, new, err = start_run(conn, "linkedin"), 0, 0, None
    cfg = {"searches": conf["linkedin"], "known": sorted(ing.known_ids("linkedin")),
           "tpr": WINDOWS[window], "maxPages": max_pages, "maxNew": max_new}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg, f)
    try:
        proc = subprocess.Popen(["node", os.path.join(HERE, "linkedin.mjs"), f.name], stdout=subprocess.PIPE, text=True, cwd=HERE)
        for line in proc.stdout:
            if not line.strip():
                continue
            r = ing.add(json.loads(line))
            conn.commit()   # 每条都提交（同上，登录模式约 25 秒才出一条）
            seen += 1
            new += r == "new"
        conn.commit()
        code = proc.wait()
        if code == 3:
            # 登录失效：改用不登录模式把这一轮抓完，同时在网页上标红提醒重新登录
            print("[linkedin] 登录模式不可用，改用不登录模式", file=sys.stderr)
            s2, n2, gerr = run_linkedin_guest(conn, ing, conf, cfg)
            seen, new = seen + s2, new + n2
            err = ("登录模式不可用（登录失效，或还没登录桌面），本次用不登录模式抓取" + (f"（{gerr}）" if gerr else "")
                   + "。登录失效的话运行 node browser.mjs login")
        elif code != 0:
            err = f"linkedin.mjs 退出码 {code}"
    except Exception:
        err = traceback.format_exc(limit=3)
    finally:
        os.unlink(f.name)
    if err:
        print(err, file=sys.stderr)
    finish_run(conn, rid, seen, new, err)
    return seen, new


def run_linkedin_guest(conn, ing, conf, cfg):
    seen = new = 0
    known = set(cfg["known"])
    try:
        for s in conf["linkedin"]:
            for p in linkedin_guest.search(s, known=known, tpr=cfg["tpr"], max_new=cfg["maxNew"]):
                r = ing.add(p)
                conn.commit()
                seen += 1
                if r == "new":
                    new += 1
                    known.add(p["source_id"])
            print(f"[linkedin-guest] {s['name']}: 累计新增 {new}", file=sys.stderr)
    except linkedin_guest.RateLimited as e:
        conn.commit()
        return seen, new, f"被限流 {e}，提前停止"
    except Exception as e:
        conn.commit()
        return seen, new, f"出错: {e}"
    return seen, new, None


GMAIL_LOCK = os.path.join(HERE, ".gmail.lock")


def gmail_lock(wait=True):
    """每分钟的邮件检查和每小时的整轮都会同步 Gmail，用文件锁避免同时处理同一封邮件。拿不到锁（wait=False）返回 None。"""
    import fcntl
    f = open(GMAIL_LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        return None
    return f   # 进程退出或 close 时释放


def run_gmail(conn):
    """同步 Gmail（没授权就跳过）。返回 sync 的统计或 None。"""
    if not os.path.exists(os.path.join(HERE, "token.json")):
        return None
    import gmail_sync
    lock = gmail_lock()
    rid, stats, err = start_run(conn, "gmail"), None, None
    try:
        stats = gmail_sync.sync(conn)
        # 匹配不上的邮件：去 SEEK / LinkedIn 搜这个职位，找到就入库并关联
        import email_lookup
        lk = email_lookup.run(conn, Ingestor(conn))
        stats.update(lookup_tried=lk["tried"], lookup_matched=lk["matched"])
        stats["unmatched"] = max(stats.get("unmatched", 0) - lk["matched"], 0)
    except Exception:
        err = traceback.format_exc(limit=3)
        print(err, file=sys.stderr)
    finish_run(conn, rid, (stats or {}).get("seen", 0), (stats or {}).get("stored", 0), err, zero_ok=True)
    lock.close()
    return stats


def gmail_poll():
    """每分钟跑一次（auto-job-mail.timer）：先用 historyId 看有没有新邮件，没有就安静退出（不记 runs）；有才完整同步 + 推送。"""
    if not os.path.exists(os.path.join(HERE, "token.json")):
        return
    import gmail_sync
    lock = gmail_lock(wait=False)
    if lock is None:          # 每小时那一轮正在同步
        return
    conn = connect()
    ids, latest = gmail_sync.new_message_ids(conn)
    known = {r[0] for r in conn.execute("SELECT id FROM emails")}
    if not [i for i in ids if i not in known]:   # 没有新邮件：游标也往前挪，免得每分钟重扫同一段历史
        gmail_sync._kv(conn, "gmail_history_id", latest); conn.commit()
        lock.close()
        return
    lock.close()              # run_gmail 会自己再拿锁
    t0 = now()
    mail = run_gmail(conn)
    if mail:
        print("Gmail: 新邮件 %d 封，求职相关 %d，匹配 %d，未归类 %d" % (mail["seen"], mail["stored"], mail["matched"], mail["unmatched"]))
        notify_mail(conn, mail)
        new = conn.execute("SELECT count(*) FROM jobs WHERE created_at >= ? AND filtered=0", (t0,)).fetchone()[0]
        if new:   # 邮件补抓带进来的新职位
            notify_phone(conn, t0, new, [])


def notify_mail(conn, stats):
    """拒信 / 笔试 / 面试 / offer 推到手机。"""
    import gmail_sync, push
    items = (stats or {}).get("notable") or []
    if not items:
        return
    lines = []
    for cat, subject, job_id in items[:4]:
        who = ""
        if job_id:
            j = conn.execute("SELECT title, company FROM jobs WHERE id=?", (job_id,)).fetchone()
            who = f"{j['title']} @ {j['company']}" if j else ""
        lines.append(f"{gmail_sync.LABEL[cat]}：{who or subject}")
    title = {"offer": "收到 Offer 🎉", "interview": "面试邀请", "assessment": "笔试/测评"}.get(items[0][0], "申请有更新")
    url = f"/?job={items[0][2]}" if len(items) == 1 and items[0][2] else "/"
    try:
        push.send_all(conn, title, "\n".join(lines), url, urgency="high")
    except Exception as e:
        print(f"[push] {e}", file=sys.stderr)


HIGH_SCORE = 60   # 推送标题里"几个值得看"的分数线


def notify_phone(conn, t0, new, errors):
    """推到手机（Web Push）：有新职位列出前 3 个，点通知打开第一个；出错单独推一条。"""
    import push
    try:
        if new:
            # 按匹配分从高到低（没打出分的排最前，不埋掉）
            from score import RANK_SQL
            rows = conn.execute(f"""SELECT j.id, j.title, j.company, j.llm_score FROM jobs j WHERE j.created_at >= ? AND j.filtered=0
                                    ORDER BY {RANK_SQL} DESC NULLS FIRST, j.id DESC""", (t0,)).fetchall()
            hi = sum(1 for r in rows if r["llm_score"] is None or r["llm_score"] >= HIGH_SCORE)
            lines = [(f"[{r['llm_score']}] " if r["llm_score"] is not None else "") + f"{r['title']} @ {r['company']}" for r in rows[:3]] \
                + ([f"…还有 {len(rows) - 3} 个"] if len(rows) > 3 else [])
            url = f"/?job={rows[0]['id']}" if len(rows) == 1 else "/"
            title = f"{new} 个新职位" + (f"（{hi} 个 {HIGH_SCORE} 分以上）" if hi < len(rows) else "")
            push.send_all(conn, title, "\n".join(lines), url, urgency="high")
        if errors:
            push.send_all(conn, "抓取出错", "\n".join(errors))
    except Exception as e:
        print(f"[push] {e}", file=sys.stderr)


def linkedin_due(conn, min_interval):
    """距离上次 LinkedIn 抓取不到 min_interval 分钟就跳过（防止手动连跑；定时器每小时触发，LinkedIn 每次都跑）。"""
    if not min_interval:
        return True
    r = conn.execute("SELECT (julianday('now') - julianday(max(started_at))) * 1440 FROM runs WHERE source='linkedin'").fetchone()
    return r[0] is None or r[0] >= min_interval


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["seek", "linkedin", "gmail"])
    ap.add_argument("--seek-days", type=float, default=3)
    ap.add_argument("--linkedin-window", choices=list(WINDOWS), default="24h")
    ap.add_argument("--linkedin-pages", type=int, default=8, help="每条搜索最多翻几页（实际按结果数决定）")
    ap.add_argument("--linkedin-max-new", type=int, default=40, help="每条搜索最多点开多少个新职位")
    ap.add_argument("--notify", action="store_true", help="跑完弹桌面通知（定时任务用）")
    ap.add_argument("--gmail-poll", action="store_true", help="只检查有没有新邮件，有才同步（每分钟的定时任务用）")
    ap.add_argument("--linkedin-min-interval", type=float, default=0, help="距上次 LinkedIn 抓取不到这么多分钟就跳过 LinkedIn")
    a = ap.parse_args()
    if a.gmail_poll:
        return gmail_poll()
    conf = tomllib.load(open(rules.config_path("searches.toml"), "rb"))
    conn = connect()
    t0 = now()
    ing = Ingestor(conn)
    bg = None
    if a.only in (None, "seek", "linkedin"):   # 匹配分：后台边抓边打，新职位入库十几秒后就有分数
        try:
            import score
            bg = score.Background().start()
        except Exception as e:   # agy 不可用等：只是没有匹配分，不算抓取出错，下一轮再补
            print(f"[score] 跳过：{e}", file=sys.stderr)
    if a.only in (None, "seek"):
        print("SEEK: 看到 %d 条，新增 %d 条" % run_seek(conn, ing, conf, a.seek_days))
    if a.only in (None, "linkedin") and not linkedin_due(conn, a.linkedin_min_interval):
        print("LinkedIn: 距上次不到 %g 分钟，这次跳过" % a.linkedin_min_interval)
    elif a.only in (None, "linkedin"):
        print("LinkedIn: 看到 %d 条，新增 %d 条" % run_linkedin(conn, ing, conf, a.linkedin_window, a.linkedin_pages, a.linkedin_max_new))
    if a.only in (None, "seek"):   # 放在 LinkedIn 之后：两边共用一个浏览器 profile，不能同时开
        print("SEEK 官网链接：补上 %d 个" % resolve_seek_links(conn))
    if bg:   # 推送之前等后台打完：推送按匹配分挑前几个
        try:
            r = bg.finish()
            print("匹配分（agy）：打分 %d 个，失败 %d 个" % (r["scored"], r["failed"]))
        except Exception as e:
            print(f"[score] 出错：{e}", file=sys.stderr)
    mail = None
    if a.only in (None, "gmail"):   # 放在抓取之后：新抓到的职位也能被邮件匹配上
        mail = run_gmail(conn)
        if mail:
            print("Gmail: 新邮件 %d 封，求职相关 %d，匹配 %d，未归类 %d" % (mail["seen"], mail["stored"], mail["matched"], mail["unmatched"]))
    c = conn.execute("SELECT count(*) total, sum(filtered) filtered FROM jobs").fetchone()
    print(f"库里共 {c['total']} 个职位，其中被过滤 {c['filtered'] or 0} 个")
    # 只看这一轮：新增且没被过滤的职位数、这一轮的错误
    new = conn.execute("SELECT count(*) FROM jobs WHERE created_at >= ? AND filtered=0", (t0,)).fetchone()[0]
    errors = [f"{r['source']}: {r['error'].strip().splitlines()[-1]}"
              for r in conn.execute("SELECT source, error FROM runs WHERE started_at >= ? AND error IS NOT NULL", (t0,))]
    print(f"这一轮新增（未过滤）{new} 个")
    if a.notify and (new or errors):
        title = "抓取出错" if errors else f"新职位 {new} 个"
        body = "\n".join(errors) or "打开 http://127.0.0.1:8765 查看"
        subprocess.run(["notify-send", "-a", "auto job", "-u", "critical" if errors else "normal", title, body], check=False)
    if a.notify:
        notify_phone(conn, t0, new, errors)
        notify_mail(conn, mail)
    if errors:
        sys.exit(2)   # 已处理、已推送的错误；systemd 里 SuccessExitStatus=2，不触发 OnFailure（那个只管崩溃，退出码 1）


if __name__ == "__main__":
    main()
