# auto job

[English](README.md) | 中文

抓 SEEK + LinkedIn 的职位 → 过滤、去重 → SQLite（`jobs.db`）→ 网页查看、跟踪申请 → 手机推送。

- 做了什么：[DONE.md](DONE.md)
- 踩过的坑：[PITFALLS.md](PITFALLS.md)

## 访问

- 本机：http://127.0.0.1:8765
- 手机（tailnet 内，手机要开 Tailscale）：https://<主机名>.<tailnet>.ts.net:8443/
  - iPhone：Safari 打开 → 分享 → 添加到主屏幕 → 从图标打开 → 开启通知

## 自动运行

全部是 systemd user 单元，开了 Linger，开机即运行：

| 单元 | 作用 |
|---|---|
| `auto-job.timer` → `auto-job.service` | 8:00–22:00 每小时抓一次 SEEK + LinkedIn + 同步 Gmail；有新职位、申请结果或出错就通知 |
| `auto-job-mail.timer` → `auto-job-mail.service` | 全天每分钟查一次 Gmail historyId，有新邮件才同步（含邮件补抓、推送） |
| `auto-job-web.service` | 网页，127.0.0.1:8765 |
| `tailscale serve --https=8443` | 把网页发布到 tailnet |

```bash
systemctl --user list-timers auto-job.timer       # 下次什么时候跑
systemctl --user start auto-job.service           # 现在手动跑一次
journalctl --user -u auto-job.service -n 50       # 看日志
systemctl --user restart auto-job-web.service     # 改了 web.py / static 后重启网页
```

LinkedIn 登录模式的 Chrome 跑在 Xvfb 虚拟屏幕上（不弹窗口；`sudo apt install xvfb`），但需要已登录的桌面会话（keyring 解锁才能读 cookie）；登录失效或没登录桌面时会自动改用不登录的公开接口，网页顶部标红。
重新登录专用 profile：`node browser.mjs login`。

## 常用命令

```bash
python3 run.py                       # 手动抓一遍（SEEK 3 天 + LinkedIn 24 小时）
python3 ingest.py --refilter         # 改了 filters.toml 后重算过滤（保留手动状态）
python3 score.py --rescore           # 改了 ~/job-match/AGENTS.md 后给待处理的重新打分
python3 push.py test "标题" "内容"    # 给手机发一条测试推送
python3 -m unittest test_rules       # 测试过滤和去重
python3 gmail.py test                # 测试 Gmail 只读连接（重新授权：python3 gmail.py auth）
python3 run.py --only gmail          # 只同步 Gmail
```

## 文件

| 文件 | 作用 |
|---|---|
| `searches.toml` | 8 条搜索（2 职位 × 2 地区 × 2 平台；不进 git，模板见 `searches.example.toml`） |
| `filters.toml` | 过滤规则（不进 git，模板见 `filters.example.toml`） |
| `seek.py` | SEEK JSON 接口 |
| `linkedin.mjs` + `browser.mjs` | LinkedIn 登录模式（专用 Chrome profile） |
| `linkedin_guest.py` | LinkedIn 不登录模式（自动兜底） |
| `seek_links.mjs` | SEEK 外部投递职位的官网链接（登录 profile 打开投递页，记下跳转地址） |
| `rules.py` | 规范化、过滤、去重打分（纯函数） |
| `ingest.py` | 入库、过滤、去重、合并 |
| `run.py` | 跑全部搜索 + 通知 |
| `db.py` | 表结构 |
| `web.py` + `static/` | 网页 + PWA（manifest、service worker、图标） |
| `push.py` | Web Push（VAPID 私钥在 `push_keys.json`，不进 git） |
| `gmail.py` | Gmail 只读授权和读取（`credentials.json` + `token.json`，都不进 git） |
| `gmail_sync.py` | 邮件同步：预筛、分类、匹配职位、推进状态 |
| `email_lookup.py` | 邮件补抓：邮件对不上库里的职位时，去 SEEK / LinkedIn 搜出来建职位 |
| `llm.py` | Gemini 分类（带月度预算上限） |
| `score.py` | 匹配分：agy CLI 按 `~/job-match/AGENTS.md`（简历 + 规则，不进 git）打分 |
| `test_rules.py` | 测试 |
