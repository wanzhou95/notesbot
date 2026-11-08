"""归纳草稿审核 + 反馈台账：让 AI 记住你的口味，越用越准。

闭环
----
1. digest 把草稿写进 05-Skills/_待审/ 或 06-Wiki/_待审/
2. 你审核：通过 / 否定 / 修改意见（网页「待审」区或聊天命令）
3. 每次审核都写进 _index/归纳反馈.md：
   - 「你的原话」原样留着（可回溯）
   - AI 把意见提炼成一条**祈使句规则**，并判断作用范围（全局 / 仅这张卡）
4. 下次归纳（定时 or 手动「沉淀 X」）把适用规则注入 prompt，明确要求遵守

为什么用 Markdown 表格而不是 JSON
--------------------------------
这是个人知识库，一切以文件为准。台账放在库里，Obsidian 直接能看到、能改、
能删行。不想让某条规则生效，把那行删掉即可，不需要任何后台开关。

规则作用域
----------
- 全局：以后所有卡片都遵守（例如「别名最多 4 个」「坑没内容就整节删掉」）
- 仅本卡：只对同名卡片/同一来源生效（例如「这张不该叫 codex使用」）
- 你可以显式指定：`意见 全局：...` / `意见 本卡：...`，不写就由 AI 判断

留痕原则
--------
否定不删除文件，移到 .trash/（Obsidian 回收站，可恢复）；
通过会补进 RAG 索引；修改意见会**立刻重写草稿**，你能马上看到效果。
"""
import re
from datetime import datetime
from pathlib import Path

import tasks
from config import user_dir

_llm = None


def get_llm():
    """惰性创建模型客户端：导入本模块不强依赖 httpx，方便离线自测。"""
    global _llm
    if _llm is None:
        from llm import LLMRouter
        _llm = LLMRouter()
    return _llm


FEEDBACK_REL = "_index/归纳反馈.md"
DRAFT_DIR = "_待审"
CARD_DIRS = ("05-Skills", "06-Wiki")

# 反馈类型
APPROVE = "赞同"
REJECT = "否定"
REVISE = "修改"

HEADER = "| 时间 | 对象 | 类型 | 我的原话 | 提炼规则 | 范围 | 来源 |"
SEP = "|---|---|---|---|---|---|---|"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------- 待审草稿 ----------------

def _draft_dirs(user):
    root = user_dir(user)
    return [(root / d / DRAFT_DIR, d) for d in CARD_DIRS]


def pending(user):
    """列出待审草稿。返回 [{name, rel, path, kind, title, when, aliases, body}]。"""
    import skills
    root = user_dir(user)
    out = []
    for d, folder in _draft_dirs(user):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            rel = str(p.relative_to(root)).replace("\\", "/")
            c = skills.parse_card(p, rel)
            if not c:
                continue
            fm = tasks.parse_fm(tasks.split_fm(p.read_text(encoding="utf-8"))[0])
            out.append({
                "name": p.stem, "rel": rel, "path": p, "folder": folder,
                "kind": c["kind"], "title": c["title"], "when": c["when"],
                "aliases": c["aliases"], "steps": len(c["steps"]),
                "body": c["body"], "source": tasks.clean(fm.get("source")),
            })
    return out


def find_draft(user, query):
    """按名字模糊匹配草稿（大小写不敏感）。返回 (卡片, 候选数)。"""
    q = (query or "").strip().lower()
    items = pending(user)
    if not q:
        return (items[0] if len(items) == 1 else None), len(items)
    exact = [c for c in items if c["name"].lower() == q or c["title"].lower() == q]
    if len(exact) == 1:
        return exact[0], 1
    hits = [c for c in items if q in c["name"].lower() or q in c["title"].lower()
            or any(q in a.lower() for a in c["aliases"])]
    return (hits[0] if hits else None), len(hits)


def brief(user):
    """给聊天命令用的一段摘要。"""
    items = pending(user)
    if not items:
        return "待审区是空的 —— 定时归纳每天 03:00 跑，任务「结束」后会产生草稿。"
    lines = ["待审 %d 张（在 %s/ 下）：" % (len(items), "、".join(
        "%s/%s" % (d, DRAFT_DIR) for d in CARD_DIRS))]
    for i, c in enumerate(items, 1):
        bits = [c["kind"] == "skill" and "技能卡" or "知识页"]
        if c["steps"]:
            bits.append("%d 步" % c["steps"])
        lines.append("%d. 《%s》（%s）" % (i, c["title"], " · ".join(bits)))
        if c["when"]:
            lines.append("   什么时候用：%s" % c["when"][:60])
    lines.append("")
    lines.append("发「通过 名字」/「否定 名字 原因」/「意见 名字：具体怎么改」；")
    lines.append("或去网页「技能」页顶部的待审区点按钮。")
    return "\n".join(lines)


# ---------------- 反馈台账 ----------------

def feedback_path(user):
    p = user_dir(user) / FEEDBACK_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cell(s):
    """表格单元格：去掉竖线和换行，避免把表格撑坏。"""
    return re.sub(r"[\r\n|]", " ", str(s or "").strip()).strip()


def load_feedback(user):
    """读台账，返回 [{time, target, kind, quote, rule, scope, source}]。

    任何异常都退化成空列表：反馈台账是「增强项」，不该拖垮归纳主流程。
    """
    try:
        p = feedback_path(user)
        if not p.exists():
            return []
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[0] in ("时间", "") or set(cells[0]) <= set("-: "):
            continue
        out.append({
            "time": cells[0], "target": cells[1], "kind": cells[2],
            "quote": cells[3], "rule": cells[4], "scope": cells[5],
            "source": cells[6] if len(cells) > 6 else "",
        })
    return out


def save_feedback(user, rows):
    lines = [
        "# 归纳反馈台账", "",
        "> 你审核草稿时说的话记在这里。「我的原话」留档，「提炼规则」是 AI 下次归纳要遵守的。",
        "> 不想让某条规则生效，直接删掉那一行；想改口味，改「提炼规则」那一格即可。",
        "> 范围：`全局` = 所有卡片都遵守，`仅本卡` = 只对这张卡/同一来源生效。", "",
        HEADER, SEP,
    ]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            _cell(r.get("time")), _cell(r.get("target")), _cell(r.get("kind")),
            _cell(r.get("quote")), _cell(r.get("rule")), _cell(r.get("scope")),
            _cell(r.get("source"))))
    feedback_path(user).write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_feedback(user, target, kind, quote, rule, scope, source=""):
    rows = load_feedback(user)
    rows.append({
        "time": _now(), "target": target, "kind": kind, "quote": quote,
        "rule": rule, "scope": scope, "source": source,
    })
    save_feedback(user, rows)
    return rows[-1]


# ---------------- 规则提炼 ----------------

SCOPE_GLOBAL = "全局"
SCOPE_CARD = "仅本卡"

_DISTILL_PROMPT = """你是个人知识库的「口味记录员」。用户在审核 AI 生成的草稿卡片，你要把他的话提炼成一条以后能直接遵守的规则。

输出格式（严格两行，不要任何解释、前言、代码围栏、思考过程）：
SCOPE: 全局
RULE: 一句话祈使句规则

判断 SCOPE：
- 「全局」= 这是对以后所有同类卡片的通用要求。例如：别名最多几个、某节没内容怎么办、步骤写多细、语气、要不要编造、别写空话。
- 「仅本卡」= 只针对眼前这一张卡的具体内容。例如：这张不该叫某个名字、这个步骤写错了、这张卡应该归到另一个目录。

RULE 要求：
- 一条祈使句，25 字以内，能直接塞进「生成卡片时必须遵守」的清单里。
- 不要复述用户原话里的情绪，只留可执行部分。
- 不要写「用户希望……」这种转述，直接写「别名不超过 4 个」这种命令。
- 如果用户只是在夸奖（如「挺好」「不错」），把被夸的点提炼成保持型规则，例如「保持步骤具体到能照着做」。
- 如果用户在否定（如「这根本不是技能」），提炼成排除型规则，例如「一次性安装记录不要做成技能卡」。

卡片：%s（%s）
反馈类型：%s
用户原话：%s
%s"""


def distill(title, kind, fb_kind, quote, scope_hint=""):
    """把用户原话提炼成 (scope, rule)。失败时兜底为原话本身，绝不丢反馈。"""
    hint = ""
    if scope_hint:
        hint = "\n注意：用户已显式指定范围为「%s」，必须照用。" % scope_hint
    prompt = _DISTILL_PROMPT % (
        title, "技能卡" if kind == "skill" else "知识页", fb_kind, quote, hint)
    try:
        raw = get_llm().chat(prompt, tier="router", max_tokens=300) or ""
    except Exception:
        raw = ""
    if (raw or "").startswith("[模型调用失败]"):
        raw = ""

    scope, rule = "", ""
    for line in (raw or "").splitlines():
        m = re.match(r"^\s*SCOPE\s*[:：]\s*(.+)$", line)
        if m:
            v = m.group(1).strip()
            scope = SCOPE_GLOBAL if "全局" in v or "global" in v.lower() else SCOPE_CARD
        m = re.match(r"^\s*RULE\s*[:：]\s*(.+)$", line)
        if m:
            rule = m.group(1).strip().strip("。`\"'")
    if scope_hint:
        scope = SCOPE_GLOBAL if scope_hint == SCOPE_GLOBAL else SCOPE_CARD
    if not scope:
        scope = SCOPE_GLOBAL if _looks_global(quote) else SCOPE_CARD
    if not rule:
        rule = _cell(quote)[:60] or "按用户意见调整"
    return scope, rule[:80]


_GLOBAL_HINT = re.compile(
    r"以后|每次|所有|统一|默认|都|一律|别再|不要再|凡是|通用|整体|风格|格式|"
    r"别名|字数|篇幅|步骤|别写|不要写|几步|几条|不超过|最多|至少")


def _looks_global(quote):
    return bool(_GLOBAL_HINT.search(quote or ""))


def rules_for(user, title="", source_name="", kind=""):
    """取本次归纳要遵守的规则文本（拼好可直接塞进 prompt）。

    - 全局规则：全取
    - 仅本卡：只有 target 命中当前标题/来源时才取
    - 上限 30 条，避免台账变长后撑爆上下文
    """
    rows = load_feedback(user)
    if not rows:
        return ""
    keys = {k.lower() for k in (title, source_name) if k}
    picked = []
    for r in rows:
        rule = (r.get("rule") or "").strip()
        if not rule:
            continue
        if r.get("scope") == SCOPE_GLOBAL:
            picked.append(rule)
        elif r.get("scope") == SCOPE_CARD:
            t = (r.get("target") or "").lower()
            src = (r.get("source") or "").lower()
            if t and (t in " ".join(keys) or any(t in k for k in keys)):
                picked.append(rule)
            elif src and any(src in k for k in keys):
                picked.append(rule)
    # 去重保序
    seen, uniq = set(), []
    for r in picked:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    uniq = uniq[-30:]
    if not uniq:
        return ""
    return ("\n\n【用户已确认的整理规则，优先级高于上面的默认规则，必须逐条遵守】\n"
            + "\n".join("- %s" % r for r in uniq))


# ---------------- 审核动作 ----------------

def _parse_scope_prefix(text):
    """「意见 全局：xxx」→ (全局, xxx)。没写就返回 ("", 原文)。"""
    m = re.match(r"^\s*(全局|所有卡片|通用)\s*[:：,，]?\s*(.+)$", text or "", re.S)
    if m:
        return SCOPE_GLOBAL, m.group(2).strip()
    m = re.match(r"^\s*(本卡|仅本卡|只这张|这张卡)\s*[:：,，]?\s*(.+)$", text or "", re.S)
    if m:
        return SCOPE_CARD, m.group(2).strip()
    return "", (text or "").strip()


def _ledger_key_for(user, card):
    """从草稿反查 digest 台账里的源 key（用于「重做」清哈希 / 改产出路径）。"""
    import digest
    try:
        rows = digest.load_ledger_rows(user)
        for key, r in rows.items():
            if (r.get("out") or "").replace("\\", "/") == card["rel"]:
                return key
    except Exception:
        pass
    # 兜底：按标题匹配 task:xxx-标题
    t = card["title"].lower()
    try:
        for key in digest.load_ledger(user):
            if t and t in key.lower():
                return key
    except Exception:
        pass
    return ""


def _formal_path(user, card):
    """通过后的目标路径；同名已存在则加 -2 后缀。"""
    root = user_dir(user)
    d = root / card["folder"]
    p = d / ("%s.md" % card["name"])
    i = 2
    while p.exists():
        p = d / ("%s-%d.md" % (card["name"], i))
        i += 1
    return p


def _strip_draft_fm(text):
    """草稿转正时去掉只属于草稿的字段（目前没有，留作扩展）。"""
    return text


def _approve(user, card):
    src = card["path"]
    dst = _formal_path(user, card)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    rel = str(dst.relative_to(user_dir(user))).replace("\\", "/")
    try:
        from rag import index_file
        index_file(user, rel)
    except Exception:
        pass
    # 台账里把产出从 _待审/ 改成正式路径，日报/溯源才对得上
    try:
        import digest
        key = _ledger_key_for(user, card)
        if key:
            ledger = digest.load_ledger(user)
            rows = digest.load_ledger_rows(user)
            if isinstance(rows.get(key), dict):
                rows[key]["out"] = rel
                digest.save_ledger(user, ledger, rows)
    except Exception:
        pass
    return {"ok": True, "path": rel,
            "msg": "《%s》已通过，移到 %s 并补进检索。" % (card["title"], rel)}


def _reject(user, card, quote):
    root = user_dir(user)
    trash = root / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = trash / ("%s-%s.md" % (card["name"], stamp))
    card["path"].rename(dst)
    # 台账哈希先留着 —— 这正是「这个源不再反复生成」的实现（内容没变就跳过）
    return {"ok": True, "path": str(dst.relative_to(root)).replace("\\", "/"),
            "msg": "《%s》已否定，移到 .trash/（可恢复）。这个源以后不会再生成草稿。" % card["title"]}


_REWRITE_PROMPT = """你在按用户的审核意见修改一张知识库草稿卡片。

输出格式（严格遵守，不要任何前言、解释、思考过程、代码围栏）：
第一行必须正好是（英文冒号）：
ALIASES: 词1, 词2, 词3
紧接着另起一行输出卡片正文，第一行是「# 卡片标题」，之后保持原有结构。

技能卡结构：**什么时候用** / ## 操作步骤 / ## 坑 / ## 检查项
知识页结构：**一句话定义** / ## 要点 / ## 适用与边界 / ## 关联

规则：
1. 只按「本次意见」和「已确认规则」修改，不要重写没被提到的部分，更不要编造记录里没有的内容。
2. 某节没内容就整节删掉，绝对不要写「暂无记录」这种占位话。
3. 步骤要具体到能照着做，不写「学习相关知识」这类空话。
4. 别名按用户意见和规则给，宁少勿滥。

%s
本次意见：%s

当前草稿全文：
%s"""


def _revise(user, card, quote, scope, rule):
    """按意见立刻重写草稿（仍留在 _待审/，可继续审）。"""
    rules = rules_for(user, card["title"], card["title"], card["kind"])
    prompt = _REWRITE_PROMPT % (rules, quote, card["body"][-4000:])
    raw = get_llm().chat(prompt, tier="main", max_tokens=3000)
    if (raw or "").startswith("[模型调用失败]"):
        raise RuntimeError("重写失败：模型调用不成功")
    import digest
    md, _al = digest._extract(raw, card["title"], card["kind"], card["title"])
    # 保留原有 source 指向（草稿原本指向源任务/笔记）
    try:
        src_m = re.search(r'(?m)^source\s*:\s*(.+)$', card["path"].read_text(encoding="utf-8"))
        if src_m:
            md = re.sub(r'(?m)^source\s*:\s*.+$', "source: %s" % src_m.group(1).strip(), md, count=1)
    except Exception:
        pass
    card["path"].write_text(md, encoding="utf-8")
    return {"ok": True, "path": card["rel"],
            "msg": "《%s》已按意见重写，还在待审区，可以再看一遍或直接「通过」。" % card["title"]}


def _redo(user, card):
    """删掉台账里这个源的哈希，让下次归纳按最新规则重新生成。"""
    import digest
    key = _ledger_key_for(user, card)
    if not key:
        return {"ok": False, "msg": "找不到《%s》对应的归纳源，没法重做。" % card["title"]}
    ledger = digest.load_ledger(user)
    if key not in ledger:
        return {"ok": False, "msg": "《%s》在台账里没有记录，无需重做。" % card["title"]}
    ledger.pop(key)
    rows = digest.load_ledger_rows(user)
    rows.pop(key, None)
    digest.save_ledger(user, ledger, rows)
    # 旧草稿留着没意义，移进回收站，避免重做后两份并存
    if card["path"].exists():
        trash = user_dir(user) / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        card["path"].rename(trash / ("%s-重做-%s.md" % (card["name"], stamp)))
    return {"ok": True, "msg": "《%s》已解除去重标记，下次归纳会按最新反馈规则重新生成。" % card["title"]}


def act(user, query, action, opinion="", scope_hint="", dry_distill=None):
    """统一审核入口。action: approve / reject / revise / redo。

    dry_distill 仅供离线测试注入假的提炼结果，避免测试依赖网络。
    返回 {"ok", "msg", "feedback", "path"}。
    """
    card, n = find_draft(user, query)
    if not card:
        total = len(pending(user))
        return {"ok": False, "msg": ("待审区没有《%s》这张草稿。" % query) if total
                else "待审区是空的，没有可审核的草稿。"}
    if n > 1 and query.strip():
        return {"ok": False, "msg": "「%s」匹配到 %d 张草稿，说得更具体一点。" % (query, n)}
    if action == "redo":
        return _redo(user, card)

    quote = (opinion or "").strip()
    scope, hint_stripped = _parse_scope_prefix(quote)
    quote = hint_stripped
    if scope_hint:
        scope = scope_hint

    if action == "approve":
        if not quote:
            quote = "通过"
        fb_kind = APPROVE
    elif action == "reject":
        fb_kind = REJECT
    else:
        fb_kind = REVISE
        if not quote:
            return {"ok": False, "msg": "修改意见不能为空，例如「意见 codex使用：别名最多 4 个」。"}

    # 提炼规则（失败兜底成原话，绝不因为模型问题丢掉反馈）
    if dry_distill is not None:
        d_scope, d_rule = dry_distill
    else:
        d_scope, d_rule = distill(card["title"], card["kind"], fb_kind, quote, scope)
    if scope:
        d_scope = scope

    # 先记反馈，再执行动作 —— 就算后面文件操作失败，你的话也已经留档
    fb = append_feedback(user, card["title"], fb_kind, quote, d_rule, d_scope,
                          source=card.get("source", ""))

    if action == "approve":
        res = _approve(user, card)
    elif action == "reject":
        res = _reject(user, card, quote)
    else:
        try:
            res = _revise(user, card, quote, d_scope, d_rule)
        except Exception as e:
            return {"ok": False, "feedback": fb,
                    "msg": "意见已记进反馈台账，但重写草稿失败：%s（草稿没动，可重试）" % str(e)[:100]}
    res["feedback"] = fb
    res["msg"] += "\n已记入反馈台账：%s「%s」（%s）。" % (fb_kind, d_rule, d_scope)
    return res
