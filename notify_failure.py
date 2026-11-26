"""定时任务失败时由 systemd 调用（OnFailure=auto-job-failed@%n.service）：推一条到手机，附上最后一行报错。

run.py 一启动就崩溃时（比如缺 import），它自己"出错就推送"的逻辑走不到，靠这个兜底。

  python3 notify_failure.py auto-job.service
"""
import re, subprocess, sys, time
import push
from db import connect

unit = sys.argv[1] if len(sys.argv) > 1 else "auto-job.service"
conn = connect()
# 每分钟的邮件检查如果一直崩，不能每分钟推一条：同一个任务每小时最多推一次
key = f"failure_push:{unit}"
r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
if r and time.time() - float(r[0]) < 3600:
    sys.exit(0)
log = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "40", "--no-pager", "-o", "cat"],
                     capture_output=True, text=True).stdout.splitlines()
last = next((l.strip() for l in reversed(log) if re.search(r"Error|Exception|出错|失败|限流", l)), None)
ok, _ = push.send_all(conn, f"抓取崩溃：{unit}", last or f"运行 journalctl --user -u {unit} 查看", urgency="high")
if ok:   # 至少一台手机收到了才开始一小时冷却；没送达的话下次失败还会再试
    conn.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, str(time.time())))
    conn.commit()
