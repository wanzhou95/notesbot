import re

from config import user_dir, DATA_DIR, ENV
from embedding import Embedder
from vector_store import VectorStore

_embedder = None
_store = None


def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def get_store():
    global _store
    if _store is None:
        _store = VectorStore(str(DATA_DIR / "vec.db"), dim=get_embedder().dim)
    return _store


def chunk_text(text, size=800):
    """去掉 frontmatter，按段落切 chunk，每 chunk 不超过 size 字。"""
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\n", text, re.S)
        if m:
            text = text[m.end():]
    text = text.strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 1 > size:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    # 超长段落硬切
    out = []
    for c in chunks:
        while len(c) > size:
            out.append(c[:size])
            c = c[size:]
        if c:
            out.append(c)
    return out


SKIP_NAMES = {"_project.md", "欢迎.md", "00-任务总览.md"}
MIN_CHUNK = 4

# 不索引的目录名。视频字幕动辄上万字，进向量库又贵又把检索结果冲淡，
# 所以整目录排除（仍可用关键词兜底检索，只是不花 embedding 的钱）。
_VIDEO_DIR_NAME = ENV.get("VIDEO_DIR", "03-Resources/视频字幕").strip("/").split("/")[-1]
_EXTRA_SKIP = {x.strip() for x in ENV.get("RAG_SKIP_DIRS", "").replace("，", ",").split(",") if x.strip()}
SKIP_DIR_NAMES = {".git", "_index", ".stversions", ".obsidian", ".trash",
                  _VIDEO_DIR_NAME} | _EXTRA_SKIP


def _aliases(text):
    """提取 frontmatter 里的 aliases / 别名，用于增强检索命中（搜「补间」命中「关键帧」）。"""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return ""
    head = m.group(1)
    out = []
    for key in ("aliases", "alias", "别名"):
        mm = re.search(r"(?m)^%s\s*:\s*(.+)$" % key, head)
        if mm:
            out += re.findall(r"[^\s,\[\]\"'`]+", mm.group(1))
    out = [w for w in out if w and w not in ("[]", "-")]
    return " ".join(out)


def skip_file(rel_path):
    """不索引的文件：元数据、模板、版本库、索引目录、_ 开头的草稿目录。"""
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1]
    if name in SKIP_NAMES:
        return True
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    # 以 _ 开头的目录（_assets / _待审 等）一律不索引
    return any(p.startswith("_") for p in parts[:-1])


def ctx_prefix(rel_path):
    """给 chunk 加文件标题上下文（只用于向量化，不存原文）。"""
    parts = rel_path.replace("\\", "/").split("/")
    stem = re.sub(r"^\d{8}-\d{6}-", "", parts[-1].rsplit(".", 1)[0])
    parent = ""
    if len(parts) > 1:
        parent = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", parts[-2])
    return ("%s %s" % (parent, stem)).strip()


def _chunks_of(text):
    return [c for c in chunk_text(text) if len(c.strip()) >= MIN_CHUNK]


def index_file(user, rel_path):
    """索引单个笔记文件：先删该文件的旧 chunk，再加新的。"""
    root = user_dir(user)
    p = root / rel_path
    store = get_store()
    if skip_file(rel_path) or not p.exists():
        store.delete_file(user, rel_path)
        return 0
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return 0
    chunks = _chunks_of(text)
    store.delete_file(user, rel_path)
    if not chunks:
        return 0
    prefix = (ctx_prefix(rel_path) + " " + _aliases(text)).strip()
    try:
        embs = get_embedder().embed([prefix + "\n" + c for c in chunks])
    except Exception:
        return 0
    for c, e in zip(chunks, embs):
        store.add(user, rel_path, c, e)
    return len(chunks)


def reindex_user(user):
    """全量重建某用户的向量索引。"""
    root = user_dir(user)
    store = get_store()
    store.clear_user(user)
    total = 0
    for p in sorted(root.rglob("*.md")):
        if skip_file(str(p.relative_to(root))):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        chunks = _chunks_of(text)
        if not chunks:
            continue
        prefix = (ctx_prefix(rel) + " " + _aliases(text)).strip()
        try:
            embs = get_embedder().embed([prefix + "\n" + c for c in chunks])
        except Exception:
            continue
        for c, e in zip(chunks, embs):
            store.add(user, rel, c, e)
            total += 1
    return total


def _bigrams(s):
    s = re.sub(r"[\s\W_]+", "", s)
    return [s[i:i + 2] for i in range(len(s) - 1)]


def search(user, query, limit=5):
    """混合检索：语义召回 → 关键词重合度重排；失败返回空列表。"""
    root = user_dir(user)
    try:
        qemb = get_embedder().embed([query])[0]
        cands = get_store().search(user, qemb, limit=max(limit * 4, 20))
    except Exception:
        return []
    if not cands:
        return []
    qg = set(_bigrams(query))
    scored = []
    for h in cands:
        if h["distance"] > 1.45:
            continue
        tg = set(_bigrams(h["text"])) | set(_bigrams(h["path"]))
        ov = len(qg & tg) / max(len(qg), 1)
        # 距离偏远且完全无关键词重合的，判为噪声
        if h["distance"] > 1.30 and ov == 0:
            continue
        score = (1.0 / (1.0 + h["distance"])) * (1 + 0.9 * ov)
        if len(h["text"].strip()) < 8:
            score *= 0.93  # 极短片段语义泛化强，轻微降权
        scored.append((score, h))
    scored.sort(key=lambda x: -x[0])
    out, per_file = [], {}
    for score, h in scored:
        # 文件已删/移到回收站时，索引里的旧 chunk 不会自动清，这里拦住，别把死链接塞给用户
        if not (root / h["path"]).exists():
            continue
        if per_file.get(h["path"], 0) >= 2:
            continue
        per_file[h["path"]] = per_file.get(h["path"], 0) + 1
        out.append({"path": h["path"], "text": h["text"],
                    "distance": round(h["distance"], 4), "score": round(score, 4)})
        if len(out) >= limit:
            break
    return out
