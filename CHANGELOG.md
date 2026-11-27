# 修改记录（CHANGELOG）

新的在最上面。每次提交都要加一条，**标题必须和提交标题完全一致**（`docs_check.py` 按标题核对）。
项目现在是什么样看 [ARCHITECTURE.md](ARCHITECTURE.md)；这里记的是每次改了什么、为什么、数据动了什么、怎么验证的。

每条的格式：
```
## <提交标题>
<日期> · <提交号，写的时候还没有就留空，下次补>
- 起因：用户遇到的问题 / 提的需求 / 审查发现
- 改了什么：行为上的变化
- 文件：
- 数据：迁移、补数据、手动改的记录、备份在哪（没有就写"无"）
- 验证：实际做了什么验证（没做的不写）
- 遗留：还没解决的、有意不做的
```

---

## 已删除 / 试过放弃的方案（不要再做回来）

| 方案 | 什么时候放弃 | 为什么 |
|---|---|---|
| 本地 embedding 匹配分（Ollama + Qwen3-Embedding-0.6B + 逻辑回归，`match.py`） | 2026-11-26 | 投过的都在线以上（召回够），但线以上的 91 个仍被用户跳过，分不出"能做但这份简历过不了筛"的职位。Ollama 服务已停用、模型已删 |
| 用 Gemini API 给职位打分 | 2026-11-23 决定，2026-11-26 改为 agy | 当时不想把简历发给 Google；后来用户同意，改用 agy CLI（订阅额度，不用 API key） |
| 邮件分类单独一套 8 类（viewed / other …）再靠特例映射到职位状态 | 2026-11-27 | 两套状态对不上，修了三次都在特例上打转（"职位已关闭"一会儿不改状态、一会儿改成已投）。现在邮件直接输出职位状态，见 `db.next_status` |
| 按 `quick_apply` 标记决定投递链接跳不跳 Chrome | 2026-11-27 | 老帖子没有这个标记就判错。现在按链接去向决定 |
| SEEK 一键投链接投递页 `/job/<id>/apply` | 2026-11-27 | SEEK App 的通用链接排除了 `/job/*/apply`，iPhone 上只会开内嵌浏览器 |
| 精简 agy agent + `--json-schema` | 2026-11-26 | 结构化输出靠默认组件，被 `excludeDefaultComponents` 去掉了，会反复重试 4 轮 |
| 整轮抓完才统一打分 | 2026-11-26 | LinkedIn 一轮 7–10 分钟，新职位要等很久才有分数。改成后台边抓边打 |
| 底部弹层用 JS 跟手指（touchmove 改 transform） | 2026-11-25 | 几种调法在 iPhone 上都发涩（每帧等主线程、Safari 网页限 60 帧）。改用原生 scroll-snap |
| LinkedIn remote 参数 `f_WT=2`、关键词里写 "remote" | 2026-11-23 | `f_WT=2` 被新版页面删掉；关键词只是相关度加权。改用 `f_SAL` + 卡片地点筛 |
| 把日常 Chrome 的 cookie 拷进抓取 profile | 2026-11-22 | SEEK Auth0 refresh token 轮换，两边一起被登出。改用专用 profile 单独登录 |
| Gmail 邮件分类改用 agy | 2026-11-27 讨论后不做 | API 一次约 1 秒，agy 约 10 秒；面试邀请要及时推送；会和打分抢额度。成本可以忽略（11 月 77 次调用约 0.026 美元） |

---

## Project docs: ARCHITECTURE, CHANGELOG, CLAUDE.md with automatic doc checks
2026-11-27 ·
- 起因：对话一长就会忘掉前面的决定，改一处漏一处（两套状态、投递链接 4 种情况、老数据缺字段都是这样出的问题）。用户要一份"项目现在是什么样"的文档和一份修改记录。方案先由 Claude 和 agy 各自独立提出，合并后由 Codex（gpt-6.2-sol，high）对照代码和这几天的问题审查。
- 改了什么：
  - 新增 `ARCHITECTURE.md`：系统总览、执行顺序和真实约束、模块、仓库外的东西、逐列数据字典（`extra` 各来源的键、从哪天起有、补没补）、状态机、接口、外部依赖、联动清单（带预期结果）、不能破坏的规则、验证手册、已知限制。
  - 新增本文件，补写全部历史提交。
  - 新增 `CLAUDE.md`：每次对话自动加载，规定改代码前读什么、提交时更新什么。
  - 新增 `docs_check.py`：把文档里的表、列、状态、打分类别、过滤规则名、接口、环境变量、模块、提交标题和代码逐项对比。
  - `docs_check.py` 加篇幅提醒（只提示不拦）：ARCHITECTURE.md 超过 1000 行、第 12 节某个子系统超过 150 行。讨论过按模块拆成多份文档（参考 Uber 的分层加载），现在不拆：只有 20 个代码文件，最容易漏的是跨模块的联动，拆开反而分散；到阈值再拆，联动清单和第 9 节始终留在主文档。
  - 新增 `hooks/pre-commit`、`hooks/commit-msg`（`git config core.hooksPath hooks` 启用）：改了代码没改 CHANGELOG、或 `docs_check.py` 不通过、或提交标题不在 CHANGELOG 里时拒绝提交。
  - 新增 `test_core.py`（进 git，不依赖个人规则）：`next_status` 表格测试、后台打分"最后一刻进来的职位"测试、两个连接并发写入测试。
  - 新增 `checks/ui_check.mjs`：网页脚本语法、页面启动无报错、投递链接每种情况。
  - 删除 `DONE.md`、`PITFALLS.md`：现状并入 ARCHITECTURE.md，规则并入第 9 节，历史并入本文件。
  - 修 bug：邮件补抓走 LinkedIn 建出来的帖子没有记 `quick_apply`（写文档核对 extra 各来源时发现）。
  - `gmail_sync.py` 模块说明里过时的状态规则改成指向 `db.next_status`。
  - README 改链接；"改 static 要重启网页"的说法改正（页面每次请求都从磁盘读，只有改 `web.py` 要重启）。
  - 文档初稿写完后再交给 Codex（gpt-6.2-sol，high）对照代码核对，按它的意见：
    - 修 bug：合并职位后 SQLite 复用被删的最大 id，后台打分可能把旧职位的分数写到新职位上。`score._save` 改成按 id + created_at 更新，`Background._seen` 也按这两个键记（Codex 在内存库里复现过）。
    - 改正文档里约 20 处和代码不符的细节（`visa_note` 是 tag、哪些失败不算 run 错误、Gemini 失败时游标照样推进、`extra` 哪些地方会改已有帖子、接口细节等），补了联动清单（合并后 id 复用、自动任务和手动操作同时改一条数据）和第 11 节已知限制。
    - 钩子改为核对暂存区快照（以前核对的是工作区）；除 `*.md` 外改任何文件都要求 CHANGELOG；提交标题按 git 的算法取。
    - `docs_check.py`：CHANGELOG 按完整的 `## 标题` 行核对（以前是子串）、`GET /` 不再写死、环境变量也认 `os.getenv` 和单引号、模块表反向检查已删除的文件（不进 git 的文件除外）、核对过滤规则和打分类别的中文名。
    - 测试改成在对应 bug 回来时一定失败：每个测试都把 bug 临时加回去验证过（去掉最后一遍检查、去掉 BEGIN IMMEDIATE、只按 id 写分数、EMAIL_STATUSES 少一个、关闭邮件结束面试中的申请）。
    - `ui_check.mjs`：核对完整地址和按钮文字；主要 4 种情况在库里找不到时算失败；加了"按邮件建的职位"和"不知道是不是一键投但有官网链接"两种情况；每段脚本单独检查语法。
  - 改正 `index.html` 里一条错误注释：自动重新加载后打开着的详情会关掉（之前注释说会重新打开）。
- 文件：ARCHITECTURE.md、CHANGELOG.md、CLAUDE.md、docs_check.py、hooks/、test_core.py、checks/ui_check.mjs、score.py、email_lookup.py、gmail_sync.py、static/index.html（只改注释）、README.md、README.zh-CN.md；删除 DONE.md、PITFALLS.md
- 数据：无
- 验证：`docs_check.py` 通过，并故意改坏文档（删一列、改错接口名）确认会报错；`test_core` 7 个测试通过，5 个 bug 逐个加回去都能测出来；`test_rules` 通过；`ui_check.mjs` 对正式服务通过（8 种情况，库里没有"不知道是不是一键投但有官网链接"的职位）；`node --check` 能抓到重复的 `const`；两个钩子做过语法检查和提交标题提取测试；篇幅提醒用加长的快照试过会报出来。
- 遗留：ARCHITECTURE.md 第 11 节列出的已知限制都没修（撤销检查不是原子的、`--reprocess` 不拿锁、浏览器 profile 没有跨进程锁、测试隔离不完整、取全文 404 会中断同步）。

## Open quick-apply jobs in the SEEK / LinkedIn app; choose Chrome by link destination
2026-11-27 · f1dc1c4
- 起因：用户反馈一键投也跳到了 Chrome；之后 SEEK 一键投在 iPhone 上打开的是内嵌浏览器而不是 SEEK App。用户指出投递按钮一共 4 种情况，每次都只改了其中几种。
- 改了什么：iPhone 上按链接去向决定怎么打开：去 SEEK / LinkedIn 的用普通 https（有 App 就开 App），去公司官网的才用 `googlechromes://`。SEEK 一键投改链接职位页 `/job/<id>`（SEEK 的 apple-app-site-association 写了 `NOT /job/*/apply`）。`apply_url` 只存外部投递的官网地址。
- 文件：static/index.html、seek.py、email_lookup.py、DONE.md、PITFALLS.md
- 数据：给 108 条缺 `quick_apply` 的老 SEEK 帖子补标记（补完 61 条一键投、60 条外部，2 条查不到）；给 12 条 LinkedIn 不登录模式老帖子补标记（11 条补上，1 条被 429 限流）；删掉 SEEK 一键投帖子上残留的 `/apply` 形式的 `apply_url`。
- 验证：从库里每种情况各挑一个真实职位（#234、#606、#543、#589、#259、#85），在模拟 iPhone 和电脑上检查按钮地址和打开方式，页面无报错。是否真的拉起 App 需要真机确认，电脑上测不了。
- 遗留：LinkedIn 那 1 条没补上。

## One status model for jobs and emails
2026-11-27 · ea27c52
- 起因：一封"职位已关闭"邮件建出来的职位停在「新」；第一次修改成「已投」（错的），第二次改成「已关闭」。用户指出邮件分类和职位状态是两套对不上的东西，要么一一对应、要么统一。
- 改了什么：邮件分类直接输出职位状态（`db.EMAIL_STATUSES`：applied / assessment / interview / offer / rejected / closed；NULL = 没认出来，不改状态）；Gemini JSON 直接返回 `status`；所有状态变化走 `db.next_status`；网页邮件标签用职位状态的中文名。"职位已关闭"只结束还没进测评 / 面试的申请。
- 文件：db.py、gmail_sync.py、llm.py、email_lookup.py、web.py、run.py、static/index.html、DONE.md
- 数据：`emails.category` 改名 `status`，`viewed` / `other` 改成 `applied`（`db.connect` 迁移）。迁移前备份在 scratchpad 的 `jobs.backup-before-status.db`（临时目录，可能已被清理）。#598 先后被手动改成 applied、closed，时间线各记了一条说明。
- 验证：`test_rules.EmailStatus` 9 种转换；规则分类"已查看 → applied"、"已关闭 → closed"；迁移后邮件状态分布 applied 63 / rejected 7 / closed 3 / NULL 6；iPhone 尺寸下 #598 邮件标签显示「已关闭」；跑了一次 Gmail 同步。

## English README; keep the Chinese one as README.zh-CN.md
2026-11-27 · 06a0190
- 起因：用户要英文 README，中文保留。
- 改了什么：README.md 改成英文，原中文版另存 README.zh-CN.md，互相链接；补上 `email_lookup.py`；注明 `searches.toml` / `filters.toml` 不进 git。
- 文件：README.md、README.zh-CN.md
- 数据：无

## Background scorer: scan once more after scraping ends
2026-11-27 · b7d2c2e
- 起因：手动跑 SEEK 时新增的 1 个职位没有被打分。
- 改了什么：`score.Background._loop` 收到停止信号后再查最后一遍。
- 文件：score.py
- 数据：漏掉的 #608 手动补打（0 分）。
- 验证：数据库副本上模拟"抓取结束前 1 秒进来一个职位"，能打上分。
- 遗留：这个 bug 是上一次加后台打分时引入的，当时只测了"中途进来的职位"。

## Mark a job applied when an application email arrives for it
2026-11-27 · c403d22
- 起因：邮件补抓按"职位已关闭"邮件建的职位停在「新」，还显示"不投 / 已投"按钮。
- 改了什么：viewed / closed / other 邮件把 new / shortlisted / skipped 的职位改成 applied。**这是错的修法**，下一个提交（One status model…）改正。
- 文件：gmail_sync.py
- 数据：#598 改成 applied。

## Take the write lock up front when ingesting a posting
2026-11-27 · f0c4b2d
- 起因：10:06 那轮 SEEK 开始 6 秒就报 `database is locked`，整轮中断，网页顶部一直标红。
- 改了什么：`Ingestor.add()` 不在事务里时先 `BEGIN IMMEDIATE`。原因：审查修复（0630b33）加的 SAVEPOINT 让"先读后写"在延迟事务里进行，升级写锁时别的连接在写就立刻报错，不走 30 秒等待；新加的后台打分让撞上的概率变大。
- 文件：ingest.py、PITFALLS.md
- 数据：无
- 验证：新增并发测试（另一个连接占着写锁时 add 等它释放），旧代码立刻失败、新代码通过；之后手动跑 SEEK，73 条正常。

## Score new jobs in the background while scraping; auto-reload stale pages
2026-11-26 · 72fbeb3
- 起因：用户看到新职位很久都没有分数——打分在一轮抓取（7–10 分钟）结束后才统一进行。另外手机上的 PWA 一直跑旧代码，「待打分」标签没出现。
- 改了什么：`score.Background` 每 5 秒查一次新入库的待打分职位，最多 3 个并发；`run.py` 推送前等它打完。`/api/summary` 返回 `ver`（index.html 修改时间），页面发现变了就重新加载（输入中、开着弹层时不加载）。
- 文件：score.py、run.py、web.py、static/index.html、DONE.md
- 数据：无
- 验证：数据库副本上模拟抓取中途进来新职位，两个都打上分；版本检查测了三种情况（没更新不重载、更新后重载一次、之后不再重载）。
- 遗留：没测"最后一刻进来的职位"，后来出了 bug（见 Background scorer: scan once more…）。

## Open job links in the Chrome app on iOS; show a pending-score chip
2026-11-26 · 577dc42
- 起因：主屏幕 PWA 里点职位链接开在系统内嵌浏览器，用户要在 Chrome App 里打开；用户看到一个新职位没有分数。
- 改了什么：iPhone 上职位 / 投递链接改成 `googlechromes://`；没打分的待处理职位显示「待打分」。
- 文件：static/index.html
- 数据：无
- 遗留：一键投也被改成了跳 Chrome（错的），f1dc1c4 改正。改的时候新加的 `isIOS` 和已有变量重名，整页脚本报错，部署前测出来修掉了。

## Score with a minimal agy agent to drop the built-in system prompt
2026-11-26 · 5454f97
- 起因：用户问能不能屏蔽 agy 自带的提示词（只回 "ok" 也要 1 万 token）。
- 改了什么：`~/job-match/.agents/agents/scorer/agent.md`（`excludeDefaultComponents: true`，不带工具）；`score.py` 用 `--agent scorer`，把 AGENTS.md 放进消息；不用 `--json-schema`，JSON 格式写在提示里自己解析。
- 文件：score.py、DONE.md、PITFALLS.md
- 数据：无
- 验证：6 个职位实测：1 轮、约 5k token（原来 15–17k）、10–16 秒，分数和之前一致。

## Score jobs with agy instead of local embeddings; quick apply and direct apply links
2026-11-26 · 7e1b73f
- 起因：embedding 打分效果差（分不出"能做但简历过不了"的职位）；用户要用 agy 打分、同意发送完整简历；要 SEEK 的 Quick apply 也抓下来；要能直接跳到投递页，不用再去网站里点。
- 改了什么：
  - `score.py`：按 `~/job-match/AGENTS.md`（简历 + 规则）打 0–100 分和四类 track；普通开发类没有一键投的排序减 25 分。打分规则和用户反复讨论过：核心是"这份简历不改叙事能不能过 ATS"，同生态缺口不扣分，没写工资不扣分。
  - 删除 embedding 打分（`match.py`、`job_vectors`、`match_score` / `match_high`）。
  - LinkedIn Easy Apply 和 SEEK Quick apply 统一叫 `quick_apply`（「一键投」）。
  - 官网链接：LinkedIn 登录模式读 safety/go 链接；SEEK 用 `seek_links.mjs` 登录后打开投递页记下跳转地址。
  - 网页：卡片显示分数，详情显示「匹配度」。
  - 同一提交里还有前一次会话留下的网页修复：所有请求加超时（查看 15 秒、修改 20 秒），点开职位时详情立刻打开显示加载中、失败可重试（起因：4G + Tailscale 下点职位后整个页面像卡死）。
- 文件：score.py、run.py、db.py、web.py、seek.py、seek_links.mjs、linkedin.mjs、linkedin_guest.py、email_lookup.py、ingest.py、static/index.html、文档；删除 match.py
- 数据：`db.connect` 删掉 embedding 的表和列、`easy_apply` 改名 `quick_apply`；迁移前备份在 scratchpad 的 `jobs.backup-before-agy.db`（临时目录）。待处理的 13 个职位打了分；待处理里的 SEEK 帖子补了 `quick_apply`；3 个 LinkedIn 外部投递职位补了官网链接。系统层面：停用 Ollama 服务、删除模型和 `/etc/systemd/system/ollama.service.d`。
- 验证：agy 额度和速度实测（单个 12–20 秒，3 并发不限速，约 36 次调用用掉 5 小时额度的 5%、每周额度的 1%）；30 个职位的分数和用户实际选择基本一致；手机尺寸截图检查列表和详情。

## Fully remove sheet scroll layer when closed (iOS touch freeze)
2026-11-26 · cc55ae4
- 起因：用户在列表里点开一个职位后整个 App 卡住，重新打开才恢复。
- 改了什么：底部弹层的全屏滚动层关闭时 `display:none`（之前只是 `visibility:hidden`，iOS 上可能仍吃掉触摸）。
- 文件：static/index.html、PITFALLS.md
- 遗留：用户补充"蓝色选中效果出现后卡死"，后来判断更可能是请求没有超时，修复并入了 7e1b73f。

## Fix data-loss and race bugs from full code review
2026-11-26 · 0630b33
- 起因：Codex（gpt-6.2-astra，low）全仓审查。
- 改了什么：（同一提交里还带进了底部弹层改用原生 scroll-snap 的改动，见 Block companies… 一条。）入库单条失败整条回滚（SAVEPOINT）、合并带邮件 / 向量的职位、`--rebuild` 可回滚、Gmail 只有 404 才跳过且失败不推进游标、history 失效按上次成功时间回补、邮件事件用收信时间、补抓不覆盖手动处理且只补有职位名的邮件（出错 6 小时后重试）、无关邮件只存 id、写请求要求同源 Origin、请求体大小和读超时、推送订阅只收浏览器厂商地址、合并参数校验、撤销带期望状态（409）、详情 / 列表只用最后一次请求的响应、左滑提交失败恢复卡片、拉黑名单加锁、故障推送送达后才冷却、私人文件 600 + `UMask=0077`。
- 文件：ingest.py、gmail_sync.py、email_lookup.py、web.py、push.py、rules.py、notify_failure.py、static/index.html 等
- 验证：新增回滚、合并带引用、Gmail 超时等测试。
- 遗留：审查里有意不修的项：网页鉴权依赖 Tailscale ACL、Gemini 显式开关等。**这次加的 SAVEPOINT 后来引出了 database is locked（f0c4b2d 修）。**

## Block companies from the UI, swipe-down to close sheets
2026-11-25 · 193bde8
- 起因：用户要能拉黑公司（比如 Jobgether）；底部弹层只能点外面关，不能往下拉。
- 改了什么：详情「⋯」→ 拉黑公司（`blocked_companies.json`，按修改时间自动重读），「我的」页可取消；底部弹层可下拉关闭。
- 文件：rules.py、ingest.py、web.py、static/index.html、.claude/launch.json 等
- 遗留：这一版是 JS 跟手指的下拉关闭，手感发涩，几次 JS 调整用户都不满意。改用原生 scroll-snap 的版本没有单独提交，随 0630b33 一起进了仓库。

## Local match scoring, 我的 profile tab, swipe-to-skip, stricter visa filter
2026-11-25 · b60dab0
- 改了什么：本地 embedding 打分（已于 2026-11-26 删除）；「我的」页（点一行复制投递用的个人信息）；待处理左滑不投 + 撤销；「想投」按钮去掉；修左滑时列表重绘卡住；LinkedIn 卡片职位名带 "(Verified job)" 时公司 / 地点错位；职级豁免支持例外；签证规则扩写（要求公民 / NV1 / NV2 / AGSVA 等的直接过滤）。
- 文件：match.py（已删）、web.py、static/index.html、linkedin.mjs、rules.py、文档

## Fix run.py crash on startup and push an alert when a scheduled run crashes
2026-11-25 · 0f0aa7f
- 起因：`run.py` 用了 `rules.config_path()` 却没 import，每次定时抓取一启动就崩，自己的"出错推送"也走不到。
- 改了什么：补 import；已处理并推送过的错误退出码改 2（systemd 视为成功）；新增 `notify_failure.py`（OnFailure 调用，同一单元每小时最多推一次）。
- 文件：run.py、notify_failure.py

## Job search scraper: SEEK + LinkedIn ingest, rule filters, Gmail sync, PWA with push
2026-11-25 · 505527d
- 初始版本：SEEK 接口、LinkedIn 登录 / 不登录两种模式、规则过滤、跨平台去重、Gmail 只读同步 + 规则 / Gemini 分类、邮件补抓、网页 + PWA + Web Push、systemd 定时任务。2026-11-22 起开始跟踪（Gmail 第一次只同步当天）。
