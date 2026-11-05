"""定时归纳引擎：把「已结束的任务」和「手动标记的笔记」沉淀成技能卡 / 知识页。

分工
----
- 05-Skills  可执行的操作流程（how-to）：什么时候用、步骤、坑、检查项
- 06-Wiki    定型的知识词条（what-is）：定义、要点、关联

四条硬规矩
----------
1. **只新建，不改写**：AI 产出只写到 05/06 目录，绝不动你的原始笔记
2. **台账去重**：源内容哈希没变就跳过 —— 这就是「已归纳不做操作」
3. **候选池受控**：只处理 ①已结束的任务 ②frontmatter 写 `沉淀: true` 的笔记，
   不做全库扫描（那是烧钱和跑偏的根源）
4. **草稿模式**：DIGEST_DRAFT=1 时产出落到 `<目录>/_待审/`，你确认后再挪进正式目录

环境变量
--------
DIGEST_DRAFT   1=草稿模式（默认）0=直接写入正式目录
DIGEST_MAX     每次最多处理几个候选（默认 3）
"""
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path

import tasks
from config import ENV, user_dir

_llm = None


def get_llm():
    """惰性创建模型客户端：导入本模块不应强依赖 httpx，方便离线迁移/自测。"""
    global _llm
    if _llm is None:
        from llm import LLMRouter
        _llm = LLMRouter()
    return _llm

LEDGER_REL = "_index/归纳台账.md"
DRAFT = ENV.get("DIGEST_DRAFT", "1") == "1"
MAX_PER_RUN = int(ENV.get("DIGEST_MAX", "3") or 3)

SKILL_DIR = "05-Skills"
WIKI_DIR = "06-Wiki"
DRAFT_DIR = "_待审"

SKIP_DIRS = {".git", "_index", "_assets", ".stversions", ".obsidian", ".trash",
             # 视频字幕是「给人看的材料」，不做归纳素材（避免把别人的话当自己的经验）
             ENV.get("VIDEO_DIR", "03-Resources/视频字幕").strip("/").split("/")[-1]}

SKILL_PROMPT = """你是个人知识库的整理助手。把下面这份项目记录提炼成一张「技能卡」。

输出格式（务必严格遵守，不要任何前言、解释、思考过程、代码围栏）：

第一行必须正好是这样（冒号用英文冒号）：
ALIASES: 词1, 词2, 词3, 词4

紧接着另起一行输出卡片正文，正文第一行是「# 卡片标题」，之后按这个结构写：

**什么时候用**：一句话说清楚什么场景会用到它。

## 操作步骤
1. 具体到能照着做的动作
2. ...

## 坑
- 记录里提到的失败、返工、注意事项

## 检查项
- [ ] 做完后自查的点

规则：
1. ALIASES 给 4~8 个「用户以后可能用来搜它的词」，必须包含口语说法、专业术语、英文原名。
   这是检索能否命中的关键，务必认真想。
2. 只基于记录内容归纳，记录里没有的不要编造；某节没内容就写「暂无记录」。
3. 步骤要可执行，不要写「学习相关知识」这类空话。
4. 直接给出最终结果，正文里不要出现分析、权衡、英文思考过程。"""

WIKI_PROMPT = """你是个人知识库的整理助手。把下面这份材料整理成一个「知识词条」。

输出格式（务必严格遵守，不要任何前言、解释、思考过程、代码围栏）：

第一行必须正好是这样：
ALIASES: 词1, 词2, 词3

紧接着另起一行输出词条正文，正文第一行是「# 词条名」，之后按这个结构写：

**一句话定义**：...

## 要点
- ...

## 适用与边界
- 什么时候成立、什么时候不适用

## 关联
- [[相关概念]]

规则：
1. ALIASES 给 3~8 个检索时可能用到的同义词。
2. 只基于材料归纳，不要编造。
3. 面向「三个月后忘了细节的自己」，写清楚为什么是这样、怎么理解。
4. 直接给出最终结果，不要写分析过程。"""


def _now():
    return datetime.now()


def _dir(user, name, draft=False):
    d = user_dir(user) / name
    if draft:
        d = d / DRAFT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(text):
    return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:12]


def _safe_name(text, maxlen=40):
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", text or "").strip()
    s = re.sub(r"\s+", "-", s)
    return (s[:maxlen] or "未命名").strip("-")


def _strip_fence(text):
    t = (text or "").strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", t, re.S)
    return m.group(1).strip() if m else t


def _aliases_of(md):
    m = re.search(r"(?m)^aliases\s*:\s*(.+)$", md or "")
    if not m:
        return []
    return [w for w in re.findall(r"[^\s,\[\]\"'`]+", m.group(1)) if w not in ("[]", "-")]


_ALIAS_RE = re.compile(r"(?m)^\s*ALIASES\s*[:：]\s*(.+)$")


def _ensure_fm(md, title, kind, source):
    """兜底：模型没写 frontmatter 时补一份。"""
    if re.match(r"^---\r?\n", md.strip()):
        return md.strip()
    head = [
        "---",
        "title: %s" % _safe_name(title),
        "type: %s" % kind,
        "aliases: []",
        "tags: []",
        'source: "[[%s]]"' % source,
        "---",
        "",
    ]
    return "\n".join(head) + md.strip()


def _clean_alias(s):
    return (s or "").strip().strip("。.,，、[]\"'`　*-").strip()


def _yaml_safe(s):
    """frontmatter 的值里不能有冒号/井号/换行。"""
    return re.sub(r"[:#\n\r]", " ", (s or "").strip()).strip()


def _extract(raw, title, kind, source):
    """剥出（完整 md, 别名列表）。frontmatter 由代码生成，不指望模型写对。

    关键：正文从「ALIASES 行之后的第一级标题」开始截取。这样即使模型把
    思考过程（尤其是始终思考的 glm-5.3-flash）写进了 content，也会被剥掉。
    """
    text = _strip_fence(raw or "")
    aliases = []
    m = _ALIAS_RE.search(text)
    if m:
        aliases = [_clean_alias(w) for w in re.split(r"[,，、/|]", m.group(1))]
        aliases = [a for a in aliases if a and len(a) <= 20][:8]
        text = text[m.end():]

    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^#\s+\S", ln):
            start = i
            break
    body = "\n".join(lines[start:]) if start is not None else text
    body = re.sub(r"(?m)^```[a-zA-Z]*\s*$", "", body).strip()
    if len(body) < 20:
        raise RuntimeError("模型没有产出有效正文")

    head = [
        "---",
        "title: %s" % _yaml_safe(title),
        "type: %s" % kind,
        "aliases: [%s]" % ", ".join(aliases),
        "tags: []",
        'source: "[[%s]]"' % source,
        "---",
        "",
    ]
    return "\n".join(head) + body + "\n", aliases


# ---------------- 台账 ----------------

def _ledger_path(user):
    p = user_dir(user) / LEDGER_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_ledger(user):
    """返回 {源: 哈希}，用于判断内容有没有变。"""
    p = _ledger_path(user)
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("源", "") or set(cells[0]) <= set("-: "):
            continue
        out[cells[0]] = cells[1]
    return out


def load_ledger_rows(user):
    """返回 {源: {hash,type,out,time}}，重写台账时保留历史明细。"""
    p = _ledger_path(user)
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("源", "") or set(cells[0]) <= set("-: "):
            continue
        out[cells[0]] = {"hash": cells[1], "type": cells[2], "out": cells[3], "time": cells[4]}
    return out


def save_ledger(user, ledger, rows):
    lines = ["# 归纳台账", "",
             "> 由 digest 自动维护。哈希没变的源会被跳过，这就是「已归纳不做操作」的依据。", "",
             "| 源 | 哈希 | 类型 | 产出 | 时间 |",
             "|---|---|---|---|---|"]
    for src, h in sorted(ledger.items()):
        r = rows.get(src) or {}
        if not isinstance(r, dict):
            r = {}
        lines.append("| %s | %s | %s | %s | %s |" % (
            src, h, r.get("type", "-"), r.get("out", "-"), r.get("time", "-")))
    _ledger_path(user).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------- 候选池 ----------------

def _task_source(user, slug, t=None):
    """任务的归纳素材 = 步骤流水 + 行动攻略正文。"""
    t = t or tasks.read_task(user, slug)
    if not t:
        return ""
    base = user_dir(user) / "01-Projects" / slug
    parts = []
    flow = base / "步骤记录.md"
    if flow.exists():
        parts.append(flow.read_text(encoding="utf-8"))
    guide = base / "行动攻略.md"
    if guide.exists():
        raw, body = tasks.split_fm(guide.read_text(encoding="utf-8"))
        parts.append(body)
    if not parts:
        parts.append("任务：%s\n" % t["name"] + "\n".join(
            "- %s" % s["text"] for s in t["steps"]))
    return "\n\n".join(parts)


def _note_flagged(rel_path, text):
    raw, _ = tasks.split_fm(text)
    if not raw:
        return False
    fm = tasks.parse_fm(raw)
    for key in ("沉淀", "digest", "归纳"):
        if str(fm.get(key, "")).strip().lower() in ("true", "1", "yes", "是"):
            return True
    return False


def candidates(user):
    """受控候选池：已结束任务 + 标记 沉淀:true 的笔记。"""
    out = []
    flow_name = getattr(tasks, "FLOW_NAME", "步骤记录.md")
    for t in tasks.list_tasks(user):
        if t["status"] != "done":
            continue
        # 「用 技能卡」开出来的任务：步骤是卡里抄来的，你自己一步没记 →
        # 归纳它只会把旧卡重抄一遍（自我喂养），直接跳过。
        if t.get("source_skill"):
            flow = user_dir(user) / tasks.PROJECTS / t["slug"] / flow_name
            if not flow.exists():
                continue
        src = _task_source(user, t["slug"], t)
        if not src.strip():
            continue
        out.append({
            "kind": "task",
            "key": "task:%s" % t["slug"],
            "title": t["name"].strip() or t["slug"],
            "hash": _hash(src),
            "text": src,
            "source_name": t["name"],
            "slug": t["slug"],
        })

    root = user_dir(user)
    for p in sorted(root.rglob("*.md")):
        rel_parts = p.relative_to(root).parts
        if any(x in SKIP_DIRS or x.startswith(".") for x in rel_parts):
            continue
        if rel_parts[0] in (tasks.PROJECTS, SKILL_DIR, WIKI_DIR):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if not _note_flagged(p.name, text):
            continue
        _, body = tasks.split_fm(text)
        if len(body.strip()) < 40:
            continue
        rel = "/".join(rel_parts)
        out.append({
            "kind": "note",
            "key": "note:%s" % rel,
            "title": p.stem,
            "hash": _hash(body),
            "text": body,
            "source_name": p.stem,
        })
    return out


# ---------------- 生成与写入 ----------------

def _gen(title, text, kind, source_name):
    tpl = SKILL_PROMPT if kind == "skill" else WIKI_PROMPT
    prompt = "%s\n\n卡片标题：%s\n来源：%s\n\n材料：\n%s" % (tpl, title, source_name, text[-6000:])
    raw = get_llm().chat(prompt, tier="main", max_tokens=4000)
    if (raw or "").startswith("[模型调用失败]"):
        raise RuntimeError((raw or "")[:80])
    return _extract(raw, title, kind, source_name)


def _write_card(user, md, title, kind, draft=None):
    draft = DRAFT if draft is None else draft
    folder = SKILL_DIR if kind == "skill" else WIKI_DIR
    d = _dir(user, folder, draft)
    name = _safe_name(re.sub(r"^[#\s]*", "", title))
    path = d / ("%s.md" % name)
    i = 2
    while path.exists():
        path = d / ("%s-%d.md" % (name, i))
        i += 1
    path.write_text(md.rstrip() + "\n", encoding="utf-8")
    try:
        from rag import index_file
        if not draft:
            index_file(user, str(path.relative_to(user_dir(user))).replace("\\", "/"))
    except Exception:
        pass
    return path


def make_skill_card(user, slug, title):
    """从某个已完成任务生成一张技能卡（聊天命令「沉淀 X」调用）。"""
    t = tasks.read_task(user, slug)
    if not t:
        return "找不到这个任务。"
    text = _task_source(user, slug, t)
    if not text.strip():
        return "《%s》没有可归纳的内容（步骤记录和攻略都是空的）。" % t["name"]
    title = (title or t["name"]).strip() or t["name"]
    try:
        md, al = _gen(title, text, "skill", t["name"])
    except Exception as e:
        return "生成失败：%s" % e
    path = _write_card(user, md, title, "skill", draft=False)
    return "技能卡已生成：05-Skills/%s\n别名 %d 个：%s\n以后搜这些词都能命中它。" % (
        path.name, len(al), "、".join(al) if al else "（模型没给，可手动补）")


def make_wiki_page(user, title, text, source_name=""):
    """把一段材料整理成知识词条（06-Wiki）。"""
    md, _al = _gen(title, text, "wiki", source_name or title)
    return _write_card(user, md, title, "wiki", draft=False)


def list_skills(user):
    out = []
    for folder in (SKILL_DIR, WIKI_DIR):
        d = user_dir(user) / folder
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            out.append("%s/%s" % (folder, p.stem))
    return out


# ---------------- 定时入口 ----------------

def _write_report(user, items, skipped, failed, elapsed):
    p = user_dir(user) / "_index" / "归纳日报.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 归纳日报 %s" % _now().strftime("%Y-%m-%d %H:%M"), "",
             "- 本次处理：%d 个" % len(items),
             "- 跳过（已归纳）：%d 个" % skipped,
             "- 失败：%d 个" % len(failed),
             "- 耗时：%.1f 秒" % elapsed]
    if DRAFT:
        lines.append("- 模式：**草稿**（产出在 `_待审/`，确认后挪进正式目录）")
    lines.append("")
    if items:
        lines += ["## 本次产出", ""]
        for title, kind, rel in items:
            lines.append("- %s → `%s`（%s）" % (title, rel, "技能卡" if kind == "skill" else "知识页"))
        lines.append("")
    if failed:
        lines += ["## 失败", ""]
        for title, err in failed:
            lines.append("- %s：%s" % (title, err))
        lines.append("")
    if not items and not failed:
        lines.append("没有新内容需要归纳。")
    p.write_text("\n".join(lines), encoding="utf-8")


def run(user, limit=None, dry_run=False):
    """跑一轮归纳。返回统计字典。"""
    t0 = _now()
    limit = MAX_PER_RUN if limit is None else limit
    cands = candidates(user)
    ledger = load_ledger(user)
    pending = [c for c in cands if ledger.get(c["key"]) != c["hash"]]
    skipped = len(cands) - len(pending)

    if dry_run:
        return {"dry_run": True, "candidates": len(cands), "pending": len(pending),
                "skipped": skipped,
                "items": [(c["title"], c["kind"], c["key"]) for c in pending[:limit]]}

    rows = load_ledger_rows(user)
    done, failed = [], []
    for c in pending[:limit]:
        try:
            kind = "skill"
            md, _al = _gen(c["title"], c["text"], kind, c["source_name"])
            path = _write_card(user, md, c["title"], kind)
            rel = str(path.relative_to(user_dir(user))).replace("\\", "/")
            ledger[c["key"]] = c["hash"]
            rows[c["key"]] = {"type": kind, "out": rel, "time": _now().strftime("%Y-%m-%d %H:%M")}
            done.append((c["title"], kind, rel))
        except Exception as e:
            failed.append((c["title"], str(e)[:120]))

    save_ledger(user, ledger, rows)
    elapsed = (_now() - t0).total_seconds()
    _write_report(user, done, skipped, failed, elapsed)
    return {"candidates": len(cands), "pending": len(pending), "skipped": skipped,
            "done": done, "failed": failed, "elapsed": round(elapsed, 1)}


def run_all(limit=None):
    """对所有用户跑一轮（定时任务入口）。"""
    import config
    out = {}
    for name in config.load_users():
        try:
            out[name] = run(name, limit=limit)
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_all(), ensure_ascii=False, indent=2, default=str))
