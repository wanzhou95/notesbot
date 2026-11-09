"""技能卡（05-Skills）与知识页（06-Wiki）：读取、召回、按卡执行。

分工
----
- 05-Skills  可执行的操作流程（how-to）：什么时候用、步骤、坑、检查项
- 06-Wiki    定型的知识词条（what-is）：定义、要点、边界

三个入口
--------
A 向（召回）
  related()       用「标题 + aliases + 正文」本地打分，返回命中的卡片
  context_block() 把命中卡片压成一段 prompt 上下文，塞给模型
  → 谁在用：写笔记（node_note 自动挂双链）、问 AI（node_qa / /api/ask）

B 向（执行）
  use_card()      发「用 X」把卡片里的「操作步骤」变成一条任务的步骤骨架，
                  复用任务体系边做边勾 —— 技能卡是「怎么做」，任务是「正在做」

基础查询
  list_cards() / find_card()  → 技能列表、网页技能页

为什么不走向量检索
------------------
卡片数量少（几十张），靠标题和 aliases 做子串 + 二元组匹配，快、准、零成本，
而且结果可解释（能说清「因为命中了别名『补间』」）。向量检索只作兜底：
本地没命中时再看 rag 在 05/06 目录里的结果。

草稿不参与
----------
DIGEST_DRAFT=1 时归纳产出落在 `05-Skills/_待审/`，那是等你确认的草稿，
不进召回、不进列表，挪到正式目录后才生效（目录名以 _ 开头即被忽略）。
"""
import re

import tasks
from config import user_dir

SKILL_DIR = "05-Skills"
WIKI_DIR = "06-Wiki"
DRAFT_DIR = "_待审"

CARD_DIRS = (SKILL_DIR, WIKI_DIR)

# 召回门槛：标题/别名至少命中一个（强证据）；或正文二元组重合 >= 5（弱证据）
WEAK_OV = 5
SKILL_BONUS = 0.8       # 同分时技能卡（可执行流程）优先于知识页（名词解释）
CONTEXT_MAX = 1500      # 注入 prompt 的总字数上限
CARD_MAX = 800          # 单张卡进上下文的字数上限


# ---------------- 解析 ----------------

def _norm(s):
    """归一化：去标点空白、转小写（中文按字保留）。"""
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _grams(s):
    """二元组集合，用于中文短串的模糊重合。"""
    n = _norm(s)
    if len(n) < 2:
        return {n} if n else set()
    return {n[i:i + 2] for i in range(len(n) - 1)}


def _aliases(raw):
    """frontmatter 里的 aliases，兼容 `[a, b]` 与 `a, b` 两种写法。"""
    v = tasks.clean(raw)
    v = v.strip("[]")
    out = [x.strip().strip("\"'") for x in re.split(r"[,，、]", v)]
    return [x for x in out if x and x not in ("-", "")]


def _section(body, names):
    """取 `## 名称` 区的内容行（到下一个 ## 为止）。"""
    lines = body.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = re.match(r"^##+\s*(.+?)\s*$", ln)
        if m and any(n in m.group(1) for n in names):
            start = i + 1
            break
    if start is None:
        return []
    out = []
    for ln in lines[start:]:
        if re.match(r"^##+\s", ln):
            break
        if ln.strip():
            out.append(ln.rstrip())
    return out


def _bullets(lines):
    """把 `1. xxx` / `- xxx` / `- [ ] xxx` 统一成纯文本条目。"""
    out = []
    for ln in lines:
        m = re.match(r"^\s*(?:\d+[.、)]|[-*+])\s*(?:\[[ xX]\]\s*)?(.+?)\s*$", ln)
        if m:
            out.append(m.group(1).strip())
        elif out and not ln.startswith((" ", "\t")) and len(ln) < 60:
            # 续行：并入上一条，避免多行步骤被切碎
            out[-1] = (out[-1] + " " + ln.strip()).strip()
    return [x for x in out if x]


def parse_card(path, rel):
    """把一个 .md 解析成卡片字典；解析失败返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    raw, body = tasks.split_fm(text)
    fm = tasks.parse_fm(raw)

    title = tasks.clean(fm.get("title"))
    if not title:
        m = re.search(r"(?m)^#\s+(.+)$", body)
        title = (m.group(1).strip() if m else path.stem)
    stem = re.sub(r"-\d+$", "", path.stem)

    when = ""
    m = re.search(r"\*\*什么时候用\*\*\s*[:：]\s*(.+)", body) or \
        re.search(r"\*\*一句话定义\*\*\s*[:：]\s*(.+)", body)
    if m:
        when = m.group(1).strip()

    steps = _bullets(_section(body, ("操作步骤", "步骤", "SOP")))
    pits = _bullets(_section(body, ("坑", "注意")))
    checks = _bullets(_section(body, ("检查项", "检查清单", "自查")))

    kind = "wiki" if rel.startswith(WIKI_DIR) else "skill"
    aliases = _aliases(fm.get("aliases", ""))
    if not aliases:
        aliases = _aliases(fm.get("alias", "")) or _aliases(fm.get("别名", ""))

    return {
        "rel": rel,
        "path": path,
        "stem": stem,
        "title": title,
        "kind": kind,
        "aliases": aliases,
        "when": when,
        "steps": steps,
        "pits": pits,
        "checks": checks,
        "body": body.strip(),
        "keys": [title] + aliases + ([stem] if stem != title else []),
        "hay": " ".join([title, " ".join(aliases), when, " ".join(steps)]),
    }


# ---------------- 读取 ----------------

def list_cards(user, include_draft=False):
    """列举卡片（按修改时间倒序）。草稿目录默认不算。"""
    root = user_dir(user)
    out = []
    for folder in CARD_DIRS:
        d = root / folder
        if not d.is_dir():
            continue
        paths = list(d.glob("*.md"))
        if include_draft:
            paths += list((d / DRAFT_DIR).glob("*.md")) if (d / DRAFT_DIR).is_dir() else []
        for p in paths:
            rel = str(p.relative_to(root)).replace("\\", "/")
            c = parse_card(p, rel)
            if c:
                try:
                    c["mtime"] = p.stat().st_mtime
                except Exception:
                    c["mtime"] = 0
                out.append(c)
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out


def exact_card(user, query):
    """只认「整个名字就是卡名或别名」——命令判定用。

    为什么要这么死板：「用关键帧做过场」是记录句，不是命令。
    模糊匹配留给 related()/问 AI 那条路，它们本来就处理得了半句话。
    """
    q = _norm(query)
    if not q:
        return None
    for c in list_cards(user):
        names = {_norm(c["title"]), _norm(c["stem"])}
        names |= {_norm(a) for a in c["aliases"]}
        if q in names:
            return c
    return None


def find_card(user, query):
    """按名字/别名找卡：精确 > 别名 > 互相包含。找不到返回 None。"""
    q = (query or "").strip().strip("《》「」\"'")
    if not q:
        return None
    cards = list_cards(user)
    for c in cards:
        if q in (c["title"], c["stem"]):
            return c
    for c in cards:
        if q in c["aliases"]:
            return c
    cand = []
    for c in cards:
        names = [c["title"], c["stem"]] + c["aliases"]
        if any(q in n for n in names) or any(n and n in q for n in names):
            cand.append(c)
    if cand:
        # 名字越短越可能是精确目标（「关键帧」不该命中「关键帧与缓动的区别」）
        cand.sort(key=lambda c: len(c["title"]))
        return cand[0]
    return None


# ---------------- A 向：召回 ----------------

def _score(card, qn, qg):
    """返回 (总分, 是否强证据, 正文重合数)。强证据 = 标题或别名出现在问句里。"""
    strong = 0.0
    for k in card["keys"]:
        kn = _norm(k)
        if len(kn) >= 2 and kn in qn:
            strong += 5 + min(len(kn), 8)          # 长别名命中更可信
    ov = len(qg & _grams(card["hay"]))
    # 同分时技能卡优先：问「怎么做」时，可执行流程比名词解释有用
    bonus = SKILL_BONUS if card["kind"] == "skill" else 0.0
    return strong + ov + bonus, strong > 0, ov


def related(user, text, limit=2, strict=False):
    """召回相关卡片。

    strict=True 时只认「标题/别名命中」——写笔记挂双链用这个，
    避免正文偶然重合就乱连线。
    """
    qn, qg = _norm(text), _grams(text)
    if not qn:
        return []
    scored = []
    for c in list_cards(user):
        total, strong, ov = _score(c, qn, qg)
        if not strong and (strict or ov < WEAK_OV):
            continue
        if total <= 0:
            continue
        scored.append((total, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:limit]]


def _clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def card_digest(card, max_chars=CARD_MAX, with_steps=True):
    """单张卡压成 prompt 片段：卡片类型 + 标题 + 适用 + 步骤（+ 坑）。"""
    head = "【%s】%s" % ("技能卡" if card["kind"] == "skill" else "知识页", card["title"])
    parts = [head]
    if card["when"]:
        parts.append("适用：" + card["when"])
    if with_steps and card["steps"]:
        parts.append("操作步骤：\n" + "\n".join(
            "%d. %s" % (i + 1, s) for i, s in enumerate(card["steps"])))
    if card["pits"]:
        parts.append("坑：" + "；".join(card["pits"][:4]))
    if card["kind"] == "wiki" or not with_steps:
        body = _clip(re.sub(r"(?m)^#.*$", "", card["body"]), max_chars // 2)
        parts.append(body)
    return _clip("\n".join(parts), max_chars)


def context_block(user, text, limit=2):
    """给模型用的上下文块；没命中返回空串。"""
    hits = related(user, text, limit=limit)
    if not hits:
        return ""
    blocks, used = [], 0
    for i, c in enumerate(hits):
        blk = card_digest(c, max_chars=CARD_MAX if i == 0 else 400, with_steps=(i == 0))
        if used + len(blk) > CONTEXT_MAX:
            break
        blocks.append(blk)
        used += len(blk)
    return ("库里已沉淀的相关技能卡（你自己以前总结的流程，优先照它回答，"
            "并注明「按《卡名》」）：\n" + "\n\n".join(blocks))


# ---------------- B 向：执行 ----------------

def use_card(user, query):
    """「用 X」：把技能卡变成任务骨架。

    返回 dict：
      {"ok": True, "card":..., "slug":..., "added": n, "existed": bool}
      或 {"ok": False, "err": "..."}
    """
    card = find_card(user, query)
    if not card:
        return {"ok": False, "err": "没找到技能卡「%s」" % (query or "").strip()}
    if card["kind"] == "wiki" or not card["steps"]:
        return {"ok": True, "card": card, "slug": None, "added": 0, "existed": False}

    slug, new = tasks.create_task(user, card["title"], status="doing",
                                 source_skill=card["title"])
    if not slug:
        return {"ok": False, "err": "任务创建失败（名字为空？）"}
    t = tasks.read_task(user, slug) or {}
    have = {_norm(s["text"]) for s in t.get("steps", [])}
    todo = [s for s in card["steps"] if _norm(s) not in have]
    added = tasks.append_steps(user, slug, todo) if todo else 0
    tasks.set_status(user, slug, "doing")

    # 设为当前任务：接下来一句句记都进这条
    import storage
    st = storage.load_state(user)
    st["active"] = slug
    storage.save_state(user, st)
    return {"ok": True, "card": card, "slug": slug, "added": added, "existed": not new,
            "total": (tasks.read_task(user, slug) or {}).get("total", 0)}


def link_note(user, path, card):
    """在刚写的笔记末尾挂一条双链（Obsidian 里会连线）；已挂过则跳过。

    返回 True 表示这次新挂了链接。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    link = "[[%s]]" % card["title"]
    if link in text:
        return False
    if re.search(r"(?m)^> 相关技能：", text):
        # 已有这行 → 追加在后面，用顿号分隔
        text = re.sub(r"(?m)^(> 相关技能：.+)$", lambda m: m.group(1) + "、" + link, text)
    else:
        text = text.rstrip() + "\n\n> 相关技能：%s\n" % link
    try:
        path.write_text(text, encoding="utf-8")
    except Exception:
        return False
    try:
        from rag import index_file
        root = user_dir(user)
        index_file(user, str(path.relative_to(root)).replace("\\", "/"))
    except Exception:
        pass
    return True


def recommend_notes(user, text, exclude_rel=None, limit=3):
    """写笔记后推荐语义相似的旧笔记（排除 05/06，它们走 strict 技能卡路径）。

    返回 [{rel, stem, title, distance}]，按相似度降序。stem 是完整文件名（去 .md），
    title 是去日期前缀的显示名；挂链用 [[stem|title]] 才能精确命中带时间戳的文件名。
    """
    try:
        from rag import search
        hits = search(user, text, limit=limit * 4)
    except Exception:
        return []
    seen = set()
    out = []
    for h in hits:
        rel = h["path"].replace("\\", "/")
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] in CARD_DIRS:
            continue
        if exclude_rel and rel == exclude_rel:
            continue
        if rel in seen:
            continue
        seen.add(rel)
        fname = parts[-1]
        stem = fname[:-3] if fname.endswith(".md") else fname
        title = re.sub(r"^\d{8}-\d{6}-", "", stem)
        if not title:
            continue
        out.append({"rel": rel, "stem": stem, "title": title,
                    "distance": round(h.get("distance", 0), 4)})
        if len(out) >= limit:
            break
    return out


def link_notes(user, note_path, cands):
    """把选中的相关笔记挂到 note_path 末尾（> 相关：[[完整文件名|标题]]）；已挂则跳过。

    返回本次新挂的条数。
    """
    try:
        text = note_path.read_text(encoding="utf-8")
    except Exception:
        return 0
    added = 0
    for c in cands:
        link = "[[%s|%s]]" % (c["stem"], c["title"])
        if link in text or ("[[%s]]" % c["title"]) in text:
            continue
        if re.search(r"(?m)^> 相关：", text):
            text = re.sub(r"(?m)^(> 相关：.+)$",
                          lambda m: m.group(1) + "、" + link, text)
        else:
            text = text.rstrip() + "\n\n> 相关：%s\n" % link
        added += 1
    if added:
        try:
            note_path.write_text(text, encoding="utf-8")
            from rag import index_file
            index_file(user, str(note_path.relative_to(user_dir(user))).replace("\\", "/"))
        except Exception:
            pass
    return added


def rag_fallback(user, text, limit=1):
    """本地没命中时的兜底：向量检索只认 05/06 目录下的结果。"""
    try:
        from rag import search
        hits = search(user, text, limit=limit * 4)
    except Exception:
        return []
    out, seen = [], set()
    for h in hits:
        rel = h.get("path", "")
        if not rel.startswith(CARD_DIRS):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        p = user_dir(user) / rel
        c = parse_card(p, rel) if p.exists() else None
        if c:
            out.append(c)
        if len(out) >= limit:
            break
    return out
