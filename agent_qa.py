# -*- coding: utf-8 -*-
"""循环式问答：模型先判断资料够不够，不够就自己选工具补检索，最多 N 轮。

改造前：检索一次 → 生成答案 → 结束（路径写死）。
改造后：判断「资料够不够」→ 够就答 / 不够自选工具（换关键词再搜、读某个笔记、
查任务进度）→ 拿到新资料回到第一步，最多 3 轮。

设计约束（重要）：
1. 三个工具全部**只读**，绝不写库；read_note 走路径白名单，防目录穿越。
2. 判断环节用免费 glm-4.7-flash 输出短 JSON；最终答案仍由 node_qa 走原来的
   tier="main"（受设置页模型开关控制），不改动用户已有的模型选择。
3. 任何异常（模型失败 / JSON 解析失败 / 工具报错）都停止补检索，用已有资料直接答，
   保证「改了不比原来差」。
4. 轮数上限硬编码 3，可用环境变量 QA_MAX_ROUNDS 覆盖（0~5），便于 dev 调试。
"""
import json
import os
import re

from config import CATEGORIES, user_dir
import storage
import rag
import tasks

try:
    MAX_ROUNDS = max(0, min(5, int(os.environ.get("QA_MAX_ROUNDS", "3"))))
except ValueError:
    MAX_ROUNDS = 3

MAX_CTX = 12000        # 送进判断模型的资料总长上限
MAX_BLOCK = 2000       # 单次工具返回资料上限
SEED_LIMIT = 4000      # 初始资料保留长度
FORBIDDEN = {"_index", "_assets", ".stversions", ".trash", ".git"}

# 判断提示词：只输出一个 JSON，不解释、不要代码块（glm-4.7-flash 实测稳定）
DECIDE_PROMPT = """你在帮用户回答一个基于他自己知识库的问题。请先判断现有资料够不够。

只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{"action":"answer|search|read_note|task_progress","arg":"...","reason":"一句话"}

- action=answer：资料已经够回答，或继续找也不会有结果。默认优先这一项。
- action=search：换个关键词再检索一次，arg 填新的检索词（别和已试记录重复）。
- action=read_note：读某个笔记全文，arg 填资料里出现过的相对路径（如 05-Skills/xxx.md）。
- action=task_progress：查某个任务的实时进度，arg 填任务名。

规则：不要为了找而找；资料为空时优先 search；arg 必须简短具体；
已试记录里的「工具+参数」不要重复；最多再找 3 轮。

问题：%s

已试记录：%s

现有资料：
%s
"""


def _client():
    """惰性创建模型客户端，便于隔离测试注入 mock。"""
    from llm import LLMRouter
    return LLMRouter()


def _decide(question, ctx, tried, client=None):
    """让模型判断下一步。返回 dict 或 None（失败即回退，不抛给主流程）。"""
    c = client or _client()
    tried_txt = "、".join("%s:%s" % (a, g) for a, g in tried) or "无"
    prompt = DECIDE_PROMPT % (str(question)[:300], tried_txt[:600], str(ctx)[-6000:])
    try:
        raw = c.chat_messages(
            [{"role": "user", "content": prompt}],
            tier="router", max_tokens=200, models=["glm-4.7-flash"])
    except Exception:
        return None
    if not raw or raw.startswith("[模型调用失败]"):
        return None
    m = re.search(r"\{[^{}]*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


# ---------------- 路径安全（只读工具专用） ----------------

def _clean_rel(rel):
    """规范化模型给的相对路径；不安全返回 None。只允许读自己库内 7 个分类的 .md。"""
    if not rel or not isinstance(rel, str):
        return None
    s = rel.strip().replace("\\", "/").strip("/")
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        return None
    parts = s.split("/")
    for p in parts:
        p = p.strip()
        if not p or p in (".", "..") or p.startswith(".") or p in FORBIDDEN:
            return None
        if re.search(r'[\x00-\x1f/\\:*?"<>|]', p):
            return None
    if parts[0] not in set(CATEGORIES):
        return None
    if not s.lower().endswith(".md"):
        return None
    return "/".join(parts)


def _abs(user, rel):
    """resolve + startswith 双保险，防止符号链接或 .. 逃出用户库。"""
    root = user_dir(user).resolve()
    p = (root / rel).resolve()
    if p == root or not str(p).startswith(str(root) + os.sep):
        return None
    return p


# ---------------- 三个只读工具 ----------------

def tool_search(user, arg):
    """换关键词再搜：先语义检索，没结果回退关键词检索。"""
    q = str(arg or "").strip()[:120]
    if not q:
        return "", [], "空关键词"
    try:
        hits = rag.search(user, q, limit=5)
        if hits:
            body = "\n".join("【%s】%s" % (h["path"], str(h["text"])[:400]) for h in hits)
            return ("补充检索（%s）：\n%s" % (q, body))[:MAX_BLOCK], \
                   [h["path"] for h in hits], ""
        rows = storage.search(user, q, limit=5)
        if rows:
            body = "\n".join("【%s】%s" % (r["path"], str(r["snippet"])[:400]) for r in rows)
            return ("补充检索（%s，关键词）：\n%s" % (q, body))[:MAX_BLOCK], \
                   [r["path"] for r in rows], ""
    except Exception as e:
        return "", [], str(e)[:100]
    return "", [], "无新增结果"


def tool_read_note(user, arg):
    """读某个笔记全文。路径必须过白名单，且文件真实存在。"""
    rel = _clean_rel(arg)
    if not rel:
        return "", [], "路径不安全或不在允许的分类里"
    p = _abs(user, rel)
    if not p or not p.is_file():
        return "", [], "文件不存在"
    try:
        body = p.read_text(encoding="utf-8")
    except Exception as e:
        return "", [], str(e)[:100]
    return ("读取全文【%s】：\n%s" % (rel, body))[:MAX_BLOCK], [rel], ""


def tool_task_progress(user, arg):
    """查任务实时进度（从 Markdown 文件现读，比 RAG 准）。"""
    name = str(arg or "").strip()[:80]
    if not name:
        return "", [], "没给任务名"
    try:
        slug = tasks.find_task(user, name)
        if not slug:
            return "", [], "没找到这个任务"
        brief = tasks.task_brief(user, slug)
        if not brief:
            return "", [], "任务无摘要"
        return ("任务进度【%s】：\n%s" % (slug, brief))[:MAX_BLOCK], \
               ["01-Projects/" + slug], ""
    except Exception as e:
        return "", [], str(e)[:100]


ACTIONS = {
    "search": tool_search,
    "read_note": tool_read_note,
    "task_progress": tool_task_progress,
}


# ---------------- 主循环 ----------------

def gather(user, question, seed_text="", seed_sources=None,
           max_rounds=None, decide=None, tools=None):
    """跑循环，返回 (context_text, sources, trace)。

    context_text：初始资料 + 各轮补充资料，可直接拼进最终回答 prompt。
    sources：累计引用的路径（去重保序），供「存这条」和来源标注使用。
    trace：每轮决策与结果，用于验证与排障（生产不展示给用户）。
    """
    rounds = MAX_ROUNDS if max_rounds is None else max(0, min(5, int(max_rounds)))
    tools = tools or ACTIONS
    decide = decide or _decide
    seed = str(seed_text or "")[:SEED_LIMIT]
    sources = list(dict.fromkeys(str(s) for s in (seed_sources or []) if s))
    extras, tried, trace = [], [], []
    consecutive_dup = 0

    def ctx_now():
        s = seed + ("\n\n" + "\n\n".join(extras) if extras else "")
        return s[-MAX_CTX:] if len(s) > MAX_CTX else s

    for i in range(rounds):
        d = decide(question, ctx_now(), tried)
        if not d:
            trace.append({"round": i + 1, "action": "fallback",
                          "note": "判断失败，停止补检索"})
            break
        action = str(d.get("action") or "answer").strip().lower()
        arg = str(d.get("arg") or "").strip()
        if action == "answer" or action not in tools:
            trace.append({"round": i + 1, "action": "answer",
                          "reason": str(d.get("reason") or "")[:80]})
            break
        key = (action, arg)
        if key in tried:
            consecutive_dup += 1
            trace.append({"round": i + 1, "action": action, "arg": arg[:80],
                          "note": "重复，跳过"})
            if consecutive_dup >= 2:
                trace.append({"round": i + 1, "action": "break",
                              "note": "连续重复，停止"})
                break
            continue
        consecutive_dup = 0
        tried.append(key)
        try:
            txt, paths, err = tools[action](user, arg)
        except Exception as e:
            txt, paths, err = "", [], str(e)[:100]
        trace.append({"round": i + 1, "action": action, "arg": arg[:80],
                      "got": bool(txt), "err": str(err)[:80]})
        if txt:
            extras.append(txt)
            for p in paths:
                p = str(p)
                if p not in sources:
                    sources.append(p)
    return ctx_now(), sources, trace
