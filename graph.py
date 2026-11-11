import json
import re
from typing import TypedDict

from langgraph.graph import StateGraph, END

import storage
import tasks
import rag
import skills
import video
import fileops
from llm import LLMRouter

llm = LLMRouter()

CMD_PATTERNS = [
    ("start", re.compile(r"^\s*(开始|新建项目|开一个|开工)\s*(.+)$")),
    ("switch", re.compile(r"^\s*(切到|切换到|切换)\s*(.+)$")),
    ("todo", re.compile(r"^\s*(想做|待办|加个待办|加入待办|计划做)\s*[:：]?\s*(.+)$")),
    ("addchild", re.compile(r"^\s*(?:添加子任务|增加子任务|新增子任务|建子任务|加子任务|加个子任务|新建子任务|子任务)\s*[:：]?\s*(?P<arg>.+)$")),
    # 不带名字时给提示，别让「加子任务」这句话掉进步骤记录里
    ("addchild", re.compile(r"^\s*(?:添加子任务|增加子任务|新增子任务|建子任务|加子任务|加个子任务|新建子任务|子任务)\s*[:：]?\s*$")),
    ("stepdone", re.compile(r"^\s*(完成|搞定|做完|勾掉)\s*第?\s*(\d+)\s*[步项]?\s*$")),
    ("taskdone", re.compile(r"^\s*(任务完成|标记完成)\s*(.*)$")),
    ("linkadd", re.compile(r"^\s*(关联|相关联?于)\s*[:：]?\s*(.+)$")),
    ("setparent", re.compile(r"^\s*(上级|父任务)\s*[:：]?\s*(.+)$")),
    ("skillmake", re.compile(r"^\s*(沉淀|存为技能|做成技能|整理成技能)\s*[:：]?\s*(.*)$")),
    ("skilllist", re.compile(r"^\s*(技能列表|有哪些技能|技能清单|技能卡)\s*$")),
    # 归纳草稿审核（review.py）：名字与意见用冒号分隔，名字可含空格
    ("reviewlist", re.compile(r"^\s*(待审|待审列表|审核列表|审核)\s*$")),
    ("reviewfeedback", re.compile(r"^\s*(反馈台账|审核记录|我的意见|反馈列表)\s*$")),
    ("reviewapprove", re.compile(r"^\s*(通过|赞同|采用)\s*[:：]?\s*(?P<arg>.+)$")),
    ("reviewreject", re.compile(r"^\s*(否定|驳回|不要这张)\s*[:：]?\s*(?P<arg>.+)$")),
    ("reviewrevise", re.compile(r"^\s*(意见|修改意见|改一下|改改)\s*[:：]?\s*(?P<arg>.+)$")),
    ("reviewredo", re.compile(r"^\s*(重做|重新归纳)\s*[:：]?\s*(?P<arg>.+)$")),
    # 视频字幕：带动词的链接、裸的视频站链接、以及列表/查看
    ("videolist", re.compile(r"^\s*(视频列表|字幕列表|有哪些视频|视频笔记)\s*$")),
    ("videomake", re.compile(r"^\s*(?:总结|转写|提取字幕|看下|看看)\s*(?P<url>https?://\S+)(?:\s*(?:存到|归到|归入|放到|存入)\s*(?P<series>.+?))?\s*$")),
    ("videomake", re.compile(r"^\s*(?P<arg>https?://\S+)\s*$")),
    ("videoshow", re.compile(r"^\s*(?:看视频|视频笔记|视频)\s*[:：]?\s*(?!https?://)(?P<arg>.+)$")),
    # 「用 X / 按 X 做 / 技能 X」——只有 X 能对上技能卡时才算命令（见 node_classify 的二次校验）
    ("skilluse", re.compile(r"^\s*(?:用|按|照着?|跟着?|调用|技能)\s*[《「]?(?P<arg>.+?)[》」]?\s*(?:来做|来走|做|走)?\s*$")),
    ("skilluse", re.compile(r"^\s*(?:怎么做|如何做|怎么弄)\s*[《「]?(?P<arg>.+?)[》」]?\s*$")),
    ("end", re.compile(r"^\s*(结束|完成|收工)\s*$")),
    ("continue", re.compile(r"^\s*(继续|重开)\s*(.*)$")),
    ("list", re.compile(r"^\s*(列表|项目列表|任务列表|有哪些项目|有哪些任务)\s*$")),
    ("current", re.compile(r"^\s*(当前|现在在做什么|当前任务)\s*$")),
    ("leave", re.compile(r"^\s*(离开|暂停|暂离|退出|先不记了|先不记)\s*$")),
    ("note", re.compile(r"^\s*(记笔记|记下|记录|记)\s*[:：]?\s*(.+)$")),
    ("export", re.compile(r"^\s*(导出)\s*(.*)$")),
    ("linkpick", re.compile(r"^\s*(挂|挂上)\s*([0-9０-９,，、\s]+)\s*$")),
]

QUESTION_HINT = ("怎么", "如何", "为什么", "帮我查", "查一下", "搜", "搜索", "告诉我", "请问", "哪里", "哪个", "什么", "是不是")
# 句中出现这些词也当提问（否则在记录模式下会被误记成一步）
QUESTION_CONTAINS = ("还差", "还剩", "剩下", "哪些没", "哪几步", "还有哪", "进度",
                     "做到哪", "多少步", "完成了吗", "怎么样了", "总结一下", "回顾")


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
    if any(k in t for k in QUESTION_CONTAINS):
        return True
    return any(t.startswith(k) for k in QUESTION_HINT)


# 写笔记后的「相似笔记推荐」候选，供「挂 N」确认（进程内存，重启即清，无副作用）
_RECENT_LINK = {}


def _parse_nums(s):
    full = "０１２３４５６７８９"
    half = "0123456789"
    s = (s or "").translate(str.maketrans(full, half))
    return [int(x) for x in re.findall(r"\d+", s)]


_CN_RE = re.compile(r"[\u4e00-\u9fff]")
# 分享文案特征词（抖音/快手「复制打开…」那一套）
_SHARE_HINT = re.compile(r"复制|打开抖音|打开斗音|直接观看|观看视频")
# 用户跟在链接后面的处理意图词（「帮我处理这个视频」这种最容易被漏掉）
_INTENT_HINT = re.compile(r"帮我|处理|总结|转写|提取字幕|转文字|翻译|看下|看看|讲什么|说什么")
# 「记 …」是明确要存笔记，不能被视频识别抢走
_NOTE_PREFIX = re.compile(r"^\s*(记笔记|记下|记录|记)\s*[:：]?")


def _looks_like_share(text):
    """认「App 复制出来的分享文案」，返回干净 URL；不像就返回空串。

    真实形态：`https://v.douyin.com/xxxx/ z@t.EH 11/10 :3pm oQX:/`
    整句只有一个 URL 的正则接不住这种（后面还挂着口令），所以单独判一次。
    判据：
    - 不长（<=400 字）、且不是「记 …」开头（那是用户明确要存笔记）
    - 句子里能抠出视频站链接
    - 链接之外的部分满足其一：几乎没有中文（纯口令乱码）／带分享特征词／带处理意图词
      （「帮我处理这个视频」是最常见的真实说法）
    """
    text = (text or "").strip()
    if not text or len(text) > 400 or _NOTE_PREFIX.match(text):
        return ""
    url = video.extract_url(text)
    if not url or not video.is_video_url(url):
        return ""
    i = text.find(url)
    rest = (text[:i] + " " + text[i + len(url):]).strip()
    if not rest:
        return url
    if not _CN_RE.search(rest):
        return url
    if _SHARE_HINT.search(text) or _INTENT_HINT.search(rest):
        return url
    return ""


_SERIES_RE = re.compile(r"(?:存到|归到|归入|放到|存入)\s*(.+?)\s*$")


def _series_of(text):
    """从整句里抠「存到/归到 … 系列名」，没有就返回空串。"""
    m = _SERIES_RE.search(text or "")
    return (m.group(1).strip() if m else "")


def _split_parent(arg):
    """「学调色 属于 剪辑视频学习」→ (学调色, 剪辑视频学习)"""
    m = re.match(r"^(.+?)\s*(?:属于|上级是|归到|挂在|挂到)\s*(.+)$", arg.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return arg.strip(), ""


def _split_names(arg):
    """按顿号/逗号/空格拆多个任务名。"""
    parts = re.split(r"[、,，;；\s]+", (arg or "").strip())
    return [p for p in (x.strip() for x in parts) if p]


def _split_task_names(arg):
    """按顿号/逗号/分号拆多个任务名；不按空格拆，因为任务名本身可以带空格。"""
    parts = re.split(r"[、,，;；]+", (arg or "").strip())
    return [p for p in (x.strip() for x in parts) if p]


def _split_review_arg(arg):
    """「通过 codex使用：挺好」→ ("codex使用", "挺好")。

    用冒号分隔而不按空格切：草稿名可能含空格。没冒号时整串当名字、意见为空。
    「意见 codex使用：全局：别名最多 4 个」→ 名字 + 带范围前缀的意见（由 review 再解析）。
    """
    m = re.match(r"^(.+?)\s*[:：]\s*(.+)$", (arg or "").strip(), re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (arg or "").strip(), ""


def _parent_missing(user, parent, just_created=()):
    """父任务是否还不存在。

    语义和 tasks.render_index 的 `p in by_name` 保持一致（按任务名精确匹配），
    免得提示和实际缩进结果对不上。`just_created` 是本次刚建出来的名字，
    用于「想做 A、B 属于 A」这种同一批里父子一起建的情况。
    """
    if not parent or parent in just_created:
        return False
    names = {t["name"] for t in tasks.list_tasks(user)}
    return parent not in names


PARENT_MISSING_HINT = ("⚠ 上级《%s》还不存在，层级暂时不会缩进显示。"
                       "发「想做 %s」把它建出来就会自动接上（不用补挂）。")


def _pattern_arg(m):
    """取匹配到的参数：新命令用命名组，老命令用第 2 组。"""
    d = m.groupdict()
    if d.get("url"):
        return d["url"].strip()
    if "arg" in d:
        return (d["arg"] or "").strip()
    return (m.group(2) if m.lastindex and m.lastindex >= 2 else "").strip()


def node_classify(state: S) -> S:
    text = state["text"].strip()
    # 先认「App 复制的分享文案」：链接和口令粘成一句，下面按固定句式写的规则接不住
    share = _looks_like_share(text)
    if share:
        return {**state, "intent": "cmd", "cmd": "videomake", "arg": share,
                "series": _series_of(text)}
    for cmd, pat in CMD_PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        arg = _pattern_arg(m)
        # 「用 剪映导出素材」「用关键帧做过场」这类记录句不能当命令：
        # 只有整句就是卡名/别名时才执行（半句话交给问 AI 那条路，照样能答）
        if cmd == "skilluse" and not skills.exact_card(state["user"], arg):
            continue
        # 裸链接只有落在视频站上才当视频处理；其它链接照旧存成笔记/资源
        if cmd == "videomake" and not m.groupdict().get("url") and not video.is_video_url(arg):
            continue
        series = (m.groupdict().get("series") or "").strip() if cmd == "videomake" else ""
        return {**state, "intent": "cmd", "cmd": cmd, "arg": arg, "series": series}

    # 文件操作预筛（零成本粗召回）：命中才交给模型判，没命中一律走原链路。
    # 放在 active 判断之前，保证「说句话就照做」在做任务时也生效；
    # 模型判 none 时 node_fileop 会回落到 _classify_rest，行为与原来完全一致。
    if fileops.looks_like_op(text):
        return {**state, "intent": "fileop", "cmd": "", "arg": "", "series": ""}

    return _classify_rest(state)


def _classify_rest(state: S) -> S:
    """去掉命令表与文件操作预筛后的原有分流逻辑，供 node_fileop 判 none 时回落。"""
    text = state["text"].strip()
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

    if cmd == "linkpick":
        rec = _RECENT_LINK.get(user)
        if not rec or not rec.get("cands"):
            return {**state, "reply": "现在没有待关联的笔记。先写一条笔记，系统会推荐相关的。"}
        nums = _parse_nums(arg)
        cands = rec["cands"]
        picked = [cands[i - 1] for i in nums if 1 <= i <= len(cands)]
        if not picked:
            opts = "  ".join("%d《%s》" % (i + 1, c["title"]) for i, c in enumerate(cands))
            return {**state, "reply": "编号不对。可选：%s" % opts}
        n = skills.link_notes(user, rec["note"], picked)
        if n:
            names = "、".join("《%s》" % c["title"] for c in picked)
            return {**state, "reply": "已关联 %d 篇：%s（Obsidian 星图里会连线）。" % (n, names)}
        return {**state, "reply": "这些已经关联过了。"}

    if cmd == "start":
        name, parent = _split_parent(arg)
        slug, new = storage.create_project(user, name)
        if not slug:
            return {**state, "reply": "任务名不能为空，比如「开始 剪辑视频学习」。"}
        if parent:
            tasks.set_parent(user, slug, parent)
        hint = ("\n" + PARENT_MISSING_HINT % (parent, parent)) if _parent_missing(user, parent, (name,)) else ""
        if not new:
            return {**state, "reply": "任务《%s》已存在，已切换过去。%s" % (name, hint)}
        if parent:
            return {**state, "reply": "已创建任务《%s》（挂在《%s》下），开始记录，说完发「结束」。%s" % (name, parent, hint)}
        return {**state, "reply": "已创建任务《%s》，开始记录，说完发「结束」生成行动攻略。" % name}

    if cmd == "todo":
        raw, parent = _split_parent(arg)
        names = _split_task_names(raw)
        if not names:
            return {**state, "reply": "任务名不能为空，比如「想做 剪辑视频学习」。"}
        made, exist = [], []
        for n in names:
            slug, new = tasks.create_task(user, n, status="todo", parent=parent or None)
            if slug and new:
                made.append(n)
            elif slug:
                exist.append(n)
        lines = []
        if made:
            lines.append("已加入待办：" + "、".join("《%s》" % n for n in made))
        if exist:
            lines.append("已存在：" + "、".join("《%s》" % n for n in exist))
        if parent:
            lines.append("（上级：%s）" % parent)
            if _parent_missing(user, parent, made):
                lines.append(PARENT_MISSING_HINT % (parent, parent))
        if made:
            lines.append("开始做的时候说「开始 %s」。" % made[0])
        return {**state, "reply": "\n".join(lines)}

    if cmd == "addchild":
        parent_slug = st.get("active")
        if not parent_slug:
            return {**state, "reply": ("先「开始 X」或「切到 X」进入一个任务，再发「加子任务 Y」，"
                                       "Y 会自动挂在当前任务下面。")}
        p = tasks.read_task(user, parent_slug) or {}
        pname = p.get("name") or parent_slug
        names = _split_task_names(arg)
        if not names:
            return {**state, "reply": "子任务名不能为空，比如「加子任务 飞书内容」。\n"
                                      "也认「添加子任务 / 建子任务 / 新建子任务 / 子任务」，"
                                      "一次多个用顿号分隔。"}
        made, exist = [], []
        for n in names:
            slug, new = tasks.create_task(user, n, status="todo", parent=pname)
            if slug and new:
                made.append(n)
            elif slug:
                exist.append(n)
        lines = []
        if made:
            lines.append("已在《%s》下加了子任务：%s" % (pname, "、".join("《%s》" % n for n in made)))
        if exist:
            lines.append("已存在：" + "、".join("《%s》" % n for n in exist))
        lines.append("当前任务仍是《%s》。要往下做某层，发「切到 名字」；再往下加一层，"
                     "就在那层里再发「加子任务 名字」。" % pname)
        return {**state, "reply": "\n".join(lines)}

    if cmd == "switch":
        target = tasks.find_task(user, arg)
        if not target:
            return {**state, "reply": "没找到任务「%s」。回复「列表」看全部任务。" % arg}
        st["active"] = target
        storage.save_state(user, st)
        t = tasks.read_task(user, target) or {}
        return {**state, "reply": "已切到《%s》（%s，%d/%d 步）。" % (
            t.get("name", arg), tasks.STATUS_TEXT.get(t.get("status"), ""), t.get("done", 0), t.get("total", 0))}

    if cmd == "end":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的任务。发「列表」看看有哪些。"}
        t = tasks.read_task(user, slug) or {}
        name = t.get("name", slug)
        steps = storage.read_steps(user, slug)
        if not steps.strip():
            return {**state, "reply": "《%s》还没有记录任何步骤，先记几条再结束。" % name}
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
        return {**state, "reply": (
            "《%s》已结束，行动攻略 v%s 已生成（共 %d 步）。\n\n"
            "这次学到的东西要沉淀成技能卡吗？发「沉淀 技能名」就行（例如「沉淀 关键帧操作」），不需要就忽略。"
            % (name, ver, t.get("total", 0)))}

    if cmd == "continue":
        target = tasks.find_task(user, arg) or st.get("active")
        if not target:
            return {**state, "reply": "没找到任务「%s」。" % arg}
        st["active"] = target
        storage.save_state(user, st)
        tasks.set_status(user, target, "doing")
        t = tasks.read_task(user, target) or {}
        return {**state, "reply": "《%s》已重新打开（%d/%d 步），可以继续补充。" % (
            t.get("name", target), t.get("done", 0), t.get("total", 0))}

    if cmd == "list":
        ts = tasks.list_tasks(user)
        if not ts:
            return {**state, "reply": "还没有任务。发「想做 任务名」登记想法，或「开始 任务名」直接开工。"}
        lines = []
        for key, label in (("doing", "进行中"), ("todo", "待办"), ("done", "已完成")):
            group = [t for t in ts if t["status"] == key]
            if not group:
                continue
            lines.append("【%s】%d 个" % (label, len(group)))
            for t in sorted(group, key=lambda x: x["mtime"], reverse=True)[:10]:
                if t["status"] == "done":
                    prog = "攻略 v%s" % tasks._fmt_ver(t["version"]) if t["version"] else "已完成"
                elif t["total"]:
                    prog = "%d/%d 步" % (t["done"], t["total"])
                else:
                    prog = "还没开始"
                cur = " ←当前" if t["slug"] == st.get("active") else ""
                lines.append("• %s（%s）%s" % (t["name"], prog, cur))
        return {**state, "reply": "\n".join(lines)}

    if cmd == "current":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的任务。"}
        return {**state, "reply": tasks.task_brief(user, slug)}

    if cmd == "leave":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的任务。"}
        t = tasks.read_task(user, slug) or {}
        st["active"] = None
        storage.save_state(user, st)
        return {**state, "reply": "已离开《%s》（%d/%d 步），任务状态不变。下次说「切到 %s」继续。" % (
            t.get("name", slug), t.get("done", 0), t.get("total", 0), t.get("name", slug))}

    if cmd == "stepdone":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "当前没有进行中的任务。"}
        if tasks.set_step_done(user, slug, int(arg)):
            return {**state, "reply": "第 %s 步已标记完成。" % arg}
        return {**state, "reply": "没找到第 %s 步。" % arg}

    if cmd == "taskdone":
        slug = tasks.find_task(user, arg) if arg.strip() else st.get("active")
        if not slug:
            return {**state, "reply": "没找到该任务。发「列表」看看。"}
        tasks.set_status(user, slug, "done")
        t = tasks.read_task(user, slug) or {}
        if st.get("active") == slug:
            st["active"] = None
            storage.save_state(user, st)
        return {**state, "reply": "《%s》已标记完成。" % t.get("name", slug)}

    if cmd == "linkadd":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "先切到一个任务，再说「关联 另一个任务名」。"}
        rel = tasks.add_related(user, slug, _split_names(arg))
        if not rel:
            return {**state, "reply": "关联失败，确认任务名。"}
        return {**state, "reply": "已关联：%s（Obsidian 里这两张卡会连线）。" % "、".join("[[%s]]" % r for r in rel)}

    if cmd == "setparent":
        slug = st.get("active")
        if not slug:
            return {**state, "reply": "先切到一个任务，再说「上级 父任务名」。"}
        parent = arg.strip()
        # 先取名字：set_parent 成功后目录可能已被搬进父目录，旧 slug 会失效
        cur_name = (tasks.read_task(user, slug) or {}).get("name", slug)
        if tasks.set_parent(user, slug, parent):
            msg = "已把《%s》的上级设为《%s》。" % (cur_name, parent)
            if _parent_missing(user, parent):
                msg += "\n" + PARENT_MISSING_HINT % (parent, parent)
            else:
                msg += "总览里会缩进显示。"
            return {**state, "reply": msg}
        return {**state, "reply": "设置失败：上级不能是它自己。"}

    if cmd == "skillmake":
        import digest
        slug = st.get("active")
        if not slug:
            done = sorted([t for t in tasks.list_tasks(user) if t["status"] == "done"],
                          key=lambda x: x["mtime"], reverse=True)
            if not done:
                return {**state, "reply": "没有已完成的任务可以沉淀。先把一个任务「结束」掉。"}
            slug = done[0]["slug"]
        title = arg.strip() or (tasks.read_task(user, slug) or {}).get("name", slug)
        msg = digest.make_skill_card(user, slug, title)
        if "已生成" in msg:
            msg += "\n下次要做同类事，发「用 %s」把卡里的步骤变成任务，边做边勾。" % title
        return {**state, "reply": msg}

    if cmd == "skilllist":
        items = skills.list_cards(user)
        if not items:
            return {**state, "reply": "05-Skills 还是空的。任务「结束」后发「沉淀 技能名」就能生成技能卡。"}
        lines = ["技能卡 / 知识页共 %d 张：" % len(items)]
        for c in items[:15]:
            tag = "技能卡" if c["kind"] == "skill" else "知识页"
            extra = "%d 步" % len(c["steps"]) if c["steps"] else "无步骤"
            lines.append("• %s（%s·%s）" % (c["title"], tag, extra))
        if len(items) > 15:
            lines.append("…还有 %d 张，去网页「技能」页看全部。" % (len(items) - 15))
        lines.append("")
        lines.append("发「用 卡名」按卡里的步骤开一条任务，带你一步步走；写笔记或问 AI 时命中卡片会自动带上它。")
        lines.append("定时归纳的草稿在 _待审/，发「待审」看有哪些、发「通过 名字」转正。")
        return {**state, "reply": "\n".join(lines)}

    # ---- 归纳草稿审核（review.py）----
    if cmd == "reviewlist":
        import review
        return {**state, "reply": review.brief(user)}

    if cmd == "reviewfeedback":
        import review
        rows = review.load_feedback(user)
        if not rows:
            return {**state, "reply": "反馈台账还是空的。审核草稿时说的话会记到 "
                                      "_index/归纳反馈.md，下次归纳照着做。"}
        lines = ["已记下 %d 条审核反馈（台账在 _index/归纳反馈.md）：" % len(rows)]
        for r in rows[-15:]:
            lines.append("• [%s·%s] %s → %s" % (r["kind"], r["scope"],
                                              r["quote"][:36], r["rule"]))
        if len(rows) > 15:
            lines.append("…还有 %d 条，去 Obsidian 里打开台账看全部。" % (len(rows) - 15))
        lines.append("不想让某条生效，把那行删掉即可。")
        return {**state, "reply": "\n".join(lines)}

    if cmd in ("reviewapprove", "reviewreject", "reviewrevise", "reviewredo"):
        import review
        name, opinion = _split_review_arg(arg)
        action = {"reviewapprove": "approve", "reviewreject": "reject",
                  "reviewrevise": "revise", "reviewredo": "redo"}[cmd]
        if not name:
            return {**state, "reply": "要说清是哪张草稿，例如「通过 codex使用」。发「待审」看列表。"}
        res = review.act(user, name, action, opinion=opinion)
        return {**state, "reply": res["msg"]}

    if cmd == "skilluse":
        res = skills.use_card(user, arg)
        if not res.get("ok"):
            return {**state, "reply": res.get("err", "没找到这张技能卡") + "。发「技能列表」看有哪些。"}
        card = res["card"]
        if not res.get("slug"):
            # 知识页 / 没有步骤的卡：给内容，不硬造任务
            return {**state, "reply": "《%s》是知识页，没有可执行步骤，先看内容：\n\n%s\n\n"
                                      "想动手做的话，发「开始 任务名」边做边记，做完再「沉淀」成技能卡。"
                                      % (card["title"], skills.card_digest(card, 1200))}
        t = tasks.read_task(user, res["slug"]) or {}
        steps = t.get("steps", [])
        lines = ["按技能卡《%s》%s（共 %d 步，已设为当前任务）：" % (
            card["title"], "开了任务" if not res["existed"] else "接上任务", len(steps)), ""]
        for s in steps:
            lines.append("%d. %s" % (s["n"], s["text"]))
        lines.append("")
        lines.append("做完一步发「完成 N」，或网页/手机 Obsidian 里直接勾。")
        if res["added"] == 0 and res["existed"]:
            lines.append("（这张卡的步骤之前已经导进任务了，没重复添加）")
        if card["pits"]:
            lines.append("卡里记的坑：" + "；".join(card["pits"][:3]))
        if card["checks"]:
            lines.append("检查项 %d 条，收尾时对着核一遍（见 %s）。" % (len(card["checks"]), card["rel"]))
        return {**state, "reply": "\n".join(lines)}

    if cmd == "videolist":
        items = video.list_notes(user, 30)
        if not items:
            return {**state, "reply": "还没有视频速览。发一个 B 站链接给我（或网页「视频」页上传文件），"
                                      "我会把字幕取下来做一份速览，存在 %s/。" % video.VIDEO_DIR}
        lines = ["视频速览 %d 条（在 %s/，不进检索）：" % (len(items), video.VIDEO_DIR)]
        for it in items[:10]:
            bit = "（%s ｜ %s）" % (it["duration"] or "-", it["method"] or "-")
            gist = ("｜" + it["gist"][:38]) if it["gist"] else ""
            lines.append("• %s%s%s" % (it["title"], bit, gist))
        lines.append("发「看视频 标题关键词」看章节与要点；完整字幕在网页「视频」页。")
        return {**state, "reply": "\n".join(lines)}

    if cmd == "videoshow":
        items = video.list_notes(user, 200)
        q = arg.strip()
        hit = [x for x in items if q and q in x["title"]] or \
              [x for x in items if q and q in (x["gist"] or "")]
        if not hit:
            return {**state, "reply": "没找到视频笔记「%s」。发「视频列表」看看有哪些。" % q}
        it = hit[0]
        lines = ["《%s》（%s ｜ %s ｜ %s）" % (it["title"], it["duration"] or "-",
                                          it["method"] or "-", it["created"] or "-"), ""]
        if it["gist"]:
            lines += ["一句话：" + it["gist"], ""]
        if it["sections"]:
            lines.append("章节：")
            lines += ["• [%s] %s" % (s["t"], s["text"]) for s in it["sections"][:12]]
            lines.append("")
        if it["points"]:
            lines.append("要点：")
            lines += ["• " + p for p in it["points"][:8]]
            lines.append("")
        lines.append("完整字幕：%s（网页「视频」页可看全文）" % it["rel"])
        return {**state, "reply": "\n".join(lines)}

    if cmd == "videomake":
        url = arg
        series = state.get("series", "").strip()
        if not video.ffmpeg_exe():
            return {**state, "reply": "服务器没装 ffmpeg，转写跑不起来（装一下 imageio-ffmpeg 就好）。"}
        if not video.is_url(url):
            return {**state, "reply": "给我一个完整的视频链接（http/https 开头）。"}
        old = video.find_by_source(user, url)
        if old:
            return {**state, "reply": "这个链接已经总结过了：《%s》（%s）。\n发「看视频 %s」看要点和章节。"
                                      % (old["title"], old["duration"], old["title"])}
        dest = "%s/%s/" % (video.VIDEO_DIR, series) if series else "%s/" % video.VIDEO_DIR
        video.start_job(user, url=url, series=series)
        return {**state, "reply": (
            "已开始处理这个视频%s。\n"
            "先看平台有没有现成字幕（有就 0 成本）；没有就下音频、切段转写（约 0.015 元/分钟）。\n"
            "跑完存到 %s，**不进检索**，不会冲淡搜索。\n"
            "之后发「视频列表」看结果，或开网页「视频」页看章节和完整字幕。"
            % (("，归入系列「%s」" % series) if series else "", dest))}

    if cmd == "note":
        cat, path, card = _write_note(user, arg)
        reply = "已存到「%s」：%s" % (cat, path.name)
        if card:
            reply += "\n顺带关联到技能卡《%s》：想按卡里的步骤做，发「用 %s」。" % (card["title"], card["title"])
        return {**state, "reply": reply}

    if cmd == "export":
        done = [(t["slug"], t) for t in tasks.list_tasks(user) if t["version"] > 0]
        if not done:
            return {**state, "reply": "还没有可导出的攻略（任务「结束」后自动生成攻略）。"}
        if not arg:
            lines = ["可导出的行动攻略："]
            lines += ["• %s（v%s）" % (t["name"], tasks._fmt_ver(t["version"])) for _, t in done]
            lines.append("回复「导出 任务名」生成 Word；网页端任务页可直接下载 Word/PDF。")
            return {**state, "reply": "\n".join(lines)}
        target = tasks.find_task(user, arg)
        if not target or not (tasks.read_task(user, target) or {}).get("version"):
            return {**state, "reply": "没找到「%s」的攻略。回复「导出」看可导出列表。" % arg}
        try:
            import export as export_mod
            export_mod.export(user, target, "docx")
        except Exception as e:
            return {**state, "reply": "导出失败：%s" % e}
        return {**state, "reply": "《%s》攻略已导出为 Word，网页端任务页可下载（也可选 PDF）。" % (
            (tasks.read_task(user, target) or {}).get("name", target))}

    return {**state, "reply": "没识别的命令。"}


def node_step(state: S) -> S:
    user, slug = state["user"], state["project"]
    n = storage.append_step(user, slug, state["text"])
    t = tasks.read_task(user, slug) or {}
    if n == 1:
        return {**state, "reply": "已记入《%s》第 1 步。做完发「完成 1」，或直接在网页/Obsidian 里勾选。" % t.get("name", slug)}
    return {**state, "reply": "已记入第 %d 步（%d/%d）。" % (n, t.get("done", 0), t.get("total", 0))}


def _categorize_and_write(user, text):
    """读目录说明让 AI 归类，返回 (分类名, 文件路径)。复用 upload 模块。"""
    import upload
    cat, rel = upload.categorize_and_write(user, text)
    return cat, storage.user_dir(user) / rel


def _write_note(user, text):
    """写笔记 + 顺手挂技能卡双链（A 向召回的最小实现）。

    规则：笔记里出现某张卡的标题/别名（strict 模式）才挂，避免乱连线。
    返回 (分类, 路径, 卡片或 None)。
    """
    cat, path = _categorize_and_write(user, text)
    card = None
    try:
        hits = skills.related(user, text, limit=1, strict=True)
        card = hits[0] if hits else None
        if card:
            skills.link_note(user, path, card)   # 已挂过会自己跳过
    except Exception:
        card = None
    return cat, path, card


def node_note(state: S) -> S:
    user, text = state["user"], state["text"]
    cat, path, card = _write_note(user, text)
    reply = "已存到「%s」：%s（说「开始 X」可开启项目记录）" % (cat, path.name)
    if card:
        reply += "\n这条内容命中技能卡《%s》，已挂双链（Obsidian 里会连线）。" \
                 "想按卡里的步骤做，发「用 %s」。" % (card["title"], card["title"])
    # 相似笔记推荐（语义召回，经「挂 N」确认才写双链，避免乱连）
    try:
        rel = str(path.relative_to(storage.user_dir(user))).replace("\\", "/")
        cands = skills.recommend_notes(user, text, exclude_rel=rel, limit=3)
    except Exception:
        cands = []
    if cands:
        _RECENT_LINK[user] = {"note": path, "cands": cands}
        items = "  ".join("%d《%s》" % (i + 1, c["title"]) for i, c in enumerate(cands))
        reply += "\n可能相关：%s（发「挂 1 2」关联到这篇，不需要就无视）" % items
    return {**state, "reply": reply}


def node_qa(state: S) -> S:
    user, text = state["user"], state["text"]
    parts = []
    # 技能卡优先：库里已沉淀的流程比零散笔记更贴近「怎么做」，放最前面
    card_ctx = skills.context_block(user, text, limit=2)
    if card_ctx:
        parts.append(card_ctx)

    # 任务感知：问题里提到任务名时，直接把文件里的实时进度塞进上下文（比 RAG 准）
    slug = tasks.find_task(user, text)
    if slug:
        brief = tasks.task_brief(user, slug)
        if brief:
            parts.append("任务当前状态（从文件实时读取，最权威）：\n" + brief)

    # 全库目录导航：先给一层「有哪些页」的全局视野，再让 RAG 精确召回（不是取代 RAG）
    try:
        import index as idx
        toc = idx.read_index(user)
        if toc:
            parts.append("全库内容目录（先看这里有没有相关条目）：\n" + toc[:1500])
    except Exception:
        pass

    hits = rag.search(user, text, limit=5)
    if hits:
        ctx = "\n".join("【%s】%s" % (h["path"], h["text"]) for h in hits)
    else:
        hits = storage.search(user, text, limit=5)
        ctx = "\n".join("【%s】%s" % (h["path"], h["snippet"]) for h in hits) if hits else ""
    if ctx:
        parts.append("笔记片段：\n" + ctx[:4000])
    if not parts:
        return {**state, "reply": "在自己的库里没找到相关内容。库里还没沉淀技能卡的话，"
                                  "做完一件事发「沉淀 技能名」就会有一张，之后问「怎么做」会自动带上它。"}

    prompt = ("基于下面的资料回答用户问题。资料里有技能卡时，优先按卡里的步骤回答，"
              "并在开头注明「按《卡名》」；没有依据就直说没找到，不要编造。\n\n%s\n\n问题：%s" % (
                  "\n\n".join(parts), text))
    ans = llm.chat(prompt, tier="main", max_tokens=900)
    return {**state, "reply": ans}


def node_ask(state: S) -> S:
    return {**state, "reply": "这句我不确定是记笔记还是要开项目：\n1️⃣ 回复「开始 项目名」开启项目记录\n2️⃣ 直接再说一遍，我就当普通笔记存起来"}


def node_fileop(state: S) -> S:
    """自然语言文件操作。模型判 none → 完整回落到原来的分类/步骤/问答链路。"""
    kind, msg = fileops.run(state["user"], state["text"])
    if kind == "none":
        rest = _classify_rest(state)
        it = rest["intent"]
        if it == "step":
            return node_step(rest)
        if it == "qa":
            return node_qa(rest)
        return node_note(rest)
    if kind == "err":
        # 判定是操作但被拒：原话仍按笔记存下，保证用户内容零丢失
        try:
            cat, path, _ = _write_note(state["user"], state["text"])
            msg += "\n（原话已按普通笔记存到「%s」：%s）" % (cat, path.name)
        except Exception:
            pass
        return {**state, "reply": msg}
    return {**state, "reply": msg}


def build_graph():
    g = StateGraph(S)
    g.add_node("classify", node_classify)
    g.add_node("cmd", node_command)
    g.add_node("step", node_step)
    g.add_node("note", node_note)
    g.add_node("qa", node_qa)
    g.add_node("ask", node_ask)
    g.add_node("fileop", node_fileop)
    g.set_entry_point("classify")
    g.add_conditional_edges("classify", lambda s: s["intent"], {
        "cmd": "cmd", "step": "step", "note": "note", "qa": "qa", "ask": "ask",
        "fileop": "fileop",
    })
    for n in ("cmd", "step", "note", "qa", "ask", "fileop"):
        g.add_edge(n, END)
    return g.compile()
