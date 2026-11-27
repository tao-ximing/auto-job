# 项目说明（ARCHITECTURE）

> **这份文档只写项目「现在是什么样」，不写历史。** 历史、原因、放弃过的方案在 [CHANGELOG.md](CHANGELOG.md)。
>
> 怎么用：
> 1. 改代码前，读第 8 节「联动清单」里相关的那一行，以及要改的子系统那一节和第 9 节的规则。
> 2. 改完代码，同一个提交里更新这份文档（行为、表结构、接口、状态、配置、仓库外文件、新发现的规则）和 CHANGELOG.md。删掉的功能也要从这里删掉。
> 3. `python3 docs_check.py` 会把这份文档里的表、列、接口、状态、环境变量、过滤规则名和代码逐项对比，对不上就报错（提交前钩子会自动跑）。
>
> 每条描述尽量带出处（文件、函数）。实测得到的数字带日期。拿不准的写「待确认」，不当事实写。

---

## 1. 系统总览

```
            每小时 8:00–22:00                    每分钟
         auto-job.timer → run.py          auto-job-mail.timer → run.py --gmail-poll
                │                                    │
   ┌────────────┼──────────────────────┐             │ 有新邮件才往下走
   ▼            ▼                      ▼             ▼
 SEEK 接口   LinkedIn（登录 / 不登录）   后台打分线程     Gmail 同步 → 邮件补抓
 seek.py     linkedin.mjs / _guest.py   score.Background  gmail_sync.py → email_lookup.py
   │            │                      │  (agy CLI)       │
   └─── Ingestor.add()：过滤、去重 ─────┴───────┬──────────┘
                                               ▼
                                       SQLite  jobs.db（WAL）
                                               │
                    web.py（127.0.0.1:8765）◄──┴──► push.py（Web Push）→ 手机
                      ▲
          tailscale serve --https=8443（只限 tailnet）→ 手机上的 PWA（static/index.html）
```

### 1.1 进程和定时器（systemd user 单元，开了 Linger）

单元文件在仓库外：`~/.config/systemd/user/`。

| 单元 | 内容 | 关键设置 |
|---|---|---|
| `auto-job.timer` → `auto-job.service` | `run.py --notify --linkedin-min-interval 45` | 每天 08–22 点整点，`RandomizedDelaySec=8min`；`TimeoutStartSec=45min`；`SuccessExitStatus=2`；`OnFailure=auto-job-failed@%n.service` |
| `auto-job-mail.timer` → `auto-job-mail.service` | `run.py --gmail-poll` | 开机 1 分钟后起，每分钟一次；`OnFailure` 同上 |
| `auto-job-web.service` | `web.py 8765` | 常驻，`Restart=on-failure` |
| `auto-job-failed@.service` | `notify_failure.py <单元名>` | 任务崩溃时推一条到手机，同一单元 1 小时最多一次（`kv` 键 `failure_push:<unit>`），送达后才开始冷却 |
| `tailscale serve --https=8443` | 把网页发布到 tailnet | 不在 systemd 单元里，tailscale 自己保存 |

所有单元 `UMask=0077`。改了单元文件要 `systemctl --user daemon-reload`。

### 1.2 一轮抓取的执行顺序（`run.py` `main()`）

1. 启动后台打分线程 `score.Background().start()`（agy 不可用就跳过，不算出错）。
2. `run_seek`：SEEK 接口，每条帖子入库后立刻 `commit`。
3. LinkedIn：`linkedin_due` 判断距上次是否满 `--linkedin-min-interval` 分钟；`run_linkedin` 跑 `linkedin.mjs`（登录模式），退出码 3 时改跑 `run_linkedin_guest`（不登录模式）。
4. `resolve_seek_links`：给 SEEK 外部投递职位补官网链接（开浏览器）。
5. `bg.finish()`：停止找新职位，**最后再查一遍**，等已提交的打分全部做完。
6. `run_gmail`：拿 `.gmail.lock` 文件锁 → `gmail_sync.sync` → `email_lookup.run`。
7. 统计本轮（`started_at >= t0`）的新职位和错误；`--notify` 时弹桌面通知、Web Push（`notify_phone`、`notify_mail`）。
8. 本轮 `runs` 表里有 error 时退出码 2（systemd 视为成功，不触发 OnFailure）；崩溃是退出码 1，触发 OnFailure。
   **打分失败、SEEK 官网链接失败只打日志（stderr），不写 `runs`，不算出错、不推送。**

补充：
- `--only seek|linkedin|gmail` 只跑对应的步骤（`--only gmail` 不启动后台打分）。
- 没有 `token.json` 时 Gmail 两个入口都直接跳过。
- "本轮新职位"按 `jobs.created_at >= t0 AND filtered=0` 统计；"本轮错误"按 `runs.started_at >= t0` 统计。
- 只有新邮件（被拒 / 笔试 / 面试 / Offer）、没有新职位也没出错时，整轮也会推一条邮件通知（`notify_mail`）。

顺序背后的真实约束（不是"必须这个顺序"，而是这些条件不能破）：
- **专用浏览器 profile 同一时间只能被一个 Chrome 打开**：`linkedin.mjs`、`seek_links.mjs`、`node browser.mjs login` 都用它。同一个 `run.py` 里是串行的；**跨进程没有锁**（比如手动登录时定时任务正好启动，会冲突）。
- 推送按匹配分排序，所以必须在 `bg.finish()` 之后。
- Gmail 放在抓取之后，新抓到的职位才能被邮件匹配上。

### 1.3 每分钟的邮件检查（`run.py` `gmail_poll()`）

拿不到 `.gmail.lock`（整轮正在同步）就直接退出 → 用 historyId 看有没有新邮件，没有就只推进游标、不记 `runs` → 有才调用 `run_gmail` 完整同步并推送；邮件补抓带进来的新职位也会推送。
这里**不检查 `runs.error`、不推送已处理的错误**（错误只会在下一次整轮抓取时一起报；崩溃仍由 OnFailure 推送）。

---

## 2. 模块

| 文件 | 类型 | 作用 | 入口 / 被谁调用 |
|---|---|---|---|
| `run.py` | 编排 | 一轮抓取、邮件检查、推送 | systemd；手动 `python3 run.py [--only seek\|linkedin\|gmail]` |
| `seek.py` | 抓取 | SEEK 前端 JSON 接口 + GraphQL（JD、`isLinkOut`） | `run.run_seek`、`email_lookup` |
| `linkedin.mjs` + `browser.mjs` | 浏览器脚本 | LinkedIn 登录模式，stdout 每行一条帖子 JSON | `run.run_linkedin`（子进程） |
| `linkedin_guest.py` | 抓取 | LinkedIn 不登录的公开接口 | `run.run_linkedin_guest`、`email_lookup`、补数据 |
| `seek_links.mjs` | 浏览器脚本 | 登录 SEEK 打开 `/job/<id>/apply`，记下跳转到的官网地址 | `run.resolve_seek_links`（子进程） |
| `browser.mjs` | 浏览器 | 启动专用 profile 的 Chrome（Xvfb）、登录检查、`node browser.mjs login` | 上面两个 .mjs |
| `ingest.py` | 写库 | `Ingestor.add()` 入库 + 过滤 + 去重；`merge_jobs`、`refilter`、`refilter_company`、`rebuild` | 抓取、补抓、网页 |
| `rules.py` | 纯函数 + 拉黑名单文件 | 规范化、`evaluate()` 过滤、`match()` 去重打分、拉黑名单读写 | `ingest`、`web`、`gmail_sync` |
| `score.py` | 打分 | agy 打分、`Background` 后台线程、`RANK_SQL` 排序表达式 | `run.py`、`web.py`；手动 `python3 score.py [--all\|--rescore]` |
| `db.py` | 表结构 | 建表、迁移、**状态定义和 `next_status`** | 所有模块 |
| `gmail.py` | 外部接口 | Gmail 只读授权（PKCE）、请求 | `gmail_sync` |
| `gmail_sync.py` | 写库 | 拉邮件、预筛、规则分类、匹配职位、改状态 | `run.run_gmail`；手动 `--reprocess` |
| `llm.py` | 外部接口 | Gemini API 邮件分类，月度预算 | `gmail_sync` |
| `email_lookup.py` | 写库 | 匹配不上的邮件去 SEEK / LinkedIn 搜职位并入库 | `run.run_gmail` |
| `web.py` | HTTP 服务 | 网页和 JSON 接口 | `auto-job-web.service` |
| `push.py` | 外部接口 | Web Push（VAPID） | `run.py`、`web.py`、`notify_failure.py` |
| `notify_failure.py` | 兜底 | 任务崩溃时推送 | `auto-job-failed@.service` |
| `static/index.html` | 前端 | 单文件 PWA（原生 JS） | 浏览器 |
| `static/sw.js` | 前端 | service worker：**只**收推送、点通知打开页面，不缓存 | 浏览器 |
| `docs_check.py` | 检查 | 本文档和代码逐项对比；篇幅提醒（本文档超过 1000 行、第 12 节某个子系统超过 150 行时提示拆到 `docs/<子系统>.md`，原处留一行索引；联动清单和第 9 节始终留在本文档） | 提交前钩子、手动 |
| `test_core.py` | 测试（进 git） | 状态规则、邮件改状态、后台打分（中途 / 最后一刻 / id 复用）、并发写入。有本机 `filters.toml` 时会读它 | `python3 -m unittest test_core` |
| `test_rules.py` | 测试（不进 git） | 依赖个人 `filters.toml` 的测试 | `python3 -m unittest test_rules` |
| `checks/ui_check.mjs` | 检查 | 网页脚本语法、页面启动、投递链接各种情况（只发 GET，可对正式服务跑） | 改了 `static/` 后手动跑 |
| `hooks/pre-commit` | git 钩子 | 改了非 `*.md` 文件必须同时改 CHANGELOG.md；对暂存区快照跑 `docs_check.py` | `git config core.hooksPath hooks` |
| `hooks/commit-msg` | git 钩子 | 提交标题必须是 CHANGELOG.md 里的一个 `## 标题` 行 | 同上 |

---

## 3. 仓库外的东西

### 3.1 不进 git 的文件（`.gitignore`）

| 文件 | 内容 | 注意 |
|---|---|---|
| `jobs.db`、`jobs.db-*` | 数据库 | 改表结构前先 `sqlite3 jobs.db ".backup <路径>"` |
| `searches.toml` | 搜索条件 | 没有时退回 `searches.example.toml`（`rules.config_path`） |
| `filters.toml` | 过滤规则 | 没有时退回 `filters.example.toml`；`[gmail] extra_ignore_subject` 也在这里 |
| `blocked_companies.json` | 拉黑的公司 | 网页上维护；`rules.set_blocked` 原子写入 |
| `profile.json` | 「我的」页初始内容 | 只在 `kv.profile` 不存在时用 |
| `resume.txt` | 简历 | 打分实际用的是 `~/job-match/AGENTS.md` |
| `credentials.json`、`token.json` | Gmail OAuth | 600 |
| `push_keys.json` | VAPID 私钥 + subject | 600；**`GET /api/push/key` 在文件不存在时会生成它** |
| `.env` | 可选，`GEMINI_API_KEY` | |
| `.gmail.lock` | Gmail 同步文件锁 | |
| `LOCAL.md` | 本机私人信息（GCP 项目 ID 等） | |
| `test_rules.py` | 依赖个人规则的测试 | |

### 3.2 仓库外的文件和程序

| 位置 | 作用 |
|---|---|
| `~/job-match/AGENTS.md` | 打分规则 + 完整简历（单一来源）。`score._rules()` 每次读出来放进消息；在这个目录里直接和 agy 聊天时，默认 agent 也会自动读它 |
| `~/job-match/.agents/agents/scorer/agent.md` | 精简 agent：`excludeDefaultComponents: true`、`tools: []`、`inheritCustomizations: false` |
| `~/.cache/auto-job-profile` | 专用 Chrome profile（SEEK、LinkedIn 都在这里单独登录）。`AUTO_JOB_PROFILE` 可改 |
| `~/.config/systemd/user/auto-job*` | systemd 单元 |
| `~/.bashrc` | `GEMINI_API_KEY` 的兜底来源（`llm._key`） |

### 3.3 运行环境

- Python 3.14，**没有 pip**：只用标准库 + 系统自带的 `cryptography`、`PIL`。
- Node + Playwright（`~/.npm-global/lib/node_modules/playwright`，`PW_PATH` 可改）；`google-chrome`；`Xvfb`（没有时退回新版 headless）。
- LinkedIn / SEEK 登录模式需要**已登录的桌面会话**：cookie 用 GNOME keyring 加密，keyring 登录后才解锁。
- `agy` CLI（`~/.local/bin/agy`），用订阅额度，不用 API key。
- Tailscale（`tailscale serve`）。

### 3.4 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `AUTO_JOB_DB` | `jobs.db` | 数据库路径（`db.DB_PATH`） |
| `AUTO_JOB_BLOCKED` | `blocked_companies.json` | 拉黑名单路径 |
| `AUTO_JOB_MATCH_DIR` | `~/job-match` | 打分规则和 agent 所在目录 |
| `AUTO_JOB_AGY_MODEL` | `gemini-4-neon-medium` | 打分模型 |
| `AUTO_JOB_GEMINI_MODEL` | `gemini-flash-lite-latest` | 邮件分类模型 |
| `AUTO_JOB_GEMINI_BUDGET` | `8` | 邮件分类月度预算（美元） |
| `GEMINI_API_KEY` | — | 邮件分类的 key（也会读 `.env`、`~/.bashrc`） |
| `AUTO_JOB_PUSH_SUBJECT` | `push_keys.json` 里的 subject | VAPID subject |
| `AUTO_JOB_PROFILE` | `~/.cache/auto-job-profile` | 专用浏览器 profile |
| `PW_PATH` | `~/.npm-global/...` | Playwright 路径 |

**测试隔离只隔离了一部分**：`AUTO_JOB_DB` + `AUTO_JOB_BLOCKED` 能让网页和入库用测试库（`.claude/launch.json` 的 `web-test` 就是这样，端口 8799；它指向的测试库路径是某次会话的临时目录，用之前先改），但 Gmail 凭据、`push_keys.json`、浏览器 profile、agy 仍然是真的。测试时不要点「发送测试通知」，不要跑会推送或抓取的命令。

---

## 4. 数据字典

数据库：SQLite，WAL 模式，`foreign_keys=ON`，连接超时 30 秒（`db.connect`）。**`db.connect()` 每次连接都会跑建表和迁移**（见 4.4）。时间一律 UTC ISO（`db.now()`，`2026-11-27T01:02:03Z`），例外：`emails.received_at` 是本地时区 ISO。

核心概念：**job 是"一个真实的职位"，posting 是某个平台上的一条帖子**。同一个职位在 SEEK 和 LinkedIn 上各有一条、或被中介转发，就是一个 job 挂着几个 posting。

### 表 `jobs`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `id` | 主键 | `Ingestor._attach` |
| `company` | 取自主帖 | `refresh_job` |
| `title` | 取自主帖 | `refresh_job` |
| `location` | 取自主帖 | `refresh_job` |
| `company_norm` | `rules.norm_company` 规范化后的公司名 | `refresh_job` |
| `title_norm` | `rules.norm_title` 规范化后的职位名 | `refresh_job` |
| `primary_posting_id` | 主帖：非中介优先，其次最早看到的（`ORDER BY is_agency, first_seen, id`） | `refresh_job` |
| `status` | 职位状态，见 5.1 | 网页、`gmail_sync.apply_email`、`merge_jobs` |
| `filtered` | 1 = 有 reject 规则命中且没有手动放行 | `refresh_job`、放行接口 |
| `filter_override` | 1 = 手动放行（忽略 reject） | 放行接口、`merge_jobs` |
| `soft_score` | soft 命中数 − boost 命中数（目前排序不用它） | `refresh_job` |
| `notes` | 备注 | 网页 |
| `created_at` | 新建时间 | `_attach` |
| `updated_at` | 最后修改 | 多处 |
| `llm_score` | agy 匹配分 0–100；NULL = 还没打 | `score._save`、`merge_jobs`（取两边较高的） |
| `llm_track` | `ai` / `swe` / `other_story` / `cross_stack`，见 5.3 | 同上 |
| `llm_detail` | JSON：`reason`、`gaps_minor[]`、`gaps_major[]`、`red_flags[]`、`model`、`at` | 同上 |

### 表 `postings`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `id` | 主键 | `Ingestor._add` |
| `source` | `seek` / `linkedin` / `email`（邮件补抓两边都搜不到时按邮件建的） | `_add` |
| `source_id` | 平台上的 id；`source=email` 时是 Gmail message id | `_add` |
| `job_id` | 挂在哪个 job 上 | `_attach`、`merge_jobs` |
| `url` | 职位页链接；`source=email` 时指向 Gmail | `_add` |
| `title` | 帖子上的职位名 | `_add`、已见帖子变化时更新 |
| `company` | 帖子上的公司名 | 同上 |
| `location` | 地点 | 同上 |
| `work_type` | 全职 / 合同等（平台原文） | 同上 |
| `work_arrangement` | Remote / Hybrid / On-site（平台原文） | 同上 |
| `salary` | 薪资（平台原文） | 同上 |
| `posted_at` | 发布时间 UTC；LinkedIn 的 "3 hours ago" 抓取时换算 | 同上 |
| `description` | JD 纯文本 | 同上 |
| `jd_hash` | JD 的 hash，用来判断 JD 变没变 | 同上 |
| `is_agency` | 1 = 中介 | `_apply_filters` |
| `extra` | 平台特有字段 JSON，见 4.1 | `_add`（**只在新帖子时写；已见帖子不会更新 extra**） |
| `first_seen` | 第一次看到 | `_add` |
| `last_seen` | 最后一次看到 | `_add` |

### 表 `search_hits`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `posting_id` | 帖子 | `Ingestor._hit` |
| `search` | 命中的搜索名（`searches.toml` 里的 name；邮件补抓是 `邮件补抓`） | 同上 |
| `first_seen` | 第一次命中 | 同上 |
| `last_seen` | 最后一次命中 | 同上 |

### 表 `filter_results`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `posting_id` | 帖子 | `Ingestor._write_filters`（先删后写） |
| `rule` | 规则名，见 5.4 | 同上 |
| `action` | `reject` / `soft` / `boost` / `tag` | 同上 |
| `reason` | 命中的原文片段或名单名 | 同上 |

### 表 `events`（状态时间线）

`jobs.status` 不一定等于时间线上最新一条的状态：邮件事件按收信时间记（可能早于别的事件），合并职位时状态按"更往前"取。

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `id` | 主键 | |
| `job_id` | 职位 | |
| `at` | 时间（邮件事件用收信时间） | |
| `status` | 状态变化时填新状态，否则 NULL | |
| `source` | `auto`（抓取）/ `manual`（网页）/ `email`（邮件） | |
| `detail` | 说明 | |

### 表 `application_qa`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `id` | 主键 | 网页 |
| `job_id` | 职位 | 网页 |
| `question` | 问题；NULL = 整段粘贴的原文在 `answer` 里 | 网页 |
| `answer` | 回答 | 网页 |
| `source` | 默认 `paste` | 网页 |
| `created_at` | 时间 | 网页 |

### 表 `dup_candidates`（疑似重复，等网页上确认）

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `job_a` | 较小的 job id（**`job_a < job_b`**） | `_attach`、`merge_jobs` |
| `job_b` | 较大的 job id | 同上 |
| `score` | JD 相似度 | 同上 |
| `reason` | 说明 | 同上 |
| `status` | `pending` / `merged` / `distinct` | 网页、`merge_jobs` |

### 表 `push_subscriptions`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `endpoint` | 推送地址，只接受 `push.PUSH_HOSTS` 里的厂商域名 | 订阅接口 |
| `p256dh` | 浏览器公钥 | 同上 |
| `auth` | 浏览器 auth | 同上 |
| `user_agent` | | 同上 |
| `created_at` | | 同上 |

推送返回 404/410 的订阅由 `push.send_all` 删掉。

### 表 `emails`（求职相关的邮件；无关的只存 id）

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `id` | Gmail message id | `gmail_sync.process_message` |
| `thread_id` | Gmail thread id（网页跳 Gmail 用） | 同上 |
| `received_at` | 收信时间，**本地时区** ISO | 同上 |
| `from_addr` | 发件地址 | 同上 |
| `from_name` | 发件人名 | 同上 |
| `subject` | 标题 | 同上 |
| `snippet` | 摘要 | 同上 |
| `body` | 去掉链接、截断后的正文 | 同上 |
| `status` | 邮件说明的职位状态，`db.EMAIL_STATUSES` 之一；NULL = 没认出来 | 同上 |
| `company` | 从邮件提取的公司名 | 同上 |
| `title` | 从邮件提取的职位名 | 同上 |
| `job_id` | 关联的职位 | `apply_email`、网页手动关联 |
| `match_reason` | 匹配说明 / 匹配不上的原因 | 同上 |
| `state` | `matched` / `unmatched` / `ignored` | 同上；网页「忽略」只改这一列 |
| `classified_by` | `rule` / `gemini` | 同上 |
| `lookup_at` | 邮件补抓的时间；NULL = 没补抓过 | `email_lookup.run` |
| `lookup_result` | 补抓结果；以 `出错` 开头的 6 小时后重试 | 同上 |
| `created_at` | | `process_message` |

自动判为无关的邮件（`state='ignored'`）：发件人、标题、正文存 NULL，摘要存空字符串；`thread_id`、`received_at`、`classified_by`、提取出的公司 / 职位名仍然保留。网页上手动「忽略」的只改 `state`，原内容全部保留。

### 表 `kv`

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `key` | 键 | 见下 |
| `value` | 值（字符串 / JSON） | 见下 |

| 键 | 值 | 谁写 |
|---|---|---|
| `gmail_history_id` | Gmail 增量同步游标 | `gmail_sync.sync`、`run.gmail_poll` |
| `gmail_last_ok` | 上次同步成功的 Unix 时间；history 过期时按它回补 | `gmail_sync.sync` |
| `profile` | 「我的」页内容 JSON 数组 `[{group, label, value}]` | 网页 |
| `failure_push:<单元名>` | 上次崩溃推送的 Unix 时间 | `notify_failure.py` |

### 表 `llm_usage`（Gemini 邮件分类用量，按月）

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `month` | `YYYY-MM` | `llm._record` |
| `calls` | 调用次数 | 同上 |
| `tokens_in` | 输入 token | 同上 |
| `tokens_out` | 输出 + 思考 token | 同上 |
| `cost` | 按偏高单价估算的美元 | 同上 |

### 表 `runs`（每次抓取 / 同步一条）

| 列 | 含义 / 取值 | 谁写 |
|---|---|---|
| `id` | 主键 | `run.start_run` |
| `source` | `seek` / `linkedin` / `gmail` | 同上 |
| `started_at` | | 同上 |
| `finished_at` | | `run.finish_run` |
| `seen` | 看到的条数 | 同上 |
| `new` | 新增条数 | 同上 |
| `error` | 出错信息；0 条也算出错（gmail 除外）。网页顶部红条显示每个来源**最近一条**的 error | 同上 |

### 4.1 `postings.extra`（JSON）各来源的键

**重新抓到已见帖子时不会更新 extra**（`Ingestor._add` 只更新 url/标题/公司/地点/工作类型/办公方式/薪资/发布时间/JD）。会改已有帖子 extra 的只有：`run.resolve_seek_links`（`apply_url`、`apply_url_tried`）、`db.connect` 里的迁移、手动补数据脚本。所以新加一个 extra 键，老帖子不会有，要么写补数据脚本，要么让读的地方能处理缺这个键。

同一个值在不同接口里的位置不一样：`GET /api/jobs` 把 `quick_apply`、`apply_url` 摊平到每条 posting 上（`p.quick_apply`）；`GET /api/jobs/<id>` 放在 `p.extra` 里；`score.QUICK_SQL` 直接读数据库里的 JSON。网页 `applyUrl` 两种都认。

| 键 | 来源 | 含义 | 从什么时候有 / 老数据 |
|---|---|---|---|
| `quick_apply` | seek、linkedin（登录 / 不登录）、邮件补抓（seek / linkedin） | true = 一键投（SEEK Quick apply / LinkedIn Easy Apply），false = 外部投递，缺失或 null = 不知道。SEEK：`isLinkOut` 为 false 或缺失时算一键投；拉 JD 失败时写 null | LinkedIn 登录模式最早叫 `easy_apply`（`db.connect` 已迁移）；SEEK 2026-11-26 起；邮件补抓的 LinkedIn 帖子 2026-11-27 起。老帖子 2026-11-27 补过一次，仍有少数缺失（数字见 CHANGELOG） |
| `apply_url` | seek（`seek_links.mjs`）、linkedin 登录模式 | 外部投递的公司官网地址。SEEK 只给 `quick_apply=false` 的查；**LinkedIn 只要读到 "Apply on company website" 链接就写，和 Easy Apply 检测是分开的**。网页对一键投的帖子不用它 | 2026-11-26 起；2026-11-27 之前 SEEK 一键投曾存过 `/job/<id>/apply`，已清掉 |
| `apply_url_tried` | seek | 查过官网链接的时间（查到没查到都写）。只有 `seek_links.mjs` 正常退出时才写；整批失败的下一轮还会查 | 2026-11-26 起 |
| `teaser` | seek（含邮件补抓） | 列表摘要 | |
| `classification` | seek（邮件补抓的没有） | SEEK 分类 | |
| `posted_text` | linkedin | "3 hours ago" 原文 | |
| `applicants` | linkedin | 申请人数原文 | |
| `mode` | linkedin 不登录模式 | `guest` | |
| `seniority`、`industries` | linkedin 不登录模式 | 职位标准字段 | |
| `via` | 邮件补抓建的帖子 | `email_lookup` | |
| `note` | `source=email` | 说明 | |

### 4.2 抓取脚本交给 `Ingestor.add()` 的帖子格式

- 必填：`source`、`source_id`、`search`。
- 新帖子还应带：`url`、`title`、`company`、`location`、`description`、`posted_at`（UTC ISO）；可选 `work_type`、`work_arrangement`、`salary`、`extra`。
- 已知帖子可以只带必填字段（LinkedIn 增量不再点开），只更新 `last_seen` 和 `search_hits`。
- 返回 `new` / `seen`。**调用方每条之后要 `commit`**（抓取是按网络速度一条条来的，攒着不提交会一直占写锁）。

### 4.3 拉黑名单 `blocked_companies.json`

`[{name, at}]`。`rules.blocked()` 按文件修改时间缓存成 `{规范化名: 原名}`；写入用锁 + 临时文件 + `os.replace`。文件损坏时普通读取当作空名单并警告，`strict=True` 时抛错。

### 4.4 `db.connect()` 里的迁移（幂等，每次连接都跑）

- `emails` 补 `lookup_at`、`lookup_result` 列。
- `emails.category` 改名为 `status`，`viewed` / `other` 改成 `applied`（2026-11-27）。
- `jobs` 补 `llm_score`、`llm_track`、`llm_detail`；删掉 `match_score`、`match_high`、`job_vectors` 表、`kv.match_model`（2026-11-26 删 embedding 打分）。
- `postings.extra` 里的 `easy_apply` 改名为 `quick_apply`。

加新迁移时：必须幂等；改表结构前备份 `jobs.db`；`web.py` 每个请求都会 `connect()`，迁移要快。

---

## 5. 状态机和派生规则

### 5.1 职位状态（`db.STATUSES`、`db.STATUS_LABEL`；网页 `STATUS`）

全项目只有这一套。邮件分类的结果也直接是其中之一。

| 状态 | 中文 | 所在视图 | 能由邮件设置（`EMAIL_STATUSES`） |
|---|---|---|---|
| `new` | 新 | 待处理 | 否 |
| `shortlisted` | 想投（不再有按钮，老数据照常显示） | 待处理 | 否 |
| `skipped` | 不投 | 已结束 | 否 |
| `applied` | 已投 | 进行中 | 是 |
| `assessment` | 笔试/测评 | 进行中 | 是 |
| `interview` | 面试 | 进行中 | 是 |
| `rejected` | 被拒 | 已结束 | 是 |
| `offer` | Offer | 进行中 | 是 |
| `closed` | 已关闭 | 已结束 | 是 |

视图（`web.VIEWS`，网页 `VIEW_OF`）：
- 待处理 `active`：`filtered=0` 且 new / shortlisted。
- 进行中 `pipeline`：applied / assessment / interview / offer，**不看 filtered**（投了说明你决定要它）。
- 已结束 `closed`：`filtered=0` 且 skipped / rejected / closed。
- 已过滤 `filtered`：`filtered=1` 且不在进行中。
- 全部 `all`。

### 5.2 邮件 → 职位状态（`db.next_status(cur, target)`）

职位状态怎么变**只写在这个函数里**，邮件同步、邮件补抓、网页手动关联都走它（经 `gmail_sync.apply_email`）。

| 邮件说明的状态 | 规则 |
|---|---|
| NULL（没认出来） | 不改状态，只挂上邮件、写一条时间线 |
| `rejected` | 除了 `offer`（和已经是被拒）都改成被拒 |
| `closed` | 只有 new / shortlisted / skipped / applied 改成已关闭；已进测评 / 面试 / Offer / 被拒的不动；之后再来面试邀请照样往前推 |
| 其他（applied / assessment / interview / offer） | 只往前走（`_RANK`），`offer` / `rejected` 之后不再变 |

邮件分类（`gmail_sync.classify_rules` → 不确定时 `llm.classify_email`）：
- 规则模板：SEEK 确认 / 不太可能进一步 / 已查看 / 职位关闭；LinkedIn 已发送 / 已查看；标题里明确的结果；具体的拒信措辞。"对方已查看"算 `applied`。
- 规则不确定（只在正文里出现的线索、标题是 Update / Outcome / Next steps 之类）或没提取到公司名时交给 Gemini；Gemini 的 JSON 直接返回 `status`（取值 `EMAIL_STATUSES`）和 `job_related`。
- **Gemini 调用失败 / 超预算 / 没 key 时**（`llm.classify_email` 返回 None），用规则的初步结果（可能是 NULL）存库，**游标照样往前推**，这封邮件不会再被自动重新分类。
- 被拒 / 笔试 / 面试 / Offer（`gmail_sync.NOTABLE`）推送到手机：按邮件本身的分类推，即使没匹配上职位、或职位状态没变。

网页手动改状态（`POST /api/jobs/<id>/status`）**不走 `next_status`**，可以改成任何状态。

### 5.3 匹配分（`score.py`）

- 后台和默认命令只给 `filtered=0` 且 new / shortlisted、`llm_score IS NULL` 的职位打分，每个只打一次。`--rescore` 重打待处理的；`--all` 给所有 `filtered=0` 且没打分的打（包括已处理的）。
- 写分数时核对 `id` + `created_at`（`score._save`）：打分期间职位被合并删掉、id 被新职位复用时丢弃这个分数（`created_at` 精确到秒，同一秒内删除又复用 id 的极端情况防不住）。
- `track` 四类：

| track | 中文 | 含义 |
|---|---|---|
| `ai` | AI 方向 | 职位就是简历讲的故事 |
| `swe` | 普通开发 | 后端 / 全栈，几乎不提 AI |
| `other_story` | 要换简历 | 能做，但这份简历过不了筛（data、DevOps、平台…） |
| `cross_stack` | 跨栈 | Java / PHP / Salesforce / SAP / 移动端… |

- 排序分 `score.RANK_SQL`：`llm_score`，`swe` 类且没有任何一条一键投帖子的减 `SWE_PENALTY=25`。SQL 表达式，**表别名必须是 `j`**；改它不用重打分。网页列表和推送共用。
- 排序按每个职位自己的状态决定（`web.list_jobs`）：状态是 new / shortlisted 的按排序分从高到低，**没打分的排最前**（`NULLS FIRST`，不埋掉），同分按发布时间；其他状态按最近活动时间。「全部」「已过滤」视图里也是这样。
- 网页：卡片显示分数 + 类别，没打分的待处理职位显示「待打分」；分数颜色 ≥70 绿、≥45 灰、其余红（`scoreCls`）；详情「匹配度」一节列理由、风险、硬缺口、小缺口。
- 推送标题里的「N 个 60 分以上」：`run.HIGH_SCORE = 60`。
- agy 调用实测（2026-11-26）：1 轮、约 5k token、10–16 秒。代码里没有断言，换模型或 agy 升级后可能变。

### 5.4 过滤规则名（`rules.evaluate`；网页 `RULE` 里的中文名）

| 规则名 | 中文 | 说明 |
|---|---|---|
| `blocked` | 已拉黑 | 拉黑名单，动作写死为 reject |
| `crowdwork_company` | 众包/标注 | 公司名单 |
| `crowdwork_title` | 标注类职位 | 职位名 |
| `crowdwork_jd` | AI 训练零工 | JD 同时有按小时计价 + 评估 AI 输出 |
| `outsourcing` | IT 外包 | 公司名单 |
| `seniority` | 职级过高 | 有豁免（FDE） |
| `phd` | PhD | 职位名或 JD 硬性要求 |
| `agency` | 中介 | 名单 / 名称模式；动作写死为 tag |
| `agency_low_priority` | 低优先级中介 | |
| `visa` | 身份要求 | 动作写死为 reject |
| `visa_note` | 工作权限 | 动作写死为 tag，只打标签 |
| `intern` | 实习 | |
| `off_stack` | 技术栈不对口 | |
| `stack_match` | 技术栈匹配 | boost / 标签 |

其余规则的 action（reject / soft / boost / tag）和具体名单、正则在 `filters.toml`（个人配置，不进 git；格式见 `filters.example.toml`）。

### 5.5 去重（`rules.match`、`Ingestor._attach`）

- 同平台：`source + source_id` 唯一。
- 跨帖子：JD 相似度 ≥0.8 直接合并；同公司同职位名且 JD ≥0.4（或有一边没 JD）合并；同公司同职位名但 JD 不像 → 疑似重复；职位名不同但 JD ≥0.6 → 疑似重复。疑似重复在网页上确认合并或「不是」。
- 合并（`ingest.merge_jobs`）把 drop 的 postings / events / application_qa / emails 改挂到 keep，状态取更"往前"的，备注拼接，`filter_override` 取或，匹配分取较高的（连同 track、detail），疑似重复关系改挂到 keep，然后**删掉 drop 这一行**。

### 5.6 投递按钮（网页 `applyUrl`、`extLink`、`PLATFORM_HOST`）

每条帖子一个按钮。地址：`quick_apply` 为 true → 帖子的 `url`（职位页，原样使用）；否则有 `apply_url` 就用它（包括 `quick_apply` 缺失的帖子）；都没有 → 帖子的 `url`。

iPhone 上**按地址去哪决定怎么打开**（不看 `quick_apply`，标记缺了也不会判错）：去 SEEK / LinkedIn 的用普通 `https`（装了 App 就开 App，那里是登录状态）；去其他域名（公司官网）的换成 `googlechromes://`，直接在 Chrome 打开。电脑上一律普通链接、新标签页。

| 情况 | 按钮文字 | iPhone 上的地址 |
|---|---|---|
| SEEK 一键投 | 一键投（SEEK） | `https://au.seek.com/job/<id>`（**不是** `/apply`） |
| LinkedIn 一键投 | 一键投（LinkedIn） | `https://www.linkedin.com/jobs/view/<id>/` |
| SEEK 外部投递，有官网链接 | 去官网投递 | `googlechromes://<官网>` |
| LinkedIn 外部投递，有官网链接 | 去官网投递 | `googlechromes://<官网>` |
| 外部投递还没有官网链接；或不知道是不是一键投且没有官网链接 | 在 SEEK / LinkedIn 打开 | `https://` 职位页 |
| 不知道是不是一键投、但有官网链接 | 去官网投递 | `googlechromes://<官网>` |
| `source=email` | 在 Gmail 打开 | `googlechromes://mail.google.com/...` |

官网链接怎么来：
- LinkedIn：登录模式抓取时读 "Apply on company website" 链接（`linkedin.com/safety/go/?url=<官网>`）。不登录模式拿不到。
- SEEK：接口只有 `isLinkOut`；`seek_links.mjs` 用登录的专用 profile 打开 `/job/<id>/apply`，记下跳转到的地址。只查规则放行的待处理职位，每条只查一次（`apply_url_tried`），每轮最多 20 个（`run.SEEK_LINKS_PER_RUN`）。

---

## 6. HTTP 接口（`web.py`）

- 只监听 `127.0.0.1`；手机经 `tailscale serve` 访问。
- 只处理 GET 和 POST。所有 POST 必须带 `Origin`，且 `Origin` 的 netloc（主机 + 端口）等于 `Host` 头、scheme 是 http 或 https，否则 403。
- 静态文件（`/`、`/sw.js`、图标等）不连数据库。其他请求各自 `connect()`，出错 rollback；数据库打不开返回 503。
- 请求体 ≤256KB（在读取请求体时检查）；读超时 30 秒。
- 很多写接口不检查职位是否存在：比如给不存在的职位改备注返回 `200 null`，手动关联到不存在的职位也返回成功（见第 11 节）。
- 响应 `cache-control: no-store`。

| 接口 | 参数 | 返回 / 作用 |
|---|---|---|
| `GET /` | | `static/index.html`（每次请求都从磁盘读） |
| `GET /sw.js` | | service worker（必须在根路径） |
| `GET /manifest.webmanifest` | | PWA manifest |
| `GET /static/icon-192.png` | | 图标 |
| `GET /static/icon-512.png` | | 图标 |
| `GET /static/apple-touch-icon.png` | | 图标 |
| `GET /apple-touch-icon.png` | | 图标 |
| `GET /favicon.ico` | | 图标 |
| `GET /api/summary` | | 各状态计数（不含被过滤的）、各视图计数、每个来源最近一次 run、待确认重复数、未归类邮件数、`statuses`、`ver`（index.html 修改时间） |
| `GET /api/jobs` | `view`（默认 active）、`q`、`source` | 列表，最多 1000 条（搜索时先截 1000 条再做整词匹配）；每条带 `rank_score`、postings（摊平的 `quick_apply`、`apply_url`）、tags、searches；英文搜索整词匹配、中文按子串，标题 / 公司命中的排前面 |
| `GET /api/jobs/<id>` | | 详情：job、postings（含 extra、filters、searches）、dups、emails、events、qa；404 |
| `POST /api/jobs/<id>/status` | `status`（必须在 STATUSES）、`detail`、`expect` | 改状态 + 写时间线。`expect` 和当前状态不同时 409（撤销用）。**先查后改，不是原子的**：查完到改之间邮件同步写入的话拦不住 |
| `POST /api/jobs/<id>/note` | `detail` | 时间线加一条备注 |
| `POST /api/jobs/<id>/notes` | `notes` | 改备注 |
| `POST /api/jobs/<id>/qa` | `question`、`answer`、`source` | 加一条问答 |
| `POST /api/jobs/<id>/override` | `value` | 手动放行 / 取消放行，重算 `filtered` |
| `POST /api/qa/<id>/delete` | | 删一条问答 |
| `GET /api/emails` | `state`（默认 unmatched） | 邮件列表，最多 200 |
| `POST /api/emails/<id>/link` | `job_id` | 手动关联（走 `apply_email` → `next_status`）；已经是 matched 且关联到同一个职位时不重复写。不检查职位是否存在 |
| `POST /api/emails/<id>/ignore` | | 只改 `state='ignored'` |
| `GET /api/blocked` | | 拉黑名单 |
| `POST /api/blocked` | `company`、`blocked` | 拉黑 / 取消，然后 `refilter_company` |
| `GET /api/profile` | | 「我的」页内容 |
| `POST /api/profile` | `items` | 保存（每项截断长度） |
| `GET /api/push/key` | | VAPID 公钥 + 订阅数。**文件不存在时会生成 `push_keys.json`** |
| `POST /api/push/subscribe` | `endpoint`、`keys.p256dh`、`keys.auth` | 只接受 `push.PUSH_HOSTS` 里的厂商地址 |
| `POST /api/push/unsubscribe` | `endpoint` | |
| `POST /api/push/test` | | **真的给所有订阅发一条** |
| `POST /api/dups` | `a`、`b`、`action`（merge / distinct）、`keep` | 处理疑似重复；这对不是 pending 时 409；`keep` 必须是 a 或 b，不传时保留 id 较小的 |

---

## 7. 外部依赖

| 依赖 | 怎么用 | 限制 / 坑 |
|---|---|---|
| SEEK 搜索接口 `au.seek.com/api/jobsearch/v5/search` | 不登录，默认按发布时间倒序翻页（`searches.toml` 的参数可以覆盖） | 只给库里没有的帖子拉 JD，间隔 1–3 秒 |
| SEEK GraphQL `au.seek.com/graphql` `jobDetails` | JD（`content`）+ `isLinkOut` | 不登录拿不到官网地址；内省关闭 |
| SEEK 投递页 `/job/<id>/apply` | 登录状态下会跳到官网（`seek_links.mjs`） | SEEK App 的通用链接**排除了** `/job/*/apply`，不能给用户点 |
| LinkedIn 登录模式 | 专用 profile + Xvfb，模拟人工浏览，库里已有的不点开；默认每条搜索最多点开 40 个、最多翻 8 页（`run.py` 参数），没有下一页按钮时提前停 | 登录失效或没有桌面会话 → 退出码 3 → 自动改用不登录模式，网页标红 |
| LinkedIn 不登录接口 `jobs-guest` | 搜索、职位详情（能看出是不是外部投递） | 每页 10 条；HTTP 429 / 999 就停；不认 remote 参数 |
| Gmail API | 只读（`gmail.readonly`），Personal mode GCP 项目，historyId 增量 | **只有 HTTP 404 才能跳过**：取元数据 404 跳过这封；history 过期 404 按 `gmail_last_ok` 回补；其他错误不推进游标。取全文时的 404 会让整次同步失败 |
| Gemini API（`llm.py`） | 邮件分类，结构化 JSON 输出 | 月度预算做在代码里（偏高估价）；key 按 环境变量 → `.env` → `~/.bashrc` 的顺序找（systemd 里没有这个环境变量） |
| agy CLI | 打分：`agy -p <AGENTS.md + JD + JSON 格式> --agent scorer --model <模型> --output-format json --disable-slash-commands --print-timeout 240s` | **不能加 `--json-schema`**（精简 agent 没有结构化输出组件，会反复重试）；精简 agent **不会自动读 AGENTS.md**；用订阅的 Gemini 额度（`agy -p /quota` 查） |
| Web Push（`push.py`） | VAPID（RFC 8292），`cryptography` 签名 | Apple 要求 subject 是 mailto: 或 https:；iOS 16.4+、必须从主屏幕图标打开才能订阅 |
| Tailscale | `tailscale serve --https=8443` | 本机不走 MagicDNS，测试用 `curl --resolve`；不能用 100.x IP（PWA 要有效证书） |

---

## 8. 联动清单（改 X 时必须一起看 Y，并确认预期结果）

| 改什么 | 一起看 | 预期结果 / 怎么验证 |
|---|---|---|
| **职位状态**（加 / 改状态、改转换规则） | `db.STATUSES` / `STATUS_LABEL` / `EMAIL_STATUSES` / `_RANK` / `next_status`；`ingest.merge_jobs` 的状态顺序；`web.VIEWS`；`gmail_sync.NOTABLE` / `TEMPLATES` / `match_job` 的 `prefer`；`llm.PROMPT`；网页 `STATUS` / `VIEW_OF` / `PRIMARY` / `MAIL_CLS` / `moreSheet`；`run.notify_mail` | `docs_check.py` 通过；`test_core` 的 `next_status` 表格测试覆盖新规则；**自动同步和网页手动关联两条路径都试**；关闭邮件：new / applied → closed，interview 不变 |
| **邮件分类** | `gmail_sync.classify_rules` / `TEMPLATES` / `process_message`、`llm.SCHEMA` / `PROMPT` / `classify_email`、`db.EMAIL_STATUSES` | 分类结果只能是 `EMAIL_STATUSES` 之一或 NULL；Gemini 失败时用规则初步结果且游标照常推进（5.2）；`python3 gmail_sync.py --reprocess` **不会重新分类已经存进库的邮件**，也**不拿 `.gmail.lock`**（别和定时任务同时跑） |
| **投递链接 / 一键投** | `seek.py`（`isLinkOut`）、`seek_links.mjs`、`run.resolve_seek_links`、`linkedin.mjs`、`linkedin_guest.py`、`email_lookup.py`；`web.list_jobs`（摊平）/ `web.job_detail`（嵌在 extra）、`score.QUICK_SQL`；网页 `applyUrl` / `extLink` / `PLATFORM_HOST` / `isQuick` | 5.6 那张表的**每一行**（包括缺 `quick_apply` 的老帖子）都要对：跑 `node checks/ui_check.mjs`；新抓的四种来源都要写 `quick_apply`；`apply_url` 只存官网 |
| **`postings.extra` 加新键** | 所有写 extra 的地方（`seek.py`、`linkedin.mjs`、`linkedin_guest.py`、`email_lookup.py` 三处） | 老帖子不会自动有（`_add` 不更新已见帖子的 extra）：写补数据脚本，或者读的地方处理缺失；在 4.1 表里写清楚从哪天起有、补没补 |
| **入库 / 写数据库** | `Ingestor.add`、所有长时间运行的写入方（抓取、后台打分、Gmail、网页） | 先读后写的事务必须 `BEGIN IMMEDIATE` 开头；网络请求、LLM、推送期间不能占着写锁；`test_rules.Concurrency` 两个连接并发写的测试要过 |
| **挂在职位上的新数据**（新表 / 新列带 job_id） | `ingest.merge_jobs`（改挂 / 合并）、`ingest.rebuild` 的拒绝条件、`web.job_detail` | 合并两个职位后数据不丢；`rebuild` 在有这类数据时拒绝 |
| **合并 / 删除职位** | `ingest.merge_jobs`、`score._save` / `Background._seen`（按 id + created_at）、所有拿着 job id 做异步工作的地方 | SQLite 会复用被删掉的最大 id：异步写回时必须核对不是另一个职位（`test_core.BackgroundScorer.test_reused_job_id_not_overwritten`） |
| **同一条数据可能同时被自动任务和手动操作修改** | `email_lookup.run`（先查再入库再关联）、`web` 的手动关联 / 忽略 / 合并（先查后写）、撤销的 `expect` | 目前都不是原子的（第 11 节）。改这些路径时要考虑"手动操作落在中间"会不会被覆盖 |
| **打分 / 排序** | `score.TRACKS` / `SCHEMA` / `RANK_SQL` / `Background`；`web.list_jobs`；`run.notify_phone` / `HIGH_SCORE`；网页 `TRACK` / `scoreCls` / `fitHtml`；`~/job-match/AGENTS.md` | `docs_check.py` 通过；`test_core` 的后台打分测试（包括"最后一刻进来的职位"）；改了 AGENTS.md 后 `python3 score.py --rescore` |
| **agy 调用方式** | `score.score_one`、`~/job-match/.agents/agents/scorer/agent.md` | 一次调用 1 轮（`num_turns=1`）、约 5k token；不加 `--json-schema`；消息里带 AGENTS.md 全文 |
| **过滤规则** | `filters.toml` + `filters.example.toml`（`load_filters` 要求所有段都在）、`rules.evaluate`、网页 `RULE` | `docs_check.py` 通过；改完 `python3 ingest.py --refilter`。**`[gmail] extra_ignore_subject` 是 `gmail_sync` 导入时读的**，`--refilter` 管不到，下次进程启动才生效 |
| **网页 `static/index.html`** | 全部；请求序号 `detailSeq` / `listSeq` / `pickSeq` 和 `curId()` | `node checks/ui_check.mjs`（脚本语法、页面启动无报错、投递链接）。新的异步请求要考虑"响应回来时用户已经换了职位 / 换了搜索词"（第 11 节列了现有的漏洞）。不用重启服务（每次请求都从磁盘读），已打开的 PWA 会因为 `ver` 变化自动重新加载 |
| **`web.py`** | | `systemctl --user restart auto-job-web.service` |
| **网页自动刷新 / 版本检查** | `web.summary` 的 `ver`、网页 `loadSummary`（有输入框焦点、开着弹层时不重新加载）、`static/sw.js`（不缓存） | 已经开着的页面在页面文件修改后下一次刷新数据时重新加载，且只加载一次。`ver` 是整秒的修改时间，同一秒内的两次修改只算一次 |
| **底部弹层 / 列表手势** | 网页 `openSheet` / `closeSheet` / `sheetState`、`#sheetWrap`、`touching` / `renderPending` | 关闭时 `#sheetWrap` 必须 `display:none`；手指按着时不重画列表 |
| **专用浏览器 profile** | `browser.mjs`、`linkedin.mjs`、`seek_links.mjs`、`node browser.mjs login` | 同一时间只有一个 Chrome 用它；不要把日常 Chrome 的 cookie 拷进去 |
| **Gmail 同步 / 恢复** | `gmail_sync.sync` / `new_message_ids` / `process_message`、`run.gmail_poll` / `gmail_lock`、`email_lookup.run` | 临时错误不能推进 `gmail_history_id`；补处理用 `after:2026/11/22`（跟踪起点），不要用 `newer_than` |
| **systemd 单元** | `~/.config/systemd/user/`（不在仓库） | `daemon-reload`；本文 1.1 节同步更新 |
| **个人信息** | 所有进 git 的文件 | 仓库是公开的：简历、邮箱、电话、个人过滤规则、GCP 项目 ID 不能进 git |

---

## 9. 不能破坏的规则（规则 + 原因）

### 数据库
- **先读后写的事务用 `BEGIN IMMEDIATE` 开头。** `SAVEPOINT` / 默认事务是延迟的，读完再升级写锁时如果别的连接在写，SQLite 直接报 `database is locked`，不走 30 秒等待。现在只有 `ingest.Ingestor.add` 和 `ingest.rebuild`（重建部分）这样做；网页的手动关联 / 合并、`email_lookup.run` 还是先查后写（第 11 节）。
- **网络请求、LLM、推送期间不占写锁**：抓取每条入库后立刻 commit；Gmail 忽略的邮件也先 commit 再继续。
- **不要为了"去掉"职位删 jobs 行**，改状态：删了下次抓取会当新职位加回来。（合并重复、`rebuild` 是有意的删除。）
- **改过滤规则用 `ingest.py --refilter`，不要用 `--rebuild`**：`rebuild` 会重建去重归属，有手动数据时会拒绝执行。
- **不要在需要回滚的事务里用 `executescript()`**：它会先自动提交。

### 抓取 / 浏览器
- **专用 profile 不能拷日常 Chrome 的 cookie**：SEEK 用 Auth0 refresh token 轮换，两边共用一个会话会被判为盗用，两边都掉线。
- **登录用 `node browser.mjs login`**（普通 Chrome，不经 Playwright）：Google 拒绝自动化浏览器登录。
- `browser.mjs` 去掉 Playwright 默认的 `--password-store=basic`，改用 `gnome-libsecret`，否则 Chrome 解不开 keyring 加密的 cookie 会直接删掉；Xvfb 下去掉 `WAYLAND_DISPLAY`、加 `--ozone-platform=x11`。
- LinkedIn 超过点开上限的职位**不入库**（入库了没有 JD，之后再也不会被点开）。
- LinkedIn remote 搜索用 `f_SAL` 参数 + 卡片地点 "(Remote)" 严格筛，按结果数翻完所有页（`f_WT=2` 已失效）。

### Gmail / Gemini
- 只有 404 能当"已删除"跳过；其他错误不推进 `gmail_history_id`。
- 补处理漏掉的邮件用 `after:2026/11/22`，不要 `newer_than`（会处理跟踪开始之前的邮件、改职位状态）。
- 自动判为无关的邮件不存发件人、标题、正文。
- LinkedIn 只有 `jobs-noreply@linkedin.com` 是申请相关。

### 打分
- 不传 `--json-schema`；AGENTS.md 由 `score.py` 放进消息。
- 后台打分停下前要再查一遍（`Background._loop` 的 `stopping`），否则最后几秒入库的职位漏到下一轮。
- 打分只用来排序，**不隐藏职位**（用户原则：多看几个也不要漏）。

### 网页 / iPhone
- 输入框、下拉框、文本框字号 16px（小于 16px 时 iOS 聚焦会放大页面且不缩回）。
- 底部弹层用原生 `scroll-snap` 做（JS 跟手指在 iPhone 上发涩）；外框 `.sheet` 负责圆角裁剪，`.sheet-body` 负责滚动；关闭时 `#sheetWrap` 必须 `display:none`（只 `visibility:hidden` 的话 iOS 上隐藏的全屏滚动层仍吃掉触摸，整页点不动）。
- 待处理视图里手指按着时不重画列表（重画会让 `touchend` 发到脱离页面的节点上，卡片卡在半路）。
- 打开详情、加载列表用请求序号（`detailSeq` / `listSeq`），只用最后一次请求的响应；详情里的操作一律作用到屏幕上显示的那个职位（`curId()`）。`pickSeq` 只区分不同的"选职位"弹层，同一弹层里连续搜索没有保护；`refreshAll`、`setStatus`、`saveNotes` 的响应也不检查用户是否已经换了职位（第 11 节）。
- 所有请求有超时（查看 15 秒、修改 20 秒；只管到收到响应头为止，读响应体时已经不计时）；点开职位时详情立刻打开、显示加载中。
- 投递链接按去向决定怎么打开（5.6）；SEEK 一键投不能链接 `/apply`。
- 撤销带 `expect`，状态已被别处改过时不覆盖（但见第 6 节：不是原子的）。
- `sw.js` 必须在根路径、不缓存任何页面和接口。

### 安全 / 隐私
- 网页只监听 `127.0.0.1`；写请求同源检查比较的是 Origin 的 host 和 Host 头（经 tailscale serve 后是 https）。
- 推送订阅只接受浏览器厂商的地址（`push.PUSH_HOSTS`）。
- 私人文件新建时设 600（`gmail.py`、`push.py`），不会去改已有文件的权限；systemd `UMask=0077`。
- 仓库公开：个人信息、过滤规则、简历不进 git。

---

## 10. 验证手册

| 场景 | 命令 |
|---|---|
| 文档和代码对得上 | `python3 docs_check.py` |
| 核心测试（不依赖个人规则） | `python3 -m unittest test_core` |
| 全部测试（依赖本机 `filters.toml`） | `python3 -m unittest test_rules` |
| 网页脚本语法 + 页面启动 + 投递链接 | 服务在跑时 `node checks/ui_check.mjs [http://127.0.0.1:8765]` |
| 用测试库看网页 | 复制一份库：`sqlite3 jobs.db ".backup /tmp/x.db"`，然后 `AUTO_JOB_DB=/tmp/x.db AUTO_JOB_BLOCKED=/tmp/b.json python3 web.py 8799`（注意 3.4 节：推送、Gmail 仍是真的） |
| 无界面浏览器里测手势 | 用 CDP `Input.dispatchTouchEvent` 逐步发 touchStart/Move/End；`Input.synthesizeScrollGesture` 在无界面 Chrome 里不触发滚动 |
| 从本机访问 tailnet 地址 | `curl --resolve <域名>:8443:<本机 tailnet IP> https://<域名>:8443/` |
| 改完要做的事 | 改 `web.py` → 重启 `auto-job-web.service`；改 `filters.toml` → `ingest.py --refilter`；改 `AGENTS.md` → `score.py --rescore`；改 systemd 单元 → `daemon-reload` |

测完要清理：测试时写进正式库 `runs` 表的伪造错误会让网页一直标红。

---

## 11. 已知限制（还没修）

- 撤销的 `expect` 检查不是原子的（第 6 节）。
- 网页手动关联邮件、处理疑似重复都是先查后写；`email_lookup.run` 先确认邮件还没处理、再入库提交、再关联——中间你在网页上手动关联或忽略了这封邮件，会被覆盖。
- Gemini 调用失败时邮件按规则的初步结果存库、游标照常推进，不会再自动重新分类（5.2）。
- 网页上一些写接口不检查职位是否存在（第 6 节）。
- 网页：同一个"选职位"弹层里连续搜索，响应可能乱序显示；`refreshAll` / `setStatus` / `saveNotes` 的响应回来时如果已经换了职位，可能画到错的职位上；请求超时不管读响应体的阶段。
- 页面版本 `ver` 是整秒，同一秒内的两次修改只算一次；自动重新加载时，正在看的职位详情会关掉、回到列表（打开职位不会把 id 写进地址，只有从通知点进来的 `/?job=` 会，且启动后马上去掉）。
- `gmail_sync.py --reprocess` 不拿 `.gmail.lock`，也不会重新分类已存的邮件。
- 专用浏览器 profile 没有跨进程锁。
- 测试环境变量只隔离了数据库和拉黑名单（3.4 节）。
- 取邮件全文时遇到 404 会让整次同步失败（只有取元数据的 404 被跳过）。
- 按职位名只保留工程类岗位、职位下架后自动标记：还没做。

---

## 12. 各子系统的细节规则（原 PITFALLS.md 里仍然有效的部分）

每条：规则 → 原因。改到对应子系统时看一眼。

### 抓取
- LinkedIn 新版搜索页的 `f_TPR` 只认 24 小时 / 一周 / 一个月，结果按相关度排、看过的职位隐藏发布时间 → 取 24 小时，逐个点开读发布时间，自己换算成绝对时间。
- LinkedIn 公开接口不认 `f_WT` 也不认 `f_SAL`，remote 搜索只保留地点是 "Australia" 的（保守，会漏）。
- LinkedIn 卡片上的 job id 点开前就能读到（`componentkey`），库里已有的不点开。
- LinkedIn 卡片职位名末尾可能带空格或 "(Verified job)"，比较前两边都规范化，否则公司 / 地点整体错位。
- 抓取要像人：鼠标移动、滚轮、按 JD 长度停留、随机长停顿；SEEK 拉 JD 间隔随机 1–3 秒。
- 没有登录桌面会话时（开了 Linger，开机未登录也会触发定时任务），LinkedIn 登录模式不可用，自动退到公开接口。

### 过滤 / 去重
- 公司名按规范化后**完整相等**匹配别名，不用子串（"Talent"、"Turing" 子串会误伤）。
- 职位名规范化只去掉地名、Remote 之类噪音，不在 " - " 处截断（否则同公司不同团队的职位被合并）。
- 同公司的 JD 套话多，不同岗位 JD 相似度也有 0.4–0.5 → 职位名不同时疑似重复的阈值是 0.6。
- PhD：只有单独一行的硬性要求才过滤，"BS, MS, or PhD" 这种可选项不算。
- AI 训练零工换公司名就漏网 → 除了公司名单，还按 JD 判断（按小时计价 + 评估 AI 输出 / RL environment / benchmark）。
- 职级规则豁免 Forward Deployed Engineer（Staff FDE 例外，见 `exempt_except_patterns`）。

### Gmail
- 授权用 GCP **Personal mode** 项目（只有创建者能授权、不用验证、refresh token 不会 7 天过期、没有未验证警告）。只能在新建项目时选。项目 ID、用的是哪个 Google 账号见 `LOCAL.md`。新建 OAuth client 时 secret 只能当场下载。
- 只申请 `gmail.readonly`。
- SEEK 确认邮件正文第一句后面没有句号，公司名会一路匹配下去 → 在 " seek" / "[" / "." 处截断。
- LinkedIn "Your application to X at Y" 的纯文本正文几乎是空的 → 纯文本和 HTML 取更长的那份。
- 确认邮件里常有 "unsuccessful applicants will be notified"、"later stages may include an interview" → 拒信只认具体措辞；只在正文里出现的面试 / 笔试 / 确认线索交给 Gemini 确认。
- 招聘方本人发的信（个人邮箱、标题只是 "Senior Engineer - 公司名"）：发件人和标题看不出来时，Gmail 放在「主要」标签页或没有群发特征的信再看正文。不能只靠"没有 List-Unsubscribe"判断真人来信（招聘外联工具也会加退订头）。
- 和求职无关的机构受理邮件也叫 "Acknowledgement of Application Received" → 在 `filters.toml` 的 `[gmail] extra_ignore_subject` 里排除。

### 推送 / 手机
- 不能用 100.x 的 IP 代替 `…ts.net` 域名：Web Push、service worker、PWA 要 HTTPS + 有效证书，Tailscale 只给 `…ts.net` 签证书。
- 本机网页走 tailnet 的 8443 端口（443 被别的程序占着）。
- iPhone 推送：iOS 16.4+，必须用 Safari「添加到主屏幕」、从图标打开才能申请通知权限。
- VAPID subject 用的是 tailnet 网页地址，没放个人邮箱。
- 主屏幕 Web App 不会自己刷新：回到前台（visibilitychange / pageshow）、每分钟自动刷新列表（输入时不刷、不重画详情）、顶部有刷新按钮；页面代码更新靠 `ver` 自动重新加载。
- 手动 / 脚本改状态不会推送；推送只在抓取发现新职位、邮件说明被拒 / 笔试 / 面试 / Offer、任务出错时发。
- 每小时的通知只统计本轮（`started_at >= t0`），只在有未过滤新职位或出错时发。
