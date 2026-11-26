"""规范化、过滤规则（filters.toml）和去重打分。纯函数，不碰数据库，方便测试。"""
import hashlib, json, os, re, sys, tempfile, threading, tomllib
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 规范化 ────────────────────────────────────────────────────────────────
_CO_STOP = {"pty", "ltd", "limited", "inc", "llc", "co", "corp", "corporation", "the", "australia", "au", "aus"}


def norm_company(s):
    s = (s or "").lower().replace("&", " and ")
    words = re.findall(r"[a-z0-9.]+", s)
    words = [w.strip(".") for w in words]
    return " ".join(w for w in words if w and w not in _CO_STOP)


_TITLE_NOISE = re.compile(r"\b(remote|hybrid|onsite|on-site|melbourne|sydney|brisbane|perth|adelaide|canberra|australia|vic|nsw|qld|wfh|"
                          r"full[- ]time|part[- ]time|contract|permanent|fixed[- ]term)\b")


def norm_title(s):
    s = (s or "").lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)          # 去掉括号里的内容
    s = _TITLE_NOISE.sub(" ", s)
    s = re.sub(r"\bsr\b\.?", "senior", s)
    s = re.sub(r"\bjr\b\.?", "junior", s)
    return " ".join(re.findall(r"[a-z0-9+#]+", s))


def jd_hash(text):
    return hashlib.sha1(" ".join(re.findall(r"\w+", (text or "").lower())).encode()).hexdigest() if text else None


def shingles(text, k=3):
    w = re.findall(r"[a-z0-9+#]+", (text or "").lower())
    return {" ".join(w[i:i + k]) for i in range(max(len(w) - k + 1, 0))}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def config_path(name):
    """个人配置（searches.toml / filters.toml）不进 git；没有时退回仓库里的 *.example.toml。"""
    local = os.path.join(HERE, name)
    return local if os.path.exists(local) else os.path.join(HERE, name.replace(".toml", ".example.toml"))


# ── 网页上手动拉黑的公司（blocked_companies.json，不进 git）──────────────────
BLOCKED_PATH = os.environ.get("AUTO_JOB_BLOCKED") or os.path.join(HERE, "blocked_companies.json")
_blocked_cache = (None, {})


def blocked_list(strict=False):
    """[{name, at}]，按拉黑时间排。文件坏了：strict 时报错（别拿空名单把它覆盖掉），否则打印警告当成空。"""
    try:
        return json.load(open(BLOCKED_PATH))
    except FileNotFoundError:
        return []
    except ValueError as e:
        if strict:
            raise RuntimeError(f"{BLOCKED_PATH} 内容损坏，先手动修好：{e}")
        print(f"[rules] 拉黑名单读不了：{e}", file=sys.stderr)
        return []


def blocked():
    """{规范化公司名: 展示名}。文件改了会自动重新读（网页和定时任务是不同进程）。"""
    global _blocked_cache
    try:
        mt = os.path.getmtime(BLOCKED_PATH)
    except OSError:
        return {}
    if _blocked_cache[0] != (BLOCKED_PATH, mt):
        _blocked_cache = ((BLOCKED_PATH, mt), {norm_company(b["name"]): b["name"] for b in blocked_list()})
    return _blocked_cache[1]


_blocked_lock = threading.Lock()


def set_blocked(name, on, at=None):
    """读-改-写整段加锁（网页是多线程的），先写到独立临时文件再原子替换。"""
    with _blocked_lock:
        items = [b for b in blocked_list(strict=True) if norm_company(b["name"]) != norm_company(name)]
        if on:
            items.append({"name": name.strip(), "at": at})
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(BLOCKED_PATH), prefix=".blocked-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, BLOCKED_PATH)


# ── 过滤规则 ──────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_filters(path=None):
    with open(path or config_path("filters.toml"), "rb") as f:
        d = tomllib.load(f)
    flags = re.I

    def companies(section):
        """{规范化别名: 展示名}"""
        out = {}
        for aliases in section["companies"]:
            for a in aliases:
                out[norm_company(a)] = aliases[0]
        return out

    return {
        "crowd": companies(d["crowdwork"]["always"]),
        "crowd_action": d["crowdwork"]["always"]["action"],
        "crowd_title": [re.compile(p, flags) for p in d["crowdwork_title"]["patterns"]],
        "crowd_title_action": d["crowdwork_title"]["action"],
        "crowd_jd_rate": re.compile(d["crowdwork_jd"]["rate_pattern"], flags),
        "crowd_jd": [re.compile(p, flags) for p in d["crowdwork_jd"]["patterns"]],
        "crowd_jd_action": d["crowdwork_jd"]["action"],
        "outsourcing": companies(d["outsourcing"]),
        "outsourcing_action": d["outsourcing"]["action"],
        "seniority": [re.compile(p, flags) for p in d["seniority"]["patterns"]],
        "seniority_action": d["seniority"]["action"],
        "seniority_exempt": [re.compile(p, flags) for p in d["seniority"].get("exempt_patterns", [])],
        "seniority_exempt_except": [re.compile(p, flags) for p in d["seniority"].get("exempt_except_patterns", [])],
        "phd_title": [re.compile(p, flags) for p in d["phd"]["title_patterns"]],
        "phd_jd": [re.compile(p, flags) for p in d["phd"]["jd_patterns"]],
        "phd_action": d["phd"]["action"],
        "agency_low": companies(d["agency"]["low_priority"]),
        "agency_low_action": d["agency"]["low_priority"]["action"],
        "agency": companies(d["agency"]["normal"]),
        "agency_patterns": [re.compile(p, flags) for p in d["agency"]["normal"].get("name_patterns", [])],
        "visa_reject": [re.compile(p, flags) for p in d["visa"]["reject"]],
        "visa_soft": [re.compile(p, flags) for p in d["visa"]["soft"]],
        "intern": [re.compile(p, flags) for p in d["intern"]["patterns"]],
        "intern_action": d["intern"]["action"],
        "off_title": [re.compile(p, flags) for p in d["stack"]["off_title_patterns"]],
        "off_jd": [re.compile(p, flags) for p in d["stack"]["off_jd_patterns"]],
        "on_stack": [re.compile(p, flags) for p in d["stack"]["on_patterns"]],
        "off_action": d["stack"]["off_action"],
        "match": [(name, re.compile(p, flags)) for name, p in d["stack"]["match"]["categories"]],
        "match_min": d["stack"]["match"]["min_hits"],
        "match_action": d["stack"]["match"]["action"],
    }


def _snippet(text, m, pad=40):
    a, b = max(m.start() - pad, 0), min(m.end() + pad, len(text))
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


def evaluate(posting):
    """返回 (results, is_agency)。results = [(rule, action, reason)]，action ∈ reject / soft / boost / tag。"""
    F = load_filters()
    out = []
    co, title, jd = posting.get("company") or "", posting.get("title") or "", posting.get("description") or ""
    cn = norm_company(co)

    if cn and cn in (B := blocked()):
        out.append(("blocked", "reject", B[cn]))
    if cn in F["crowd"]:
        out.append(("crowdwork_company", F["crowd_action"], F["crowd"][cn]))
    for p in F["crowd_title"]:
        if m := p.search(title):
            out.append(("crowdwork_title", F["crowd_title_action"], m.group(0)))
            break
    if F["crowd_jd_rate"].search(jd):
        for p in F["crowd_jd"]:
            if m := p.search(jd):
                out.append(("crowdwork_jd", F["crowd_jd_action"], _snippet(jd, m)))
                break
    exempt = any(p.search(title) for p in F["seniority_exempt"]) and not any(p.search(title) for p in F["seniority_exempt_except"])
    for p in ([] if exempt else F["seniority"]):
        if m := p.search(title):
            out.append(("seniority", F["seniority_action"], m.group(0)))
            break
    for p in F["intern"]:
        if m := p.search(title):
            out.append(("intern", F["intern_action"], m.group(0)))
            break
    phd = next((m.group(0) for p in F["phd_title"] if (m := p.search(title))), None) or \
          next((_snippet(jd, m) for p in F["phd_jd"] if (m := p.search(jd))), None)
    if phd:
        out.append(("phd", F["phd_action"], phd))
    if cn in F["outsourcing"]:
        out.append(("outsourcing", F["outsourcing_action"], F["outsourcing"][cn]))

    is_agency = False
    if cn in F["agency_low"]:
        out.append(("agency_low_priority", F["agency_low_action"], F["agency_low"][cn]))
        is_agency = True
    elif cn in F["agency"]:
        out.append(("agency", "tag", F["agency"][cn]))
        is_agency = True
    elif any(p.search(co) for p in F["agency_patterns"]):
        out.append(("agency", "tag", co))
        is_agency = True

    text = f"{title}\n{jd}"
    for p in F["visa_reject"]:
        if m := p.search(text):
            out.append(("visa", "reject", _snippet(text, m)))
            break
    for p in F["visa_soft"]:
        if m := p.search(text):
            out.append(("visa_note", "tag", _snippet(text, m)))
            break
    if m := next((m for p in F["off_title"] if (m := p.search(title))), None):
        out.append(("off_stack", F["off_action"], m.group(0)))
    elif (m := next((m for p in F["off_jd"] if (m := p.search(jd))), None)) and not any(p.search(text) for p in F["on_stack"]):
        out.append(("off_stack", F["off_action"], _snippet(jd, m)))
    hits = [name for name, p in F["match"] if p.search(text)]
    if len(hits) >= F["match_min"]:
        out.append(("stack_match", F["match_action"], ", ".join(hits)))
    return out, is_agency


# ── 去重 ──────────────────────────────────────────────────────────────────
AUTO_JD = 0.8       # JD 几乎一样：直接合并（中介转发、同一平台重复发帖）
SAME_ROLE_JD = 0.4  # 公司 + 职位名都一样、JD 也比较像：合并
CANDIDATE_JD = 0.6  # 职位名不同但 JD 像（同公司的 JD 套话很多，0.4~0.5 很常见，不算）：标成疑似重复，在网页上确认


def match(new, other):
    """new / other: dict(company_norm, title_norm, sh)。返回 ('auto' | 'candidate' | None, score, reason)。"""
    j = jaccard(new["sh"], other["sh"])
    same_role = bool(new["company_norm"]) and new["company_norm"] == other["company_norm"] and new["title_norm"] == other["title_norm"]
    no_jd = not new["sh"] or not other["sh"]
    if j >= AUTO_JD:
        return "auto", j, f"JD 相似度 {j:.2f}"
    if same_role and (j >= SAME_ROLE_JD or no_jd):
        return "auto", max(j, 0.5), f"同公司同职位，JD 相似度 {j:.2f}"
    if same_role:
        return "candidate", j, f"同公司同职位，但 JD 相似度只有 {j:.2f}"
    if j >= CANDIDATE_JD:
        return "candidate", j, f"JD 相似度 {j:.2f}（公司或职位名不同）"
    return None, j, None
