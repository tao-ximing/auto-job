# 踩过的坑

按领域分组。每条：现象 → 原因 → 现在的处理。

## 登录 / 浏览器

- **SEEK 两边一起被登出**：把日常 Chrome 的 cookie 复制到脚本 profile，结果两边都掉线。
  SEEK 用 Auth0 refresh token 轮换，两个客户端共用一个会话时，一边刷新会让另一边的旧 token 被判为盗用，整个会话作废。
  → 脚本用专用 profile（`~/.cache/auto-job-profile`），里面单独登录；`node browser.mjs login` 重新登录。
- **Playwright 启动后 cookie 被删**：Playwright 默认带 `--password-store=basic`，Chrome 解不开系统 keyring 加密的 cookie 会直接删掉。
  → `browser.mjs` 去掉这个默认参数，改用 `gnome-libsecret`。
- **Google 登录拒绝自动化浏览器**（"This browser or app may not be secure"）。→ 登录时用普通方式启动 Chrome，不经 Playwright。
- **headless 被 Cloudflare 拦**（旧版 headless、当时用浏览器抓 SEEK 的时候）。→ 曾经总是有界面，窗口放到屏幕外。
- **Wayland 不让程序指定窗口位置，"屏幕外"的窗口每小时弹到桌面上**，被人随手关掉后整轮 LinkedIn 中止，
  还被误报成"登录失效"（2026-11-23）。→ 抓取时先起一块 Xvfb 虚拟屏幕，Chrome 照常有界面运行但画在虚拟屏幕上（去掉 WAYLAND_DISPLAY、
  `--ozone-platform=x11`），抓完连同 Xvfb 一起关。没装 Xvfb 时退回新版 headless（UA 换成普通 Chrome）。
  仍然需要登录桌面：cookie 用系统 keyring 加密，keyring 要登录后才解锁。
- **点得太快**。→ `linkedin.mjs` 加了鼠标移动、滚轮、按 JD 长度停留、随机长停顿；SEEK 拉 JD 间隔随机 1–3 秒。

## LinkedIn

- **新版搜索页只认 24h / 一周 / 一个月的 `f_TPR`**，`r3600` 之类会被忽略；结果按相关度排，不能按时间排；看过的职位隐藏发布时间。
  → 取 24h，逐个点开读发布时间，自己换算成绝对时间。
- **LinkedIn 的 remote 筛选参数换了**：旧的 `f_WT=2` 加载后会被页面从 URL 删掉，旧版 `/jobs/search/` 也会跳到新版。
  新参数是 `f_SAL=f_SA_id_225001:272001`（用户在页面上点 Remote 后从 URL 里抄出来的；我用脚本点隐藏的复选框没触发生效，一度误以为没有可用参数）。
  它比较宽松：结果里仍有一半以上是 Hybrid / On-site；但实测加不加，Remote 职位集合完全一样，扫的卡片少约 1/3。
  → AU remote 搜索带 `f_SAL` 缩小范围，再按卡片地点 "(Remote)" 严格筛，并**按页面结果数翻完所有页**（最多 8 页）。
  关键词写 "remote software engineer" 只是相关度加权，不要用。公开接口不认 `f_WT` 也不认 `f_SAL`，只保留地点是 "Australia" 的（保守，会漏）。
- **超过点开上限的职位被存进库却没有 JD，之后再也不会被点开**。→ 超过上限的不入库，下次运行再点开。
- **登录失效 / 没有桌面会话时整轮没数据**。→ `linkedin.mjs` 退出码 3，`run.py` 自动改用公开接口（`linkedin_guest.py`）；公开接口每页 10 条，429 就停。
- **点开太多会给账号带来风险**。→ 卡片上的 job id 点开前就能读到，库里已有的不点；每条搜索最多点开 40 个。2026-11-23 起应用户要求从每 2 小时改成每小时，留意有没有出现验证码 / 登录失效。

## 过滤 / 去重

- **公司名子串匹配误伤**："Talent" 会命中所有带 talent 的公司，"Turing" 也一样。→ 规范化后按别名完全相等匹配。
- **职位名在 " - " 处截断，导致同公司不同团队的岗位被合并**（Quantium 两个 Senior Data Scientist）。→ 只去掉地名、Remote 之类噪音词。
- **同公司 JD 套话多，不同岗位 JD 相似度也有 0.4–0.5**，被误报疑似重复。→ 职位名不同时阈值提到 0.6。
- **职级规则误伤 FDE**：Forward Deployed Engineer 不管什么级别都可以投。→ `exempt_patterns` 豁免 forward deployed / FDE。
- **PhD 规则**："BS, MS, or PhD in…" 只是可选项，不能过滤；单独一行 "A PhD in…" 才是硬性要求。
- **AI 训练零工换名字就漏网**。→ 除了公司名单，加了按 JD 判断：同时出现按小时计价 + 评估 AI 输出 / RL environment / benchmark。
- **删除旧职位后，下次抓取会当新职位重新加回来**。→ 不删行，改状态（closed / skipped）。
- **`ingest.py --rebuild` 会清掉手动状态**。→ 改过滤规则用 `--refilter`（只重算过滤）；`--rebuild` 检测到手动操作会拒绝执行。

- **投了的职位被过滤规则命中后，「进行中」里看不到**（比如职位名带 Lead、JD 要求公民身份，但你还是投了）。→ 进行中的视图不看 filtered。
- **搜索按子串匹配 JD 全文，结果几乎是全部**（"lead" 命中 leadership，"rag" 命中 storage）。→ SQL 粗筛 + Python 整词匹配，标题命中排前。

## 定时任务 / 通知

- **systemd user 服务里跑带界面的 Chrome**：需要图形会话的环境变量（GNOME 登录时会导入）；开了 Linger 后没登录桌面也会触发，此时 LinkedIn 登录模式不可用，自动退到公开接口。
- **每小时通知太吵、LinkedIn 跳过那小时还重复报上一轮的结果**。→ 只统计本轮（`started_at >= t0`），只在有未过滤新职位或出错时通知。
- **测试时伪造的"登录失效"留在 runs 表里，网页一直标红**。→ 测完要清掉。

- **抓取攒很多条才 commit，长时间占着写锁，网页上点按钮报 database is locked**（LinkedIn 登录模式约 25 秒一条）。→ 每条入库后立刻 commit。

## 网络 / 手机

- **端口冲突**：8790 被另一个 node 程序占用；`tailscale serve` 的 443 也被它占着。→ 网页走 tailnet 的 8443。
- **这台机器自己不走 MagicDNS**，curl `…ts.net` 报解析失败。→ 测试时用 `--resolve 域名:8443:<本机 tailnet IP>`。
- **手机打不开 `…ts.net`（ERR_NAME_NOT_RESOLVED）**：手机 Tailscale 没开。
- **不能用 100.x 的 IP 代替域名**：Web Push / service worker / PWA 要求 HTTPS + 有效证书，Tailscale 只给 `…ts.net` 域名签证书。
- **经 tailscale serve 访问后写操作全被拒**：同源检查只认 `http://`，代理后是 `https://`。→ 改成比较 origin 的 host 和 Host 头。
- **iPhone 推送**：iOS 16.4+，必须用 Safari「添加到主屏幕」、从图标打开才能申请通知权限；Claude App 内置浏览器打不开、也不支持 service worker。
- **主屏幕 Web App 不会自己刷新**：没有地址栏和刷新按钮，切到后台再回来 iOS 只恢复旧画面，改了状态手机上看不到。
  → 回到前台（visibilitychange / pageshow）自动刷新、每分钟刷新列表（输入时不刷、不重画详情）、顶部加「刷新」按钮。
- **改状态不会推送**：推送只在抓取发现新职位时发，手动 / 脚本改状态（比如重新打开）不会提醒。
- **VAPID 的 sub**：Apple 要求 mailto: 或 https:，用的是 tailnet 网页地址，没放个人邮箱。

## Gmail / Gemini

- **发来的 `credentials.json` 是模板**（`YOUR_CLIENT_ID_HERE`）；`adc.json` 是 gcloud ADC，只有 cloud-platform 等权限，调 Gmail 返回 403，而且权限过大，已删除。
- **新建的 OAuth client 只在创建时能下载 secret**：弹窗关掉就再也看不到，要当场 Download JSON。
- **`gmail.readonly` 属于 restricted 权限**。External 用户类型下：Testing 的 refresh token 7 天过期；
  切 In production 要先在 Branding 填首页和隐私政策链接（必须在自己的授权域名上），授权时还会有未验证警告。
  → **用 Personal mode**（Google 官方的个人用途用户类型，引导页 `console.cloud.google.com/auth/overview/personal`）：
  只有创建者本人能授权，不需要验证，没有 7 天过期，也没有未验证警告。只能在新建项目时选，External 项目不能改过去。
  现在用的是 Personal mode 项目 （项目 ID 见 LOCAL.md）；旧 External 项目里的 client 已删除。
- **两个 Google 账号**：浏览器里登录了两个 Google 账号，Personal mode 只认创建项目的那个（具体账号见 LOCAL.md）。
- **Claude in Chrome 连不上**：扩展登录的 Claude 账号和 App 不一致时，`list_connected_browsers` 是空的；用户重新登录扩展后才连上。
- **邮件分类的坑**：
  - SEEK 确认邮件正文第一句后面没有句号（`...submitted to Acacia Energy seek [`），公司名会一路匹配下去 → 在 " seek" / "[" / "." 处截断。
  - LinkedIn 的 "Your application to X at Y" 纯文本正文几乎是空的 → 纯文本和 HTML 取更长的那份。
  - 确认邮件里常有 "unsuccessful applicants will be notified"、"later stages may include an interview" → 拒信只认具体措辞；
    正文里出现的面试 / 笔试 / 确认线索不直接采信，交给 Gemini 确认。
  - LinkedIn 发信地址很多：jobs-noreply 才是申请相关；jobalerts / invitations / messages / inmail / updates 都是噪音。
  - 和求职无关的政府 / 机构受理邮件也叫 "Acknowledgement of Application Received" → 在 filters.toml 的 `[gmail] extra_ignore_subject` 里排除。
  - 有的邮件对应的职位不在库里（不是通过 SEEK / LinkedIn 搜到的）→ 进「未归类」，手动关联或忽略。
- **招聘方本人发的信被预筛丢掉**（面试后的拒信：发件人是个人邮箱，标题只是 "Senior Engineer - 公司名"）。
  → 发件人 / 标题看不出来时，Gmail 放在"主要"标签页、或没有群发特征的信，再看正文有没有 application / interview / role 等词。
  不能只靠"没有 List-Unsubscribe"判断真人来信：招聘外联工具会给本人写的信也加退订头。
- **补处理漏掉的邮件时别用 newer_than:7d**：会把跟踪开始（2026-11-22）之前的邮件也处理进来、改职位状态。→ 用 after:2026/11/22。
- **`GEMINI_API_KEY` 只在 `~/.bashrc` 里**，非交互 shell 和 systemd 读不到。→ 用到时从环境变量 / `.bashrc` / `.env` 读。

- **主屏幕 PWA 里点外链会开在系统内嵌浏览器，不是 Chrome**（`target=_blank` 也一样）。→ iPhone 上把职位 / 投递链接的 `https://` 换成 `googlechromes://`，直接拉起 Chrome App。
- **iOS 输入框字号 < 16px，聚焦时页面自动放大且不缩回**。→ input / select / textarea 统一 16px。

## 匹配分 / agy

- **打分提示里只放简历、不写硬条件，模型只看技术匹配**：纯股权的 Co-Founder / CTO 被打了 75 分。→ 硬条件和"简历能不能过筛"的标准写进 `~/job-match/AGENTS.md`。
- **agy 每次调用自带约 1 万 token 的系统提示词**（只回 "ok" 也要 10k）。→ 自定义 agent 写 `excludeDefaultComponents: true`，降到几百。
- **精简 agent 配 `--json-schema` 会反复重试**（4 轮、35 秒）：结构化输出靠默认组件，被一起去掉了。→ 不传 `--json-schema`，格式写进提示、自己解析。
- **精简 agent 不读 AGENTS.md**（`excludeDefaultComponents` 连规则文件一起去掉）。→ 内容由调用方放进消息。
- **"能做"不等于"能投"**：data engineer 能做，但这份 AI 方向的简历过不了 ATS。打分要问"不改叙事能不能过筛"，不是"有没有能力"。
- （已删除的 embedding 方案）召回够、精度不够：投过的都在线以上，但线以上大部分仍要手动跳过。

## 环境

- **Python 3.14 没有 pip**。→ 全部只用标准库 + 系统自带的 `cryptography`、`PIL`。

## 左滑时重绘列表会把卡片"卡"在半路（2026-11-25）
左滑不投后 220ms 会重绘列表。如果这时手指正在滑下一张卡，那张卡的 DOM 被换掉，`touchend` 发到已脱离页面的节点上、冒泡不到列表，
结果卡片停在半路、这次不投也没发出去。现在手指按着时 `renderList` 只记一笔，松手后再画；`touchcancel` 也会把卡片弹回原位。


## 底部弹层用原生滚动吸附做，不用 JS 跟手指（2026-11-25）
JS 跟手指（touchmove 里改 transform）几种调法在 iPhone 上都发涩：每一帧都要等主线程，Safari 网页还限 60 帧。
现在弹层放进全屏滚动容器 `#sheetWrap` = [透明垫片 100%] + [弹层]，`scroll-snap-type: y mandatory` 只有"打开 / 关闭"两个吸附点，
往下拉就是浏览器原生滚动（跟手、惯性、松手吸附都不经过页面 JS），滚到顶就收起。
- 弹层外框负责圆角裁剪（overflow: hidden），里面 `.sheet-body` 负责滚动。外框自己滚动时，内容在嵌套滚动下会露到圆角外面。
- 长内容先滚 `.sheet-body`，滚到顶后继续往下拉会自动接力到外层，把弹层拉下来。
- 无界面 Chrome 里 `Input.synthesizeScrollGesture` 不触发滚动；要测手势，用 CDP `Input.dispatchTouchEvent` 一步步发 touchStart/Move/End。

## Gmail 同步：只有 404 才能当"已删除"跳过（2026-11-25）
以前取单封邮件元数据时任何异常都当成已删除，一次超时就会让那封信永久漏掉（游标照样往前推）。
现在只有 HTTP 404 跳过，其他错误直接抛出，这一轮不推进 historyId。测试见 test_rules.py 的 Robustness。
- 弹层滚动层关闭时必须 `display:none`，不能只 `visibility:hidden`：iOS Safari 上隐藏的全屏滚动层可能仍吃掉触摸，整页点不动（2026-11-26，点开职位后整个 App 卡住）。
