# 已完成

截至 2026-11-22。

## 抓取

- **SEEK**（`seek.py`）：调前端 JSON 接口，不需要登录；按发布时间倒序翻页，只给新职位拉 JD。
- **LinkedIn 登录模式**（`linkedin.mjs` + `browser.mjs`）：本机 Chrome + 专用登录 profile；库里已有的不点开；按结果数翻完所有页；remote 搜索用 LinkedIn 的 remote 参数（`f_SAL`）缩小范围、再按卡片地点筛；带随机停顿模拟人工浏览。
- **LinkedIn 不登录模式**（`linkedin_guest.py`）：登录失效时自动切换，走公开接口。
- **搜索范围**（`searches.toml`，个人配置不进 git，格式见 `searches.example.toml`）：职位关键词 × 地区 × SEEK、LinkedIn。
- **搜索条件核对**：SEEK 参数和账号里的 Saved searches 一致、结果数和网页一致；LinkedIn 参数没有被页面删掉。

## 存储 / 过滤 / 去重

- **SQLite**（`db.py`，`jobs.db`）：job（真实职位）和 posting（平台帖子）分开；状态时间线 `events`；申请问答 `application_qa`；过滤结果 `filter_results`；疑似重复 `dup_candidates`；抓取记录 `runs`；推送订阅 `push_subscriptions`。
- **过滤**（`filters.toml` + `rules.py`；个人规则不进 git，格式见 `filters.example.toml`）：被过滤的职位记下原因，不删除，网页上可以放行。
  - AI 标注 / 众包平台（30 家，含 TaskVerse、Crossing Hurdles、Great Value Hiring、Scale AI、Turing 等）
  - 按 JD 识别 AI 训练零工（按小时计价 + 评估 AI 输出）
  - IT 外包（Capgemini、Cognizant、Spait、Infosys、TCS 等）
  - 职级高于 senior（Principal / Staff / Lead / Head / Director / Manager…），Forward Deployed Engineer 例外
  - PhD（职位名，或 JD 硬性要求）
  - 工作身份要求（按 JD 正则）：可配置为过滤或只打标签
  - 中介：低优先级的降权，普通中介打标签
- **去重**（`ingest.py`）：同平台按 id；跨平台按 JD 相似度 + 公司 / 职位名；拿不准的标疑似重复，网页上确认；合并后优先展示非中介的帖子。
- **测试**：`python3 -m unittest test_rules`（依赖个人规则，不进 git）。

## 匹配分（`match.py`）

- 本地 Ollama + Qwen3-Embedding-0.6B（只用 CPU、最多 4 核、最低优先级，配置在 `/etc/systemd/system/ollama.service.d/cpu-only.conf`）。
  简历在 `resume.txt`（不进 git），不出本机。
- 每个职位两个向量（职位名 + JD、职位名），加 JD·简历、职位名·目标方向两个相似度，用你的「已投 / 不投」训练逻辑回归；每天重训。
- 分数线：交叉验证下让 90% 投过的职位在线以上。**不隐藏任何职位**：待处理里线以上一组在前、线以下一组在后（中间有分隔线、卡片标「低匹配」），
  两组内都按时间排；推送先列线以上的。
- 离线验证（2026-11-25，正 28 / 负约 160）：AUC ≈ 0.82，前 20 名里平均 11 个投过的（随机约 3 个）；线以下约一半是你会不投的。
  试过按句"要求覆盖度"、去掉 JD 套话，都没有提升。

## 网页

- `web.py` + `static/index.html`，只用标准库，本机 http://127.0.0.1:8765。
- **手机优先的界面**：底部标签栏（待处理 / 进行中 / 已结束 / 已过滤）；职位卡片直接显示发布时间、来源、薪资、Easy Apply、标签；
  详情全屏滑入，底部操作栏按状态给出常用操作（新：想投 / 不投 / 已投；已投：笔试 / 面试 / 被拒…），其余在「⋯」里；
  改状态有提示 + 撤销，职位移出当前列表时自动回到列表；点「已投」弹出可跳过的问答面板；备注失焦自动保存；JD 折叠；
  下拉刷新、回到前台 / 每分钟自动刷新；搜索默认搜全部职位；「⋯」里是通知设置、全部职位、抓取状态。电脑上是两栏布局。
- 详情里还有：原帖链接、过滤原因和放行、疑似重复合并、问答、时间线、命中的搜索。
- **手机访问**：`tailscale serve` 发布在 https://<主机名>.<tailnet>.ts.net:8443/（只限 tailnet 内）。
- **PWA + 推送**（`push.py`、`static/sw.js`、`static/manifest.webmanifest`）：手机已添加到主屏幕并订阅成功，后台能收到推送。

- **我的**（第 5 个标签）：投递时要填的个人信息，点一行复制、可增删改。存在库里 `kv.profile`；初始内容来自不进 git 的 `profile.json`。

## 自动运行（systemd user，开了 Linger，开机就跑）

- `auto-job.timer` → `auto-job.service`：每天 8:00–22:00 每小时一次；SEEK 和 LinkedIn 每次都跑（2026-11-23 起 LinkedIn 从每 2 小时改成每小时）；有未过滤新职位或出错时，弹桌面通知并推送到手机。
- `auto-job-mail.timer` → `auto-job-mail.service`：全天每分钟用 historyId 查一次有没有新邮件（没有就直接退出、不记 runs），有才完整同步；和每小时那一轮用 `.gmail.lock` 文件锁互斥。
- `auto-job-web.service`：网页常驻。
- `tailscale serve --https=8443`：后台常驻，重启后保留。

## Gmail（只读）

- GCP Personal mode 项目 （项目 ID 见 LOCAL.md），只启用 Gmail API，Desktop client，只申请 `gmail.readonly`。
- `gmail.py`：本地回调 + PKCE 授权，refresh token 存 `token.json`（600，不进 git）。
- **同步**（`gmail_sync.py`，接在 `run.py` 抓取之后，每小时跑）：第一次只拉当天（2026-11-22），之后按 historyId 增量。
  - 预筛只看发件人 + 标题，不相关的邮件不存、不发给 Gemini；LinkedIn 只认 jobs-noreply@linkedin.com。
  - 规则分类：SEEK（已提交 / 不太可能进一步 / 已查看 / 职位关闭）、LinkedIn（已发送 / 已查看）模板，具体的拒信措辞，标题里明确的结果。
  - 只有正文线索、或标题是 Update / Outcome / Next steps 的交给 Gemini（`llm.py`，gemini-flash-lite-latest，结构化输出），
    月度预算上限 $8 做在代码里（按偏高单价估算）。60 天样本里 37 封交给 Gemini，约 $0.011。
  - 按公司名 + 职位名匹配职位；状态只往前推（确认→已投，拒信→被拒，笔试 / 面试 / offer），写进时间线；
    拒信 / 笔试 / 面试 / offer 推送到手机。匹配不上的进「未归类」。
- **邮件补抓**（`email_lookup.py`，Gmail 同步后自动跑）：匹配不上的邮件按公司 + 职位名去 SEEK / LinkedIn（公开接口，不用账号）搜，
  公司对上、职位名够像才入库并关联；两边都搜不到时按邮件本身建职位（source=email，链接指向这封邮件）；
  每封只补抓一次，结果记在 `emails.lookup_at / lookup_result`；公司名都没有的才进「未归类」。
- **预筛**：除了招聘系统发件人 / 标题关键词，像真人来信的（Gmail「主要」标签页或没有群发特征）再看正文。
  漏掉的邮件可以重新处理：`python3 gmail_sync.py --reprocess "after:2026/11/22 -in:sent"`。
- **网页**：职位详情有「相关邮件」（点开跳 Gmail）；有未归类邮件时「⋯」上有红点，里面可以搜职位关联或忽略。

- **搜索**：只搜当前页；整词匹配（"rag" 不命中 storage）；标题 / 公司命中的排前面，只在 JD 里出现的标「JD 提到」。
- **进行中**：已投 / 笔试 / 面试 / offer 的职位不管有没有被过滤规则命中都显示（投了说明你决定要它）。

## 还没做
- 按职位名只保留工程类岗位（目前 Data Administrator、Martech Manager 之类还会进来）。
- 职位下架后自动标记。
- 2026-11-25：签证规则扩写，要求公民 / PR / NV1 / NV2 / TSPV / Baseline / AGSVA 的直接过滤（"Australian or New Zealand citizen"、"open to … citizens"、"Eligibility: Australian Citizens" 等写法）；"Citizenship preferred"、"eligibility for security clearance" 只打标签。修了快速连续左滑时卡片卡住的问题。
- 2026-11-25：拉黑公司。职位详情「⋯」→ 拉黑「公司」，这家以后的职位直接过滤、现有的移到「已过滤」，可撤销；「我的」页底部列出拉黑的公司，可以取消。名单存在 `blocked_companies.json`（不进 git），网页和定时抓取都读它。
- 2026-11-25：底部弹层（改状态、设置、编辑等）可以往下拖关掉：拖过 80px 或快速一甩；弹层内容没滚到顶时照常滚动，在输入框里拖不会关。
- 2026-11-25：底部弹层改用浏览器原生滚动吸附（scroll-snap）实现下拉关闭，拖动、惯性、吸附都交给浏览器，不再由 JS 跟手指。
- 2026-11-25：按 Codex 全盘审查修了一批 bug：单条入库失败整条回滚、合并带邮件/向量的职位、`--rebuild` 失败可回滚、Gmail 临时故障不再永久漏信（只有 404 才跳过、失败不推进游标、history 失效按上次成功时间回补）、邮件事件用收信时间、补抓不覆盖手动处理且只补有职位名的邮件（出错 6 小时后重试）、无关邮件不再存正文、网页写请求必须同源 Origin、推送订阅只收浏览器厂商地址、合并参数校验、撤销遇到状态已变不覆盖、详情/列表只用最后一次请求的响应、左滑提交失败会恢复、拉黑名单加锁、故障推送送达后才进入冷却、私人文件 0600 + systemd UMask=0077。
