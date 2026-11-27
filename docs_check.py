"""把 ARCHITECTURE.md / CHANGELOG.md 和代码逐项对比，对不上就报错（退出码 1）。提交前钩子（hooks/pre-commit）会自动跑。

核对的内容（文档里的写法见 ARCHITECTURE.md 对应章节）：
- 数据字典：每张表的列（`### 表 `xxx`` 下第一张表的第一列）和 db.py 实际建出来的表结构
- 职位状态（5.1）：db.STATUSES、db.STATUS_LABEL、db.EMAIL_STATUSES、网页 STATUS
- 打分类别（5.3）：score.TRACKS、网页 TRACK
- 过滤规则名（5.4）：rules.evaluate 里实际产生的规则名、网页 RULE
- HTTP 接口（第 6 节）：web.py 里实际的路由
- 环境变量（3.4）：代码里实际读取的环境变量
- 模块（第 2 节）：进 git 的每个 .py / .mjs / static 文件都要出现
- CHANGELOG.md：git 历史里每个提交的标题都要出现
- 篇幅提醒（不算错误）：ARCHITECTURE.md 超过 1000 行、第 12 节某个子系统超过 150 行

  python3 docs_check.py                                  # 核对工作区
  python3 <快照>/docs_check.py --root <快照> --repo <仓库>   # 核对暂存区导出的快照（提交前钩子这样用）
"""
import json, os, re, subprocess, sys

_args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
HERE = os.path.abspath(_args.get("--root") or os.path.dirname(os.path.abspath(__file__)))   # 读文件的地方
REPO = os.path.abspath(_args.get("--repo") or HERE)                                          # 跑 git 的地方
sys.path.insert(0, HERE)
errors = []


def read(name):
    return open(os.path.join(HERE, name), encoding="utf-8").read()


def git(*args):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if r.returncode:
        errors.append(f"git {' '.join(args)} 失败：{r.stderr.strip()}")
    return r.stdout


def section(md, title_re):
    """从标题（正则匹配整行）开始，到下一个同级或更高级标题为止。"""
    lines = md.splitlines()
    for i, l in enumerate(lines):
        m = re.match(r"(#+) ", l)
        if m and re.search(title_re, l):
            level, out = len(m.group(1)), []
            for l2 in lines[i + 1:]:
                m2 = re.match(r"(#+) ", l2)
                if m2 and len(m2.group(1)) <= level:
                    break
                out.append(l2)
            return "\n".join(out)
    errors.append(f"ARCHITECTURE.md 里找不到标题 {title_re!r}")
    return ""


def first_table(text, col=0):
    """返回第一张 markdown 表格每一行（去掉表头和分隔行）的单元格列表。"""
    rows, started = [], False
    for l in text.splitlines():
        if l.startswith("|"):
            started = True
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            rows.append(cells)
        elif started:
            break
    return rows[2:]


def ticked(cell):
    return re.findall(r"`([^`]+)`", cell)


def compare(what, doc, code):
    doc, code = set(doc), set(code)
    if doc - code:
        errors.append(f"{what}：文档里有、代码里没有：{sorted(doc - code)}")
    if code - doc:
        errors.append(f"{what}：代码里有、文档里没有：{sorted(code - doc)}")


def js_object_keys(html, name):
    m = re.search(r"const " + name + r" = \{(.*?)\};", html, re.S)
    if not m:
        errors.append(f"static/index.html 里找不到 const {name}")
        return {}
    return dict(re.findall(r"(\w+): '([^']*)'", m.group(1)))


arch = read("ARCHITECTURE.md")
html = read("static/index.html")

# ── 数据字典 ──
import db
conn = db.connect(":memory:")
code_tables = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
               for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
doc_tables = {}
for m in re.finditer(r"^### 表 `(\w+)`", arch, re.M):
    doc_tables[m.group(1)] = [ticked(r[0])[0] for r in first_table(arch[m.end():]) if ticked(r[0])]
compare("数据库表", doc_tables, code_tables)
for t in set(doc_tables) & set(code_tables):
    compare(f"表 {t} 的列", doc_tables[t], code_tables[t])

# ── 职位状态 ──
rows = first_table(section(arch, r"^### 5\.1 "))
compare("职位状态（5.1）", [ticked(r[0])[0] for r in rows], db.STATUSES)
compare("能由邮件设置的状态（5.1 最后一列）", [ticked(r[0])[0] for r in rows if r[-1] == "是"], db.EMAIL_STATUSES)
compare("db.STATUS_LABEL 的状态", db.STATUS_LABEL, db.STATUSES)
js_status = js_object_keys(html, "STATUS")
compare("网页 STATUS 的状态", js_status, db.STATUSES)
for k, v in db.STATUS_LABEL.items():
    if js_status.get(k) not in (None, v):
        errors.append(f"状态 {k} 的中文名不一致：db.STATUS_LABEL={v!r}，网页 STATUS={js_status[k]!r}")
for r in rows:
    k = ticked(r[0])[0]
    if k in db.STATUS_LABEL and not r[1].startswith(db.STATUS_LABEL[k]):
        errors.append(f"状态 {k} 的中文名不一致：db.STATUS_LABEL={db.STATUS_LABEL[k]!r}，文档={r[1]!r}")

# ── 打分类别 ──
import score
s53 = section(arch, r"^### 5\.3 ")
track_rows = first_table(s53[s53.index("| track |"):]) if "| track |" in s53 else []
compare("打分类别（5.3）", [ticked(r[0])[0] for r in track_rows], score.TRACKS)
js_track = js_object_keys(html, "TRACK")
compare("网页 TRACK", js_track, score.TRACKS)
for r in track_rows:
    k = ticked(r[0])[0]
    if k in js_track and r[1] != js_track[k]:
        errors.append(f"打分类别 {k} 的中文名不一致：文档={r[1]!r}，网页 TRACK={js_track[k]!r}")

# ── 过滤规则名 ──
code_rules = set(re.findall(r'out\.append\(\("(\w+)"', read("rules.py")))
rule_rows = first_table(section(arch, r"^### 5\.4 "))
compare("过滤规则名（5.4）", [ticked(r[0])[0] for r in rule_rows], code_rules)
js_rule = js_object_keys(html, "RULE")
compare("网页 RULE 的规则名", js_rule, code_rules)
for r in rule_rows:
    k = ticked(r[0])[0]
    if k in js_rule and r[1] != js_rule[k]:
        errors.append(f"过滤规则 {k} 的中文名不一致：文档={r[1]!r}，网页 RULE={js_rule[k]!r}")

# ── HTTP 接口 ──
web = read("web.py")
routes = {"GET /"} if re.search(r'method == "GET" and path == "/":', web) else set()
routes |= {f"{m} {p}" for m, p in re.findall(r'method == "(GET|POST)" and path == "([^"]+)"', web)}
routes |= {f"GET {p}" for p in re.findall(r'^\s+"(/[^"]+)": \(', web, re.M)}   # STATIC
norm = lambda p: re.sub(r"\(\\[dw]\+\)", "<id>", p)
for method, pat in re.findall(r'(?:method == "(GET|POST)" and )?\(?m := re\.fullmatch\(r"([^"]+)", path\)', web):
    p = norm(pat)
    if pat.endswith("/(\\w+)"):   # /api/jobs/<id>/<action>：按 action == "..." 展开
        for action in re.findall(r'action == "(\w+)"', web):
            routes.add(f"POST {p.rsplit('/', 1)[0]}/{action}")
    elif alt := re.search(r"\(([\w|]+)\)", p):
        for a in alt.group(1).split("|"):
            routes.add(f"{method or 'GET'} {p.replace(alt.group(0), a)}")
    else:
        routes.add(f"{method or 'GET'} {p}")
doc_routes = [ticked(r[0])[0] for r in first_table(section(arch, r"^## 6\. ")) if ticked(r[0])]
compare("HTTP 接口（第 6 节）", doc_routes, routes)

# ── 环境变量 ──
code_env = set()
ENV_RE = r"""os\.(?:environ\.get\(|environ\[|getenv\()["']([A-Z][A-Z0-9_]*)["']|process\.env\.([A-Z][A-Z0-9_]*)"""
for f in git("ls-files", "*.py", "*.mjs").split():
    if f in ("docs_check.py", "test_core.py") or not os.path.exists(os.path.join(HERE, f)):
        continue
    code_env |= {a or b for a, b in re.findall(ENV_RE, read(f))}
compare("环境变量（3.4）", [ticked(r[0])[0] for r in first_table(section(arch, r"^### 3\.4 "))], code_env)

# ── 模块 ──
s2 = section(arch, r"^## 2\. ")
doc_files = set()
for r in first_table(s2):
    for name in ticked(r[0]):
        doc_files.add(name)
tracked = [f for f in git("ls-files", "*.py", "*.mjs", "static/*.html", "static/*.js", "checks/*", "hooks/*").split()
           if os.path.exists(os.path.join(HERE, f))]
missing = [f for f in tracked if f not in doc_files]
if missing:
    errors.append(f"模块表（第 2 节）缺这些进 git 的文件：{missing}")
ignored = lambda f: subprocess.run(["git", "-C", REPO, "check-ignore", "-q", f]).returncode == 0   # 比如 test_rules.py
gone = [f for f in doc_files if re.search(r"\.(py|mjs|js|html)$", f) and not os.path.exists(os.path.join(HERE, f)) and not ignored(f)]
if gone:
    errors.append(f"模块表（第 2 节）列了已经不存在的文件：{gone}")

# ── CHANGELOG：每个提交标题都要有 ──
headings = set(re.findall(r"^## (.+)$", read("CHANGELOG.md"), re.M))
subjects = git("log", "--format=%s").splitlines()
lost = [s for s in subjects if s not in headings]
if lost:
    errors.append(f"CHANGELOG.md 缺这些提交（按提交标题核对）：{lost}")

# ── 篇幅提醒（只提示，不拦提交）：超过就该把那一节拆到 docs/<子系统>.md，原处留一行索引 ──
warnings = []
if (n := arch.count("\n")) > 1000:
    warnings.append(f"ARCHITECTURE.md 有 {n} 行（超过 1000），考虑把第 12 节的子系统拆到 docs/")
for m in re.finditer(r"^### (.+)$", section(arch, r"^## 12\. "), re.M):
    body = section(arch, r"^### " + re.escape(m.group(1)) + "$")
    if body.count("\n") > 150:
        warnings.append(f"第 12 节「{m.group(1)}」超过 150 行，考虑拆到 docs/")
for w in warnings:
    print("docs_check 提醒：" + w)

if errors:
    print("docs_check：文档和代码对不上\n- " + "\n- ".join(errors))
    sys.exit(1)
print(f"docs_check：通过（{len(code_tables)} 张表、{len(routes)} 个接口、{len(db.STATUSES)} 个状态、{len(code_rules)} 条规则、{len(code_env)} 个环境变量）")
