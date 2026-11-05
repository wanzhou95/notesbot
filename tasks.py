"""任务体系：Markdown 文件是唯一真相。

设计原则
--------
1. 不用数据库存任务。任务的全部状态都在主文件的 frontmatter 和 checkbox 里，
   所以你在 Obsidian/手机里勾一下，bot 下次读文件就知道了 —— 没有同步逻辑。
2. 任务主文件 = 01-Projects/<日期-任务名>/<任务名>.md
   主文件名必须等于任务名，Obsidian 双链 [[任务名]] 才能解析（星图前提）。
3. 状态三段式：todo 待办 / doing 进行中 / done 已完成
4. 总览 01-Projects/00-任务总览.md 由 bot 生成，手改会被下次刷新覆盖。

主文件长这样
-----------
    ---
    title: 剪辑视频学习
    status: doing
    created: 2026-11-05 20:00
    updated: 2026-11-05 21:30
    parent:
    related: ["[[摄影构图]]"]
    ---

    # 剪辑视频学习

    - [ ] 这件事已完成          ← 手机端一键完成入口

    **目标**：

    ## 步骤
    - [x] 1. 装剪映，界面分三块
    - [ ] 2. 学关键帧

    ## 关联
    - 上级：
    - 相关：[[摄影构图]]
    - 子任务：

    ## 笔记
"""
import re
from datetime import datetime

from config import user_dir

PROJECTS = "01-Projects"
INDEX_NAME = "00-任务总览.md"
GUIDE_NAME = "行动攻略.md"
FLOW_NAME = "步骤记录.md"

STATUS_TEXT = {"todo": "待办", "doing": "进行中", "done": "已完成"}
DONE_LINE = "- [ ] 这件事已完成"

# 新记录的步骤默认是否算「已完成」。
# False：记一条=一个待办项，做完勾一下（进度条才有意义）
# True ：记一条=一件已做过的事，零负担
DEFAULT_STEP_DONE = False

STEP_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(\d+)\.\s*(.*)$")


def now():
    return datetime.now()


def _ts():
    return now().strftime("%Y-%m-%d %H:%M")


def slugify(text, maxlen=30):
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", text or "").strip()
    s = re.sub(r"\s+", "-", s)
    return (s[:maxlen] or "untitled").strip("-")


def projects_root(user):
    d = user_dir(user) / PROJECTS
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------- frontmatter ----------------

def split_fm(text):
    """拆出 (frontmatter 原文, 正文)。"""
    if text.startswith("---"):
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
        if m:
            return m.group(1), text[m.end():]
    return "", text


def parse_fm(raw):
    fm = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm


def clean(v):
    return (v or "").strip().strip('"').strip("'")


def links_of(v):
    return re.findall(r"\[\[([^\]]+)\]\]", v or "")


def _fm_set(raw, key, value):
    """改写 frontmatter 中某个字段（没有就追加）。"""
    lines = raw.splitlines()
    pat = re.compile(r"^%s\s*:" % re.escape(key))
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = "%s: %s" % (key, value)
            return "\n".join(lines)
    lines.append("%s: %s" % (key, value))
    return "\n".join(lines)


def _write_raw(path, fm_raw, body):
    path.write_text("---\n%s\n---\n%s" % (fm_raw.strip("\n"), body), encoding="utf-8")


def _rel_to_user(path):
    """把绝对路径转成 {user}/{relative} 形式，供 rag 使用（惰性、失败返回 None）。"""
    try:
        from config import NOTES_ROOT
        p = path.resolve()
        rel = p.relative_to(NOTES_ROOT.resolve())
        parts = rel.parts
        if len(parts) < 2:
            return None
        return parts[0], "/".join(parts[1:])
    except Exception:
        return None


def _touch_index(path):
    info = _rel_to_user(path)
    if not info:
        return
    try:
        from rag import index_file
        index_file(info[0], info[1])
    except Exception:
        pass


# ---------------- 读任务 ----------------

def _main_file(base):
    """任务目录里的主文件（排除攻略/流水/总览）。"""
    cands = [p for p in base.glob("*.md")
             if p.name not in (GUIDE_NAME, FLOW_NAME, INDEX_NAME) and not p.name.startswith("_")]
    if not cands:
        return None
    dirname = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", base.name)
    for p in cands:
        if p.stem == dirname:
            return p
    return sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def parse_steps(body):
    steps, started = [], False
    for line in body.splitlines():
        if re.match(r"^##\s*步骤", line):
            started = True
            continue
        if started and re.match(r"^##\s", line):
            break
        m = STEP_RE.match(line)
        if m:
            steps.append({"n": int(m.group(2)),
                          "done": m.group(1).lower() == "x",
                          "text": m.group(3).strip()})
    return steps


def _guide_version(base):
    p = base / GUIDE_NAME
    if not p.exists():
        return 0
    try:
        raw, _ = split_fm(p.read_text(encoding="utf-8"))
        return float(parse_fm(raw).get("version", 0) or 0)
    except Exception:
        return 0


def read_task(user, slug):
    base = projects_root(user) / slug
    if not base.is_dir():
        return None
    main = _main_file(base)
    if not main:
        return None
    try:
        text = main.read_text(encoding="utf-8")
    except Exception:
        return None
    raw, body = split_fm(text)
    fm = parse_fm(raw)

    name = clean(fm.get("title")) or main.stem
    status = clean(fm.get("status")) or "todo"
    if status == "active":       # 兼容旧版本写法
        status = "doing"
    if status not in STATUS_TEXT:
        status = "doing"

    steps = parse_steps(body)
    # 「这件事已完成」被勾上 → 视为 done（手机端一键完成的入口）
    if re.search(r"-\s*\[[xX]\]\s*这件事已完成", body):
        status = "done"

    pv = fm.get("parent", "")
    pl = links_of(pv)
    parent = pl[0] if pl else clean(pv)

    related = links_of(fm.get("related", ""))
    if not related:
        rv = clean(fm.get("related", ""))
        related = [x.strip() for x in rv.split(",") if x.strip() and x.strip() not in ("[]", "")]

    done_cnt = sum(1 for s in steps if s["done"])
    try:
        mtime = main.stat().st_mtime
    except Exception:
        mtime = 0
    info = _rel_to_user(main)
    rel_path = info[1] if info else main.name

    return {
        "slug": slug,
        "name": name,
        "title": name,
        "status": status,
        "parent": parent,
        "related": related,
        "steps": steps,
        "total": len(steps),
        "done": done_cnt,
        "version": _guide_version(base),
        "created": clean(fm.get("created")),
        "updated": clean(fm.get("updated")),
        "mtime": mtime,
        "path": rel_path,
        "abs": str(main),
    }


def list_tasks(user):
    out = []
    root = projects_root(user)
    for base in sorted(root.iterdir()) if root.exists() else []:
        if not base.is_dir() or base.name.startswith((".", "_")):
            continue
        t = read_task(user, base.name)
        if t:
            out.append(t)
    return out


def find_task(user, query, exact=False):
    q = (query or "").strip()
    if not q:
        return None
    q = re.sub(r"^[《「\"']|[》」\"']$", "", q).strip()
    tasks = list_tasks(user)
    for t in tasks:
        if t["name"] == q or t["slug"] == q:
            return t["slug"]
    if exact:
        return None
    cand = [t for t in tasks if q in t["name"] or q in t["slug"]]
    if cand:
        # 名字越短越可能是精确目标
        cand.sort(key=lambda t: len(t["name"]))
        return cand[0]["slug"]
    return None


# ---------------- 写任务 ----------------

_BODY_TMPL = """# %(name)s

%(done_line)s

**目标**：

## 步骤

## 关联

- 上级：%(parent)s
- 相关：%(related)s
- 子任务：

## 笔记
"""


def create_task(user, name, status="todo", parent=None, related=None):
    name = re.sub(r"\s+", " ", (name or "").strip())
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", name)
    if not name:
        return None, False
    exist = find_task(user, name, exact=True)
    if exist:
        return exist, False

    ts = now()
    base_name = "%s-%s" % (ts.strftime("%Y-%m-%d"), slugify(name, 30))
    base = projects_root(user) / base_name
    i = 2
    while base.exists():
        base = projects_root(user) / ("%s-%d" % (base_name, i))
        i += 1
    base.mkdir(parents=True, exist_ok=True)

    parent_cell = "[[%s]]" % parent if parent else ""
    rel_cell = ", ".join('"[[%s]]"' % r for r in (related or []))
    body = _BODY_TMPL % {"name": name, "done_line": DONE_LINE,
                         "parent": parent_cell, "related": rel_cell}
    fm = "\n".join([
        "title: %s" % name,
        "status: %s" % status,
        "created: %s" % _ts(),
        "updated: %s" % _ts(),
        "parent: %s" % parent_cell,
        "related: [%s]" % rel_cell,
        "tags: []",
    ])
    main = base / ("%s.md" % name)
    _write_raw(main, fm, body)
    _touch_index(main)
    render_index(user)
    return base.name, True


def _insert_under(body, section, line):
    """把 line 插到 "## section" 区的末尾（该区不存在则新建）。"""
    lines = body.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^##\s*%s\s*$" % re.escape(section), ln):
            start = i
            break
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "## %s" % section, "", line, ""]
        return "\n".join(lines) + "\n"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s", lines[j]):
            end = j
            break
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    if at == start + 1:      # 空区：标题下补一个空行
        lines.insert(start + 1, "")
        at = start + 2
    lines.insert(at, line)
    return "\n".join(lines) + "\n"


def append_step(user, slug, text, done=None):
    t = read_task(user, slug)
    if not t:
        return 0
    text = " ".join((text or "").split())
    if not text:
        return 0
    if done is None:
        done = DEFAULT_STEP_DONE

    main = user_dir(user) / t["path"]
    raw_text = main.read_text(encoding="utf-8")
    fm_raw, body = split_fm(raw_text)
    n = (max(s["n"] for s in t["steps"]) + 1) if t["steps"] else 1
    body = _insert_under(body, "步骤", "- [%s] %d. %s" % ("x" if done else " ", n, text))
    fm_raw = _fm_set(fm_raw, "updated", _ts())
    if t["status"] == "todo":
        fm_raw = _fm_set(fm_raw, "status", "doing")
    _write_raw(main, fm_raw, body)

    # 时间流水（留档 + 供检索）
    flow = main.parent / FLOW_NAME
    with open(flow, "a", encoding="utf-8") as f:
        if flow.stat().st_size == 0:
            f.write("# 步骤记录\n")
        f.write("[%02d] %s %s\n" % (n, now().strftime("%H:%M"), text))
    _touch_index(flow)
    _touch_index(main)
    render_index(user)
    return n


def set_step_done(user, slug, n, done=True):
    t = read_task(user, slug)
    if not t:
        return False
    main = user_dir(user) / t["path"]
    raw_text = main.read_text(encoding="utf-8")
    fm_raw, body = split_fm(raw_text)
    box = "x" if done else " "
    new_body, cnt = re.subn(r"(?m)^(\s*-\s*)\[[ xX]\](\s*%d\.\s*)" % n,
                            lambda m: "%s[%s]%s" % (m.group(1), box, m.group(2)),
                            body)
    if not cnt:
        return False
    fm_raw = _fm_set(fm_raw, "updated", _ts())
    _write_raw(main, fm_raw, new_body)
    _touch_index(main)
    render_index(user)
    return True


def set_status(user, slug, status):
    if status not in STATUS_TEXT:
        return False
    t = read_task(user, slug)
    if not t:
        return False
    main = user_dir(user) / t["path"]
    raw_text = main.read_text(encoding="utf-8")
    fm_raw, body = split_fm(raw_text)
    fm_raw = _fm_set(fm_raw, "status", status)
    fm_raw = _fm_set(fm_raw, "updated", _ts())
    if status == "done":
        body = re.sub(r"(?m)^\s*-\s*\[ \]\s*这件事已完成\s*$",
                      "- [x] 这件事已完成", body)
    elif re.search(r"(?m)^\s*-\s*\[[xX]\]\s*这件事已完成\s*$", body):
        body = re.sub(r"(?m)^\s*-\s*\[[xX]\]\s*这件事已完成\s*$",
                      "- [ ] 这件事已完成", body)
    _write_raw(main, fm_raw, body)
    _touch_index(main)
    render_index(user)
    return True


def set_parent(user, slug, parent_name):
    t = read_task(user, slug)
    if not t or not parent_name or parent_name == t["name"]:
        return False
    main = user_dir(user) / t["path"]
    raw_text = main.read_text(encoding="utf-8")
    fm_raw, body = split_fm(raw_text)
    fm_raw = _fm_set(fm_raw, "parent", "[[%s]]" % parent_name)
    fm_raw = _fm_set(fm_raw, "updated", _ts())
    _write_raw(main, fm_raw, body)
    _touch_index(main)
    render_index(user)
    return True


def add_related(user, slug, names):
    t = read_task(user, slug)
    if not t:
        return []
    rel = list(t["related"])
    for n in names:
        n = (n or "").strip()
        if n and n != t["name"] and n not in rel:
            rel.append(n)
    main = user_dir(user) / t["path"]
    raw_text = main.read_text(encoding="utf-8")
    fm_raw, body = split_fm(raw_text)
    cell = ", ".join('"[[%s]]"' % r for r in rel)
    fm_raw = _fm_set(fm_raw, "related", "[%s]" % cell)
    fm_raw = _fm_set(fm_raw, "updated", _ts())
    # 同步正文的「相关」行
    body = re.sub(r"(?m)^- 相关：.*$",
                  "- 相关：" + "、".join("[[%s]]" % r for r in rel), body)
    _write_raw(main, fm_raw, body)
    _touch_index(main)
    render_index(user)
    return rel


# ---------------- 关联区刷新 + 总览 ----------------

def _link_cell(names):
    return "、".join("[[%s]]" % n for n in names)


def refresh_links(user, tasks=None):
    """按 parent 字段重建每个任务正文里的「关联」区（内容没变就不写盘）。"""
    tasks = tasks or list_tasks(user)
    kids = {}
    for t in tasks:
        p = t.get("parent")
        if p:
            kids.setdefault(p, []).append(t["name"])
    for t in tasks:
        main = user_dir(user) / t["path"]
        try:
            raw_text = main.read_text(encoding="utf-8")
        except Exception:
            continue
        fm_raw, body = split_fm(raw_text)
        pcell = "[[%s]]" % t["parent"] if t.get("parent") else ""
        rcell = _link_cell(t["related"])
        ccell = _link_cell(sorted(kids.get(t["name"], [])))
        new_body = body
        new_body = re.sub(r"(?m)^- 上级：.*$", "- 上级：" + pcell, new_body)
        new_body = re.sub(r"(?m)^- 相关：.*$", "- 相关：" + rcell, new_body)
        new_body = re.sub(r"(?m)^- 子任务：.*$", "- 子任务：" + ccell, new_body)
        if new_body != body:
            _write_raw(main, fm_raw, new_body)


def _node_line(t, depth, rel_map):
    ind = "  " * depth
    bits = []
    if t["status"] == "done":
        bits.append("攻略 v%s" % _fmt_ver(t["version"]) if t["version"] else "已完成")
    elif t["total"]:
        bits.append("%d/%d 步" % (t["done"], t["total"]))
    else:
        bits.append("还没开始" if t["status"] == "todo" else "进行中")
    tail = ""
    rel = rel_map.get(t["name"])
    if rel:
        tail = "　相关：" + _link_cell(rel)
    return "%s- [[%s]]　`%s`%s" % (ind, t["name"], "·".join(bits), tail)


def _fmt_ver(v):
    return str(int(v)) if float(v) == int(float(v)) else str(v)


def render_index(user, tasks=None):
    """生成 00-任务总览.md（三段分组 + 子任务缩进）。"""
    tasks = tasks if tasks is not None else list_tasks(user)
    refresh_links(user, tasks)

    by_name = {t["name"]: t for t in tasks}
    kids, roots = {}, []
    for t in tasks:
        p = t.get("parent")
        if p and p in by_name and p != t["name"]:
            kids.setdefault(p, []).append(t)
        else:
            roots.append(t)

    rel_map = {t["name"]: t["related"] for t in tasks if t.get("related")}

    def sk(t):
        return t.get("mtime", 0)

    lines = ["> 本文件由 bot 自动生成，手改会被下次刷新覆盖。", "",
             "# 任务总览", "", "更新于 %s" % _ts(), ""]

    empty = True
    for key, label in (("todo", "待办"), ("doing", "进行中"), ("done", "已完成")):
        group = sorted([t for t in roots if t["status"] == key], key=sk, reverse=True)
        if not group:
            continue
        empty = False
        lines.append("## %s %d" % (label, len(group)))
        for t in group:
            lines.append(_node_line(t, 0, rel_map))
            for c in sorted(kids.get(t["name"], []), key=sk, reverse=True):
                lines.append(_node_line(c, 1, rel_map))
                for g in sorted(kids.get(c["name"], []), key=sk, reverse=True):
                    lines.append(_node_line(g, 2, rel_map))
        lines.append("")

    if empty:
        lines += ["还没有任务。发「想做 任务名」登记一句话想法，或「开始 任务名」直接开工。", ""]

    path = projects_root(user) / INDEX_NAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def task_brief(user, slug):
    """给 AI 用的任务摘要（含步骤完成情况）。"""
    t = read_task(user, slug)
    if not t:
        return ""
    out = ["任务：%s（%s）" % (t["name"], STATUS_TEXT.get(t["status"], t["status"]))]
    if t["parent"]:
        out.append("上级任务：%s" % t["parent"])
    if t["related"]:
        out.append("相关任务：%s" % "、".join(t["related"]))
    if t["steps"]:
        out.append("步骤（[x] 表示已完成）：")
        for s in t["steps"]:
            out.append("  [%s] %d. %s" % ("x" if s["done"] else " ", s["n"], s["text"]))
        out.append("进度：%d/%d 步" % (t["done"], t["total"]))
    else:
        out.append("还没有记录步骤。")
    return "\n".join(out)


def stats(user, tasks=None):
    tasks = tasks if tasks is not None else list_tasks(user)
    return {
        "total": len(tasks),
        "todo": sum(1 for t in tasks if t["status"] == "todo"),
        "doing": sum(1 for t in tasks if t["status"] == "doing"),
        "done": sum(1 for t in tasks if t["status"] == "done"),
    }


# ---------------- 存量迁移 ----------------

def migrate(user):
    """把旧的 _project.md 改名成 <任务名>.md，流水步骤补成 checkbox。"""
    report = []
    root = projects_root(user)
    if not root.exists():
        return report
    for base in sorted(root.iterdir()):
        if not base.is_dir() or base.name.startswith((".", "_")):
            continue
        old = base / "_project.md"
        if not old.exists():
            continue
        try:
            text = old.read_text(encoding="utf-8")
        except Exception:
            continue
        raw, body = split_fm(text)
        fm = parse_fm(raw)
        name = clean(fm.get("title")) or re.sub(r"^\d{4}-\d{2}-\d{2}-", "", base.name)
        status = clean(fm.get("status")) or "doing"
        status = {"active": "doing"}.get(status, status)
        if status not in STATUS_TEXT:
            status = "doing"

        # 旧流水 → checkbox 步骤
        steps = []
        flow = base / FLOW_NAME
        if flow.exists():
            for line in flow.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\[(\d+)\]\s*\S*\s*(.+)$", line.strip())
                if m:
                    steps.append((int(m.group(1)), m.group(2).strip()))

        new_body = "\n".join([
            "# %s" % name, "",
            "- [x] 这件事已完成" if status == "done" else DONE_LINE, "",
            "## 步骤", "",
        ] + ["- [x] %d. %s" % (n, tx) for n, tx in steps] + [
            "",
            "## 关联", "",
            "- 上级：",
            "- 相关：",
            "- 子任务：", "",
            "## 笔记", "",
        ]) + "\n"

        new_fm = "\n".join([
            "title: %s" % name,
            "status: %s" % status,
            "created: %s" % (clean(fm.get("created")) or _ts()),
            "updated: %s" % _ts(),
            "parent:",
            "related: []",
            "tags: []",
        ])
        main = base / ("%s.md" % name)
        _write_raw(main, new_fm, new_body)
        old.unlink()
        _touch_index(main)
        report.append("%s → %s.md（%d 步）" % (base.name, name, len(steps)))
    if report:
        render_index(user)
    return report
