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


# ---------------- 项目会话（委托 tasks 模块，文件为唯一真相） ----------------

def create_project(user, name):
    """新建任务并切为当前项目。已完成/待办的同名任务会被重新打开为进行中。"""
    import tasks
    slug, new = tasks.create_task(user, name, status="doing")
    if not slug:
        return None, False
    if not new:
        tasks.set_status(user, slug, "doing")
    st = load_state(user)
    st["active"] = slug
    save_state(user, st)
    return slug, new


def project_dir(user, slug):
    return user_dir(user) / "01-Projects" / slug


def append_step(user, slug, text):
    import tasks
    return tasks.append_step(user, slug, text)


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
    import tasks
    content = strip_md_fence(content)
    base = project_dir(user, slug)
    ts = now()
    gp = base / "行动攻略.md"
    old_ver = 0
    if gp.exists():
        try:
            raw, _ = tasks.split_fm(gp.read_text(encoding="utf-8"))
            old_ver = float(tasks.parse_fm(raw).get("version", 0) or 0)
        except Exception:
            old_ver = 0
    ver = round(old_ver + 1, 1)
    header = [
        "---",
        "version: %s" % ver,
        "updated: %s" % ts.strftime("%Y-%m-%d %H:%M"),
        "---",
        "",
    ]
    gp.write_text("\n".join(header) + content.strip() + "\n", encoding="utf-8")
    # 任务状态置为已完成（同时刷新总览与索引）
    tasks.set_status(user, slug, "done")
    _reindex(user, gp)
    return ver


def list_projects(user):
    """从文件读取任务清单，返回旧格式的字典（兼容 /api/projects 与导出逻辑）。"""
    import tasks
    out = {}
    for t in tasks.list_tasks(user):
        out[t["slug"]] = {
            "name": t["name"],
            "status": t["status"],
            "steps": t["total"],
            "done_steps": t["done"],
            "created": t["created"],
            "updated": t["updated"],
            "version": t["version"],
            "changelog": [],
            "parent": t["parent"],
            "related": t["related"],
            "path": t["path"],
        }
    return out


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
        # 以 _ 开头的目录（_待审 / _assets）是草稿和附件，不进搜索（与 rag.skip_file 一致）
        if any(part.startswith("_") for part in p.parts[:-1]):
            continue
        if p.name in ("_project.md", "欢迎.md", "00-任务总览.md"):
            continue
        rel = str(p.relative_to(root))
        if rel in seen:
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        t_low = t.lower()
        best = None
        for term in terms:
            if term.lower() in t_low:
                best = term
                break
        if best:
            idx = t_low.find(best.lower())
            snippet = t[max(0, idx - 40): idx + 120].replace("\n", " ")
            hits.append({"path": rel, "snippet": snippet, "_term": best,
                         "_mtime": p.stat().st_mtime})
            seen.add(rel)
    # 长词命中优先（完整词 > 单字/双字碎片），同长度再按修改时间
    hits.sort(key=lambda x: (-len(x["_term"]), -x["_mtime"]))
    return [{"path": h["path"], "snippet": h["snippet"]} for h in hits[:limit]]
