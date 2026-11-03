import json
import re
from typing import TypedDict

from langgraph.graph import StateGraph, END

import storage
import rag
from llm import LLMRouter

llm = LLMRouter()

CMD_PATTERNS = [
    ("start", re.compile(r"^\s*(开始|新建项目|开一个)\s*(.+)$")),
    ("switch", re.compile(r"^\s*(切到|切换到|切换)\s*(.+)$")),
    ("end", re.compile(r"^\s*(结束|完成|收工)\s*$")),
    ("continue", re.compile(r"^\s*(继续|重开)\s*(.*)$")),
    ("list", re.compile(r"^\s*(列表|项目列表|有哪些项目)\s*$")),
    ("current", re.compile(r"^\s*(当前|现在在做什么)\s*$")),
    ("leave", re.compile(r"^\s*(离开|暂停|暂离|退出|先不记了|先不记)\s*$")),
    ("note", re.compile(r"^\s*(记笔记|记下|记录|记)\s*[:：]?\s*(.+)$")),
    ("export", re.compile(r"^\s*(导出)\s*(.*)$")),
]

QUESTION_HINT = ("怎么", "如何", "为什么", "帮我查", "查一下", "搜", "搜索", "告诉我", "请问", "哪里", "哪个", "什么", "是不是")


class S(TypedDict):
    user: str
    channel: str
    text: str
    intent: str
    cmd: str
    arg: str
    project: str
    reply: str


def looks_like_question(t: str) -> bool:
    t = t.strip()
    if t.endswith("？") or t.endswith("?"):
        return True
    return any(t.startswith(k) for k in QUESTION_HINT)


def node_classify(state: S) -> S:
    text = state["text"].strip()
    for cmd, pat in CMD_PATTERNS:
        m = pat.match(text)
        if m:
            return {**state, "intent": "cmd", "cmd": cmd, "arg": (m.group(2) if m.lastindex and m.lastindex >= 2 else "").strip()}

    st = storage.load_state(state["user"])
    active = st.get("active")

    if active:
        if looks_like_question(text):
            return {**state, "intent": "qa", "project": active}
        return {**state, "intent": "step", "project": active}

    if looks_like_question(text):
        return {**state, "intent": "qa", "project": ""}

    # 无活跃项目、非命令、非疑问 → 默认记笔记（开项目必须用「开始 X」命令）
    return {**state, "intent": "note"}


def node_command(state: S) -> S:
    user, cmd, arg = state["user"], state["cmd"], state["arg"]
    st = storage.load_state(user)
    if cmd == "start":
        slug, new = storage.create_project(user, arg)
        reply = "已创建项目《%s》，当前项目已切换，第 1 步开始记录。" % arg if new else "项目《%s》已存在，已切换过去。" % arg
        return {**state, "reply": reply}
    if cmd == "switch":
        target = None
        for slug, info in st.get("projects", {}).items():
            if arg and (arg in slug or arg in info.get("name", "")):
                target = slug
                break
        if target:
            st["active"] = target
            storage.save_state(user, st)
            return {**state, "reply": "已切到《%s》（进行到第 %s 步）。" % (st["projects"][target]["name"], st["projects"][target]["steps"])}
        return {**state, "reply": "没找到项目「%s」。回复「列表」看所有项目。" % arg}
    if cmd == "end":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的项目。"}
        steps = storage.read_steps(user, slug)
        name = st["projects"][slug]["name"]
        prompt = (
            "把下面这份原始步骤记录整理成《行动攻略》，用 Markdown，包含：\n"
            "## 目标与产出物\n## 前置准备\n## 分步 SOP（编号+每步做什么）\n## 踩过的坑\n## 检查清单\n## AI 建议（未验证，可留空）\n"
            "只基于记录归纳，不要编造没发生过的内容。\n\n" + steps[-6000:]
        )
        guide = llm.chat(prompt, tier="main", max_tokens=2000)
        ver = storage.write_guide(user, slug, guide, note="生成攻略")
        st = storage.load_state(user)
        st["active"] = None
        storage.save_state(user, st)
        return {**state, "reply": "《%s》已结束，行动攻略 v%s 已生成（共 %s 步）。" % (name, ver, steps.count("[") )}
    if cmd == "continue":
        target = None
        for slug, info in st.get("projects", {}).items():
            if arg and (arg in slug or arg in info.get("name", "")):
                target = slug
                break
        if target:
            st = storage.load_state(user)
            st["active"] = target
            st["projects"][target]["status"] = "active"
            storage.save_state(user, st)
            return {**state, "reply": "《%s》已重新打开（v%s，共 %s 步），可以继续补充。" % (st["projects"][target]["name"], st["projects"][target].get("version", 0), st["projects"][target]["steps"])}
        return {**state, "reply": "没找到项目「%s」。" % arg}
    if cmd == "list":
        projs = st.get("projects", {})
        if not projs:
            return {**state, "reply": "还没有项目。发「开始 项目名」就可以建一个。"}
        lines = []
        for slug, info in projs.items():
            mark = "←进行中" if slug == st.get("active") else ("已完成" if info.get("status") == "done" else "进行中")
            lines.append("• %s（%s，%s 步，v%s）%s" % (info["name"], slug, info.get("steps", 0), info.get("version", 0), mark))
        return {**state, "reply": "\n".join(lines)}
    if cmd == "current":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的项目。"}
        info = st["projects"][slug]
        return {**state, "reply": "当前项目：《%s》，已记录 %s 步。" % (info["name"], info.get("steps", 0))}
    if cmd == "leave":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的项目。"}
        info = st.get("projects", {}).get(slug, {})
        name = info.get("name", slug)
        steps = info.get("steps", 0)
        st["active"] = None
        storage.save_state(user, st)
        return {**state, "reply": "已离开《%s》（共 %s 步），项目状态不变。下次说「切到 %s」或在项目页点「切到」继续。" % (name, steps, name)}
    if cmd == "note":
        cat, path = _categorize_and_write(user, arg)
        return {**state, "reply": "已存到「%s」：%s" % (cat, path.name)}
    if cmd == "export":
        # 「导出」列出有攻略的项目；「导出 X」生成 Word（网页端项目页也可直接下载）
        done = [(slug, info) for slug, info in st.get("projects", {}).items() if info.get("version", 0) > 0]
        if not done:
            return {**state, "reply": "还没有可导出的项目攻略（项目「结束」后自动生成攻略）。"}
        if not arg:
            lines = ["可导出的项目攻略："]
            lines += ["• %s（v%s）" % (info["name"], info.get("version", 0)) for slug, info in done]
            lines.append("回复「导出 项目名」生成 Word；网页端项目页可直接下载 Word/PDF。")
            return {**state, "reply": "\n".join(lines)}
        target = None
        for slug, info in done:
            if arg in slug or arg in info.get("name", ""):
                target = slug
                break
        if not target:
            return {**state, "reply": "没找到「%s」的攻略。回复「导出」看可导出列表。" % arg}
        try:
            import export as export_mod
            out = export_mod.export(user, target, "docx")
        except Exception as e:
            return {**state, "reply": "导出失败：%s" % e}
        return {**state, "reply": "《%s》攻略已导出为 Word，网页端「项目」页可下载（也可选 PDF）。" % st["projects"][target]["name"]}
    return {**state, "reply": "没识别的命令。"}


def node_step(state: S) -> S:
    user, slug = state["user"], state["project"]
    n = storage.append_step(user, slug, state["text"])
    st = storage.load_state(user)
    name = st["projects"][slug]["name"]
    return {**state, "reply": "✅ 已记入《%s》第 %s 步" % (name, n)}


def _categorize_and_write(user, text):
    """读目录说明让 AI 归类，返回 (分类名, 文件路径)。复用 upload 模块。"""
    import upload
    cat, rel = upload.categorize_and_write(user, text)
    return cat, storage.user_dir(user) / rel


def node_note(state: S) -> S:
    cat, path = _categorize_and_write(state["user"], state["text"])
    return {**state, "reply": "已存到「%s」：%s（说「开始 X」可开启项目记录）" % (cat, path.name)}


def node_qa(state: S) -> S:
    user, text = state["user"], state["text"]
    # 优先语义检索，失败或无结果降级到关键词检索
    hits = rag.search(user, text, limit=5)
    if hits:
        ctx = "\n".join("【%s】%s" % (h["path"], h["text"]) for h in hits)
    else:
        hits = storage.search(user, text, limit=5)
        if not hits:
            return {**state, "reply": "在自己的库里没找到相关内容。"}
        ctx = "\n".join("【%s】%s" % (h["path"], h["snippet"]) for h in hits)
    prompt = "基于下面这些笔记片段回答用户问题；没有依据就直说没找到，不要编造。\n\n笔记：\n%s\n\n问题：%s" % (ctx[:4000], text)
    ans = llm.chat(prompt, tier="main", max_tokens=800)
    return {**state, "reply": ans}


def node_ask(state: S) -> S:
    return {**state, "reply": "这句我不确定是记笔记还是要开项目：\n1️⃣ 回复「开始 项目名」开启项目记录\n2️⃣ 直接再说一遍，我就当普通笔记存起来"}


def build_graph():
    g = StateGraph(S)
    g.add_node("classify", node_classify)
    g.add_node("cmd", node_command)
    g.add_node("step", node_step)
    g.add_node("note", node_note)
    g.add_node("qa", node_qa)
    g.add_node("ask", node_ask)
    g.set_entry_point("classify")
    g.add_conditional_edges("classify", lambda s: s["intent"], {
        "cmd": "cmd", "step": "step", "note": "note", "qa": "qa", "ask": "ask",
    })
    for n in ("cmd", "step", "note", "qa", "ask"):
        g.add_edge(n, END)
    return g.compile()
