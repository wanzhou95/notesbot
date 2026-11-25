# -*- coding: utf-8 -*-
"""人格语料问答：把某个视频系列的字幕建成独立索引，模拟作者口吻回答。

为什么必须独立建库（别改）：
- 主库 vec.db 的 SKIP_DIR_NAMES 故意排除了「视频字幕」——口语流水账会冲淡笔记检索。
  这里单独用 vec_persona.db，主库一行不动，两个库互不可见。
- 原 .md 一个字节不改：清洗（去口水词、拼回被切断的句子）只发生在索引过程，
  符合「Markdown 是唯一真相」。

两级索引：
- 卡片层：每篇 200~700 字摘要（标题 + 一句话总结 + 章节 + 要点）。这部分是 LLM 已经
  整理过的文字，语义密度远高于口播字幕，召回准。先用它定位「哪几条视频讲过」。
- 片段层：字幕按约 1000 字切成时间块，只在卡片命中的视频里取，精确到分钟。

覆盖度必须显式分档（0/1/2/3），不能指望模型自己判断够不够——LLM 有强烈的硬答倾向，
问它「资料够吗」它九成会说够，然后开始编。
"""
import json
import re
import sqlite3
import time

from config import ENV, DATA_DIR, user_dir

VIDEO_DIR = ENV.get("VIDEO_DIR", "03-Resources/视频字幕").strip("/")
DEFAULT_DB = DATA_DIR / "vec_persona.db"

CARD_MAX = 700          # 卡片正文上限
CHUNK_SIZE = 1000       # 时间块目标长度
MERGE_SOFT = 260        # 相邻行拼句的长度上限
EMBED_BATCH = 16        # 单次 embedding 条数（智谱有并发限制）
EMBED_RETRY = 3
CTX_CHARS = 9000        # 送进生成模型的片段总长上限
MIN_CHUNK_CHARS = 40    # 过短的块直接丢弃（ASR 对静音视频会输出「啦。」这类幻觉）

SECTION_RE = re.compile(r"(?m)^##\s*(.+?)\s*$")
LINE_TS_RE = re.compile(r"^\s*\[?(\d{1,2}(?::\d{2}){1,2})\]?\s*(.+)$")


# ---------------- 解析 ----------------

def _frontmatter(text):
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, text[m.end():]


def _sections(body):
    out = {}
    ms = list(SECTION_RE.finditer(body))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(body)
        out[m.group(1).strip()] = body[m.end():end].strip()
    return out


def _pick(secs, name):
    for k, v in secs.items():
        if name in k:
            return v
    return ""


def to_sec(s):
    """'01:24' → 84；'1:02:03' → 3723；解析失败返回 None。"""
    try:
        parts = [int(x) for x in str(s).split(":")]
    except Exception:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def fmt_ts(sec):
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def is_video_file(path):
    """只看前 400 字节判断 type，避免为过滤索引文件去读整篇。"""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(400)
    except Exception:
        return False
    return bool(re.search(r"(?m)^type:\s*video\s*$", head))


def parse_note(path):
    """视频笔记 → 结构化数据。非视频（如「xx索引.md」）返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = _frontmatter(text)
    if (fm.get("type") or "").strip() != "video":
        return None
    secs = _sections(body)
    chapters, points, pairs = [], [], []
    for line in _pick(secs, "章节").splitlines():
        m = re.match(r"^\s*[-*]\s*\[?([\d:]+)\]?\s*(.+)$", line)
        if m:
            chapters.append((m.group(1), m.group(2).strip()))
    for line in _pick(secs, "要点").splitlines():
        m = re.match(r"^\s*[-*]\s*(.+)$", line)
        if m:
            points.append(m.group(1).strip())
    for line in _pick(secs, "完整字幕").splitlines():
        m = LINE_TS_RE.match(line)
        if m:
            sec = to_sec(m.group(1))
            if sec is not None:
                pairs.append((sec, m.group(2).strip()))
    return {
        "title": fm.get("title") or path.stem,
        "url": fm.get("source") or "",
        "gist": _pick(secs, "一句话总结").replace("\n", " ").strip(),
        "chapters": chapters,
        "points": points,
        "pairs": pairs,
    }


# ---------------- 清洗 / 切分 ----------------

_FILLER_RE = re.compile(r"[呃嗯欸诶]")


def clean_line(text):
    """只做低风险清洗：去纯语气词、压缩空白。不删「对吧/是不是」——容易误伤语义。"""
    s = _FILLER_RE.sub("", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def merge_lines(pairs, soft=MERGE_SOFT):
    """把逐行字幕拼成约 soft 字的语义段。

    踩过的坑：ASR（SenseVoice）会在每个 28 秒分片末尾强行补一个句号，实测
    「…然后他一。」+「个又一个再一个解决…」本是同一句话。所以**不能**靠句末标点
    判断句子是否结束——那样几乎一段都拼不起来（467 篇实测 123 段只拼出 122 段）。
    这里改为纯长度窗口合并：目标是给 embedding 一段语义完整的上下文，
    不是还原原始断句。
    """
    out, buf, start, end = [], "", None, None
    for sec, raw in pairs:
        t = clean_line(raw)
        if not t:
            continue
        if start is None:
            start = sec
        buf += t
        end = sec
        if len(buf) >= soft:
            out.append((start, end, buf))
            buf, start = "", None
    if buf and start is not None:
        out.append((start, end, buf))
    return out


def build_chunks(merged, size=CHUNK_SIZE, min_tail=80):
    """语义段 → 约 size 字的时间块，返回 [(t_start, t_end, text)]。

    min_tail：尾巴太短就并回上一块。否则会出现「2 个字」的碎片块（实测存在），
    那种块既检索不到也没法引用。
    """
    out, buf, start, end = [], "", None, None
    for s, e, t in merged:
        if buf and len(buf) + len(t) > size:
            out.append((start, end, buf))
            buf, start, end = t, s, e
        else:
            if not buf:
                start = s
            buf = (buf + t) if buf else t
            end = e
    if buf:
        if out and len(buf) < min_tail:
            ps, _pe, pt = out[-1]
            out[-1] = (ps, end, pt + buf)
        else:
            out.append((start, end, buf))
    # 静音/纯音乐视频的 ASR 会吐出「啦。」这种幻觉，整篇只有几个字，不进库
    return [c for c in out if len(c[2]) >= MIN_CHUNK_CHARS]


def card_text(note, maxlen=CARD_MAX):
    parts = [note["title"]]
    if note["gist"]:
        parts.append(note["gist"])
    if note["chapters"]:
        parts.append("章节：" + "；".join("(%s)%s" % (t, x) for t, x in note["chapters"][:24]))
    if note["points"]:
        parts.append("要点：" + "；".join(note["points"][:16]))
    return "\n".join(parts)[:maxlen]


# ---------------- 向量库 ----------------

class PersonaStore:
    """人格语料的向量库：卡片层 + 片段层，两张 vec0 表 + 两张 meta 表。"""

    def __init__(self, db_path=None, dim=None):
        # sqlite_vec 只在真正用向量库时才需要，放这里便于纯文本逻辑离线测试
        import sqlite_vec

        self.path = str(db_path or DEFAULT_DB)
        if dim is None:
            from embedding import Embedder
            dim = Embedder().dim
        self.dim = dim
        self.conn = sqlite3.connect(self.path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pcard_vec "
                          "USING vec0(embedding float[%d])" % dim)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pcard_meta (rowid INTEGER PRIMARY KEY, "
            "persona TEXT, path TEXT, title TEXT, url TEXT, mtime REAL, text TEXT)")
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pchunk_vec "
                          "USING vec0(embedding float[%d])" % dim)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pchunk_meta (rowid INTEGER PRIMARY KEY, "
            "persona TEXT, path TEXT, title TEXT, url TEXT, t_start INTEGER, "
            "t_end INTEGER, seq INTEGER, text TEXT)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pcard ON pcard_meta(persona, path)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pchunk ON pchunk_meta(persona, path, seq)")
        self.conn.commit()

    def _drop(self, vec_table, meta_table, persona, rel):
        rows = self.conn.execute(
            "SELECT rowid FROM %s WHERE persona=? AND path=?" % meta_table,
            (persona, rel)).fetchall()
        for (rid,) in rows:
            self.conn.execute("DELETE FROM %s WHERE rowid=?" % vec_table, (rid,))
            self.conn.execute("DELETE FROM %s WHERE rowid=?" % meta_table, (rid,))

    def replace_file(self, persona, rel, title, url, mtime, card, card_emb,
                     chunks, chunk_embs):
        """整篇替换：先删旧行再写新行。单文件粒度落盘，天然支持断点续跑。"""
        self._drop("pcard_vec", "pcard_meta", persona, rel)
        self._drop("pchunk_vec", "pchunk_meta", persona, rel)
        rid = self.conn.execute(
            "INSERT INTO pcard_meta(persona, path, title, url, mtime, text) "
            "VALUES (?,?,?,?,?,?)", (persona, rel, title, url, mtime, card)).lastrowid
        self.conn.execute("INSERT INTO pcard_vec(rowid, embedding) VALUES (?,?)",
                          (rid, json.dumps(card_emb)))
        for seq, ((ts, te, txt), emb) in enumerate(zip(chunks, chunk_embs)):
            rid = self.conn.execute(
                "INSERT INTO pchunk_meta(persona, path, title, url, t_start, t_end, seq, text) "
                "VALUES (?,?,?,?,?,?,?,?)", (persona, rel, title, url, ts, te, seq, txt)).lastrowid
            self.conn.execute("INSERT INTO pchunk_vec(rowid, embedding) VALUES (?,?)",
                              (rid, json.dumps(emb)))
        self.conn.commit()

    def file_mtimes(self, persona):
        rows = self.conn.execute(
            "SELECT path, mtime FROM pcard_meta WHERE persona=?", (persona,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def count(self, persona=None):
        if persona:
            f = self.conn.execute("SELECT COUNT(*) FROM pcard_meta WHERE persona=?",
                                  (persona,)).fetchone()[0]
            c = self.conn.execute("SELECT COUNT(*) FROM pchunk_meta WHERE persona=?",
                                  (persona,)).fetchone()[0]
        else:
            f = self.conn.execute("SELECT COUNT(*) FROM pcard_meta").fetchone()[0]
            c = self.conn.execute("SELECT COUNT(*) FROM pchunk_meta").fetchone()[0]
        return {"files": f, "chunks": c}

    def search_cards(self, persona, emb, k=8):
        rows = self.conn.execute(
            "SELECT rowid, distance FROM pcard_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?", (json.dumps(emb), max(k * 8, 60))).fetchall()
        out = []
        for rid, dist in rows:
            m = self.conn.execute(
                "SELECT persona, path, title, url FROM pcard_meta WHERE rowid=?",
                (rid,)).fetchone()
            if m and m[0] == persona:
                out.append({"path": m[1], "title": m[2], "url": m[3], "distance": float(dist)})
            if len(out) >= k:
                break
        return out

    def search_chunks(self, persona, emb, k=60):
        rows = self.conn.execute(
            "SELECT rowid, distance FROM pchunk_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?", (json.dumps(emb), max(k * 8, 240))).fetchall()
        out = []
        for rid, dist in rows:
            m = self.conn.execute(
                "SELECT persona, path, title, url, t_start, t_end, seq, text "
                "FROM pchunk_meta WHERE rowid=?", (rid,)).fetchone()
            if m and m[0] == persona:
                out.append({"path": m[1], "title": m[2], "url": m[3], "t_start": m[4],
                            "t_end": m[5], "seq": m[6], "text": m[7], "distance": float(dist)})
            if len(out) >= k:
                break
        return out

    def chunk_at(self, persona, rel, seq):
        m = self.conn.execute(
            "SELECT title, url, t_start, t_end, text FROM pchunk_meta "
            "WHERE persona=? AND path=? AND seq=?", (persona, rel, seq)).fetchone()
        if not m:
            return None
        return {"path": rel, "title": m[0], "url": m[1], "t_start": m[2],
                "t_end": m[3], "seq": seq, "text": m[4]}


# ---------------- 建索引 ----------------

def _embed_batched(embedder, texts):
    """分批 embedding，失败退避重试。返回向量列表（顺序与输入一致）。"""
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        for attempt in range(EMBED_RETRY):
            try:
                embs = embedder.embed(batch)
                if len(embs) != len(batch):
                    raise RuntimeError("embedding 返回条数不符")
                out.extend(embs)
                break
            except Exception:
                if attempt == EMBED_RETRY - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
        time.sleep(0.15)  # 轻微节流，避免触发限流
    return out


def list_series(user):
    """用户库里现有的视频系列（一级子目录）及篇数、已索引篇数。"""
    try:
        d = user_dir(user) / VIDEO_DIR
    except KeyError:
        return []
    if not d.is_dir():
        return []
    store = PersonaStore()
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        total = sum(1 for f in p.glob("*.md") if is_video_file(f))
        out.append({"name": p.name, "total": total,
                    "indexed": len(store.file_mtimes(p.name))})
    return out


def index_series(user, series, force=False, db_path=None, verbose=False):
    """把某个系列建成独立索引。force=False 时按 mtime 增量，重复跑即断点续跑。

    返回 {"files": 处理篇数, "skipped": 跳过篇数, "chunks": 片段数, "failed": [...]}
    """
    from embedding import Embedder
    d = user_dir(user) / VIDEO_DIR / series
    if not d.is_dir():
        raise RuntimeError("系列目录不存在：%s" % d)
    embedder = Embedder()
    store = PersonaStore(db_path, dim=embedder.dim)
    done_mtimes = {} if force else store.file_mtimes(series)
    done = skipped = nchunks = 0
    failed = []
    files = sorted(f for f in d.glob("*.md") if is_video_file(f))
    for idx, path in enumerate(files, 1):
        rel = "%s/%s" % (series, path.name)
        mtime = path.stat().st_mtime
        if not force and done_mtimes.get(rel) == mtime:
            skipped += 1
            continue
        note = parse_note(path)
        if not note:
            skipped += 1
            continue
        if not note["pairs"] and not note["gist"]:
            skipped += 1
            failed.append("%s：无字幕也无总结，跳过" % path.name)
            continue
        merged = merge_lines(note["pairs"])
        chunks = build_chunks(merged)
        card = card_text(note)
        try:
            embs = _embed_batched(embedder, [card] + [c[2] for c in chunks])
        except Exception as e:
            failed.append("%s：embedding 失败 %s" % (path.name, str(e)[:120]))
            continue
        store.replace_file(series, rel, note["title"], note["url"], mtime,
                           card, embs[0], chunks, embs[1:])
        done += 1
        nchunks += len(chunks)
        if verbose:
            print("  [%d/%d] %s → 卡片1 + 片段%d" % (idx, len(files), path.name[:40], len(chunks)),
                  flush=True)
    return {"files": done, "skipped": skipped, "chunks": nchunks, "failed": failed,
            "total": len(files), "db": store.path}


# ---------------- 检索 ----------------

def retrieve(persona, question, k_cards=6, k_chunks=5, db_path=None):
    """两阶段检索：卡片层定位视频 → 片段层取原文 → 前后各扩一块上下文。

    返回 [{"path","title","url","t_start","t_end","text","distance"}]，失败返回 []。
    """
    from embedding import Embedder
    try:
        qemb = Embedder().embed([question])[0]
    except Exception:
        return []
    store = PersonaStore(db_path)
    cards = store.search_cards(persona, qemb, k=k_cards)
    if not cards:
        return []
    cand = [c["path"] for c in cards]
    cand_set = set(cand)
    picked, seen = [], set()
    for hit in store.search_chunks(persona, qemb, k=60):
        if hit["path"] not in cand_set:
            continue
        key = (hit["path"], hit["seq"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(hit)
        if len(picked) >= k_chunks:
            break
    # 卡片命中了但片段没命中（例如该篇没有字幕，只有总结）→ 退化成卡片信息
    if not picked:
        for c in cards[:k_chunks]:
            row = store.conn.execute(
                "SELECT text FROM pcard_meta WHERE persona=? AND path=?",
                (persona, c["path"])).fetchone()
            if row:
                picked.append({"path": c["path"], "title": c["title"], "url": c["url"],
                               "t_start": 0, "t_end": 0, "seq": -1, "text": row[0],
                               "distance": c["distance"], "card_only": True})
    # 上下文扩展：每块前后各带一块，避免答案被切断
    expanded, have = [], set()
    for hit in picked:
        if hit.get("card_only"):
            # 纯卡片片段没有 seq，不能走下面的邻居扩展，否则会被 seq<0 过滤掉
            if hit["path"] not in have:
                have.add(hit["path"])
                expanded.append(hit)
            continue
        for seq in (hit["seq"] - 1, hit["seq"], hit["seq"] + 1):
            if seq < 0:
                continue
            key = (hit["path"], seq)
            if key in have:
                continue
            row = store.chunk_at(persona, hit["path"], seq) if seq != hit["seq"] else hit
            if row:
                have.add(key)
                expanded.append(row)
    expanded.sort(key=lambda x: (x["path"], x["seq"]))
    return expanded


def short_title(title, maxlen=24):
    """视频标题截短用于引用。

    文件名约定是「标题 正文开头几十字」，所以取第一个空格前的部分通常就是标题。
    """
    t = (title or "").strip()
    if " " in t:
        t = t.split(" ")[0]
    return t[:maxlen] or "未命名"


def build_context(snippets, max_chars=CTX_CHARS):
    """片段 → 送模型的资料块，每条带出处标识，便于模型标注引用。"""
    parts, used = [], 0
    for s in snippets:
        head = "（《%s》%s）" % (short_title(s["title"]), fmt_ts(s.get("t_start") or 0))
        body = "%s\n%s" % (head, s["text"])
        if used + len(body) > max_chars and parts:
            break
        parts.append(body)
        used += len(body)
    return "\n\n".join(parts)


# ---------------- 覆盖度判定 ----------------

COVER_PROMPT = """用户问：%s

下面是从某作者视频字幕里检索到的片段：
%s

判断这些片段能不能支撑回答这个问题。只输出一个 JSON，不要解释、不要代码块：
{"level":0,"why":"一句话"}

level 取值：
3 = 片段直接讲了这个问题的答案，有明确观点
2 = 片段讲了相邻/相关话题，可以按他的思路推演
1 = 只有零散关键词沾边，基本没讲
0 = 完全没讲过这个话题
"""


def judge(persona, question, ctx, client=None):
    """返回 0~3。任何异常按 0 处理——宁可退到通用模型，也不要拿碎片硬编。

    这行代码的模型选择和参数是实测出来的，改之前请先看数据：
    - glm-4.7-flash：免费但极慢，同一问题 51s / 27s / 2.8s，不可用
    - deepseek-flash：1.5s 很快，但不听「只输出 JSON」，解析失败率约 70%
      （20 题两轮共 40 次只成功 16 次）
    - glm-4-flash + temperature=0：20 题两轮 100% 解析成功，语料内 30/30 判覆盖、
      语料外 10/10 正确拒答，两轮结果完全一致
    不带 temperature 时同一个问题档位会来回跳（曾出现 2 级变 0 级）。
    """
    if not ctx.strip():
        return 0
    try:
        if client is None:
            from llm import LLMRouter
            client = LLMRouter()
        raw = client.chat_ordered(
            COVER_PROMPT % (str(question)[:300], str(ctx)[:5000]),
            [("glm", "glm-4-flash")], max_tokens=160, tier="judge", temperature=0)
    except Exception:
        return 0
    if not raw or raw.startswith("[模型调用失败]"):
        return 0
    m = re.search(r"\{[^{}]*\}", raw, re.S)
    if not m:
        return 0
    try:
        d = json.loads(m.group(0))
        lv = int(d.get("level"))
    except Exception:
        return 0
    return lv if 0 <= lv <= 3 else 0


# ---------------- 生成 ----------------

BASE_RULES = """你是「%s」。下面是从你自己公开发布过的视频里检索到的原话片段。

现在有人向你提问，请用第一人称「我」直接回答，就像我在视频里跟人聊天那样。

要求：
1. 只能用片段里出现过的观点、案例、数字、经历，不得发明片段里没有的数字、人名、案例或立场。
2. 不要用「视频里提到」「他说」「作者认为」这类转述口吻——你就是我，直接讲我讲过什么。
3. 挑最关键的 2~3 点讲透，全文控制在 350 字以内，讲成连贯的话，不要列 1. 2. 3.，也不要面面俱到。
4. 想标出处时，在句末写 《短标题》mm:ss，短标题照抄片段开头括号里的，不要扩写。
5. 片段里确实没有的，直说「这个我没讲过」，不要硬凑。
6. 不要自己写「来源」「参考资料」「以上基于…整理」这类尾巴，系统会自动附上。

片段：
%s
"""

LEVEL_HINT = {
    3: "这些片段直接讲过这个问题。直接回答，把片段里的观点组织成连贯的几句话。",
    2: "片段讲的是相邻话题。先用第一人称把我说过的讲清楚（可标出处），"
       "然后单起一行写「—— 以下是我的推断，非原话 ——」，仍用第一人称讲两三句推演。",
    1: "片段只是沾边。先用一两句话讲我相关的观点（可标出处），"
       "然后单起一行写「—— 这事我没细讲，以下是我的补充 ——」，仍用第一人称补充。",
}


def sources_footer(snippets, persona_name, limit=4):
    """由代码强制附加的来源清单与免责声明。

    为什么不交给模型：实测它经常漏写结尾声明、或把引用堆在最后一行还带出
    「（00:00）」这种残渣。凡是能由代码保证的，就不要指望模型。
    """
    seen, items = set(), []
    for s in sorted(snippets, key=lambda x: (x.get("path") or "", x.get("seq") or 0)):
        t = short_title(s.get("title"))
        if t in seen:
            continue
        seen.add(t)
        items.append("《%s》%s" % (t, fmt_ts(s.get("t_start") or 0)))
        if len(items) >= limit:
            break
    refs = "　".join(items) if items else "无"
    return "——\n引用：%s\n以上基于%s公开视频内容整理，非本人发言" % (refs, persona_name)


def answer(persona, question, ctx, level=3, history=None, max_tokens=900, client=None):
    """按覆盖度档位生成人格化回答。

    注意：这里只产出「正文」，引用清单和免责声明由 sources_footer 在 ask() 里附加。
    失败返回带 [模型调用失败] 前缀的串，由上层兜底。
    """
    hint = LEVEL_HINT.get(level, LEVEL_HINT[1])
    prompt = (BASE_RULES % (persona, ctx)) + "\n" + hint + "\n\n用户问：%s" % question
    msgs = []
    for m in (history or [])[-6:]:
        role, content = m.get("role"), (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": prompt})
    try:
        if client is None:
            from llm import LLMRouter
            client = LLMRouter()
        return client.chat_messages(msgs, tier="main", max_tokens=max_tokens)
    except Exception as e:
        return "[模型调用失败] %s" % str(e)[:160]


# ---------------- 对外一步到位 ----------------

def ask(user, persona, question, history=None, db_path=None):
    """人格问答全流程。返回 dict：

    {"handled": bool, "level": 0~3, "answer": str, "sources": [路径],
     "snippets": int}
    handled=False 表示语料没覆盖，调用方应退回普通问答。
    """
    snippets = retrieve(persona, question, db_path=db_path)
    if not snippets:
        return {"handled": False, "level": 0, "answer": "", "sources": [], "snippets": 0}
    ctx = build_context(snippets)
    level = judge(persona, question, ctx)
    if level <= 0:
        return {"handled": False, "level": level, "answer": "", "sources": [],
                "snippets": len(snippets)}
    text = answer(persona, question, ctx, level=level, history=history)
    if not text or text.startswith("[模型调用失败]"):
        return {"handled": False, "level": level, "answer": text, "sources": [],
                "snippets": len(snippets)}
    sources = list(dict.fromkeys(s["path"] for s in snippets))
    return {"handled": True, "level": level,
            "answer": text.strip() + "\n\n" + sources_footer(snippets, persona),
            "sources": sources, "snippets": len(snippets)}
