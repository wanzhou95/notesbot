import json
import re
from datetime import datetime
from pathlib import Path

from config import user_dir, CATEGORIES


def now():
    return datetime.now()


def _reindex(user, path):
    """写文件后更新向量索引（惰性导入 rag，失败不影响笔记写入）。"""
    try:
        from rag import index_file
        index_file(user, str(path.relative_to(user_dir(user))))
    except Exception:
        pass


def slugify(text: str, maxlen=24):
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", text).strip()
    s = re.sub(r"\s+", "-", s)
    return (s[:maxlen] or "untitled").strip("-")


def _index_dir(user):
    d = user_dir(user) / "_index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(user):
    p = _index_dir(user) / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"active": None, "projects": {}, "pending": None}


def save_state(user, state):
    p = _index_dir(user) / "state.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 普通笔记 ----------------

def read_category_desc(user):
    """读取用户的目录说明文件，作为 AI 归类依据。"""
    p = _index_dir(user) / "目录说明.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def write_note(user, text, title=None, tags=None, category="00-灵感库", source="bot"):
    if category not in CATEGORIES:
        category = "00-灵感库"
    d = user_dir(user) / category
    d.mkdir(parents=True, exist_ok=True)
    ts = now()
    name = slugify(title or text.splitlines()[0][:30])
    path = d / ("%s-%s.md" % (ts.strftime("%Y%m%d-%H%M%S"), name))
    fm = [
        "---",
        "date: %s" % ts.strftime("%Y-%m-%d %H:%M"),
        "tags: [%s]" % ", ".join(tags or []),
        "category: %s" % category,
        "source: %s" % source,
        "---",
        "",
    ]
    path.write_text("\n".join(fm) + text.strip() + "\n", encoding="utf-8")
    _reindex(user, path)
    return path


# ---------------- 附件（图片/文件） ----------------

ASSET_TYPES = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}


def save_asset(user, name, data: bytes):
    """把上传的图片/文件存到用户库 _assets/<年月>/ 下，返回相对路径。"""
    safe = re.sub(r"[\\/:*?\"<>|\n\r\t]", "_", name).strip() or "file"
    d = user_dir(user) / "_assets" / now().strftime("%Y-%m")
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("%s-%s" % (now().strftime("%Y%m%d-%H%M%S"), safe))
    path.write_bytes(data)
    return str(path.relative_to(user_dir(user))).replace("\\", "/")


def asset_path(user, rel):
    """把 _assets 相对路径解析为绝对路径，并校验落在用户库内。"""
    root = user_dir(user).resolve()
    p = (root / rel).resolve()
    if not str(p).startswith(str(root)) or not p.is_file():
        return None
    return p


# ---------------- 项目会话 ----------------

def create_project(user, name):
    ts = now()
    slug = "%s-%s" % (ts.strftime("%Y-%m-%d"), slugify(name, 30))
    base = user_dir(user) / "01-Projects" / slug
    if base.exists():
        return slug, False
    base.mkdir(parents=True, exist_ok=True)
    meta = [
        "---",
        "title: %s" % name,
        "status: active",
        "created: %s" % ts.strftime("%Y-%m-%d %H:%M"),
        "updated: %s" % ts.strftime("%Y-%m-%d %H:%M"),
        "version: 0",
        "---",
        "",
        "# %s" % name,
    ]
    (base / "_project.md").write_text("\n".join(meta) + "\n", encoding="utf-8")
    (base / "步骤记录.md").write_text("# 步骤记录\n", encoding="utf-8")
    st = load_state(user)
    st["projects"][slug] = {
        "name": name,
        "status": "active",
        "steps": 0,
        "created": ts.strftime("%Y-%m-%d %H:%M"),
        "version": 0,
        "changelog": [],
    }
    st["active"] = slug
    save_state(user, st)
    return slug, True


def project_dir(user, slug):
    return user_dir(user) / "01-Projects" / slug


def append_step(user, slug, text):
    base = project_dir(user, slug)
    base.mkdir(parents=True, exist_ok=True)
    ts = now()
    st = load_state(user)
    info = st["projects"].setdefault(slug, {"name": slug, "status": "active", "steps": 0, "version": 0, "changelog": []})
    n = info.get("steps", 0) + 1
    with open(base / "步骤记录.md", "a", encoding="utf-8") as f:
        f.write("[%02d] %s %s\n" % (n, ts.strftime("%H:%M"), text.strip()))
    info["steps"] = n
    info["status"] = "active"
    save_state(user, st)
    _reindex(user, base / "步骤记录.md")
    return n


def read_steps(user, slug):
    p = project_dir(user, slug) / "步骤记录.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter，返回正文。"""
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\n", text, re.S)
        if m:
            return text[m.end():]
    return text


def strip_md_fence(text: str) -> str:
    """剥掉 LLM 输出误带的 ```markdown ... ``` 整体围栏。"""
    t = text.strip()
    m = re.match(r"^```(?:markdown|md)\s*\n(.*)\n```\s*$", t, re.S)
    if m:
        t = m.group(1).strip()
        return strip_md_fence(t)  # 防多层嵌套
    return text


def write_guide(user, slug, content, note=""):
    content = strip_md_fence(content)
    base = project_dir(user, slug)
    ts = now()
    st = load_state(user)
    info = st["projects"].get(slug, {})
    ver = round(info.get("version", 0) + 1, 1)
    header = [
        "---",
        "version: %s" % ver,
        "updated: %s" % ts.strftime("%Y-%m-%d %H:%M"),
        "---",
        "",
    ]
    (base / "行动攻略.md").write_text("\n".join(header) + content.strip() + "\n", encoding="utf-8")
    info["version"] = ver
    info["status"] = "done"
    if note:
        info.setdefault("changelog", []).append("v%s %s" % (ver, note))
    meta = base / "_project.md"
    if meta.exists():
        t = meta.read_text(encoding="utf-8")
        t = re.sub(r"status: \w+", "status: done", t)
        t = re.sub(r"version: [\d.]+", "version: %s" % ver, t)
        meta.write_text(t, encoding="utf-8")
    save_state(user, st)
    _reindex(user, base / "行动攻略.md")
    return ver


def list_projects(user):
    st = load_state(user)
    return st.get("projects", {})


# ---------------- 检索 ----------------

def search(user, query, limit=5):
    import re
    root = user_dir(user)
    # 清理疑问词与标点，提取核心关键词
    key = re.sub(r"[?？，。！!、,\.\s]", "", query)
    key = re.sub(r"(怎么做|做什么|是什么|怎么办|怎么|如何|为什么|该|怎样|哪些|哪个|哪里|帮我|查一下|查|搜|搜索|下|一下|请问|告诉我|是不是)", "", key)
    if not key:
        return []
    # 生成候选词：完整 key + 2/3 字子串，长词优先命中
    terms = [key]
    for i in range(len(key)):
        for L in (2, 3):
            if i + L <= len(key):
                terms.append(key[i:i + L])
    terms = sorted(set(terms), key=len, reverse=True)

    hits = []
    seen = set()
    for p in sorted(root.rglob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if ".git" in p.parts or "_index" in p.parts or ".stversions" in p.parts or ".obsidian" in p.parts or ".trash" in p.parts:
            continue
        if p.name in ("_project.md", "欢迎.md"):
            continue
        rel = str(p.relative_to(root))
        if rel in seen:
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        best = None
        for term in terms:
            if term in t:
                best = term
                break
        if best:
            idx = t.find(best)
            snippet = t[max(0, idx - 40): idx + 120].replace("\n", " ")
            hits.append({"path": rel, "snippet": snippet})
            seen.add(rel)
            if len(hits) >= limit:
                break
    return hits
