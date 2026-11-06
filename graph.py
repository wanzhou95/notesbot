import json
import re
from typing import TypedDict

from langgraph.graph import StateGraph, END

import storage
import tasks
import rag
import skills
import video
from llm import LLMRouter

llm = LLMRouter()

CMD_PATTERNS = [
    ("start", re.compile(r"^\s*(开始|新建项目|开一个|开工)\s*(.+)$")),
    ("switch", re.compile(r"^\s*(切到|切换到|切换)\s*(.+)$")),
    ("todo", re.compile(r"^\s*(想做|待办|加个待办|加入待办|计划做)\s*[:：]?\s*(.+)$")),
    ("stepdone", re.compile(r"^\s*(完成|搞定|做完|勾掉)\s*第?\s*(\d+)\s*[步项]?\s*$")),
    ("taskdone", re.compile(r"^\s*(任务完成|标记完成)\s*(.*)$")),
    ("linkadd", re.compile(r"^\s*(关联|相关联?于)\s*[:：]?\s*(.+)$")),
    ("setparent", re.compile(r"^\s*(上级|父任务)\s*[:：]?\s*(.+)$")),
    ("skillmake", re.compile(r"^\s*(沉淀|存为技能|做成技能|整理成技能)\s*[:：]?\s*(.*)$")),
    ("skilllist", re.compile(r"^\s*(技能列表|有哪些技能|技能清单|技能卡)\s*$")),
    # 视频字幕：带动词的链接、裸的视频站链接、以及列表/查看
    ("videolist", re.compile(r"^\s*(视频列表|字幕列表|有哪些视频|视频笔记)\s*$")),
    ("videomake", re.compile(r"^\s*(?:总结|转写|提取字幕|看下|看看)\s*(?P<url>https?://\S+)\s*$")),
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
        return {**state, "intent": "cmd", "cmd": "videomake", "arg": share}
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
        return {**state, "intent": "cmd", "cmd": cmd, "arg": arg}

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
        name, parent = _split_parent(arg)
        slug, new = storage.create_project(user, name)
        if not slug:
            return {**state, "reply": "任务名不能为空，比如「开始 剪辑视频学习」。"}
        if parent:
            tasks.set_parent(user, slug, parent)
        if not new:
            return {**state, "reply": "任务《%s》已存在，已切换过去。" % name}
        if parent:
            return {**state, "reply": "已创建任务《%s》（挂在《%s》下），开始记录，说完发「结束」。" % (name, parent)}
        return {**state, "reply": "已创建任务《%s》，开始记录，说完发「结束」生成行动攻略。" % name}

    if cmd == "todo":
        name, parent = _split_parent(arg)
        slug, new = tasks.create_task(user, name, status="todo", parent=parent or None)
        if not slug:
            return {**state, "reply": "任务名不能为空，比如「想做 剪辑视频学习」。"}
        if not new:
            return {**state, "reply": "《%s》已经在任务列表里了。" % name}
        msg = "已加入待办：《%s》" % name
        if parent:
            msg += "（上级：%s）" % parent
        msg += "。开始做的时候说「开始 %s」。" % name
        return {**state, "reply": msg}

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
        if tasks.set_parent(user, slug, arg.strip()):
            return {**state, "reply": "已把《%s》的上级设为《%s》，总览里会缩进显示。" % (
                (tasks.read_task(user, slug) or {}).get("name", slug), arg.strip())}
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
        return {**state, "reply": "\n".join(lines)}

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
        if not video.ffmpeg_exe():
            return {**state, "reply": "服务器没装 ffmpeg，转写跑不起来（装一下 imageio-ffmpeg 就好）。"}
        if not video.is_url(url):
            return {**state, "reply": "给我一个完整的视频链接（http/https 开头）。"}
        old = video.find_by_source(user, url)
        if old:
            return {**state, "reply": "这个链接已经总结过了：《%s》（%s）。\n发「看视频 %s」看要点和章节。"
                                      % (old["title"], old["duration"], old["title"])}
        video.start_job(user, url=url)
        return {**state, "reply": (
            "已开始处理这个视频。\n"
            "先看平台有没有现成字幕（有就 0 成本）；没有就下音频、切段转写（约 0.015 元/分钟）。\n"
            "跑完存到 %s/，**不进检索**，不会冲淡搜索。\n"
            "之后发「视频列表」看结果，或开网页「视频」页看章节和完整字幕。" % video.VIDEO_DIR)}

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
    cat, path, card = _write_note(state["user"], state["text"])
    reply = "已存到「%s」：%s（说「开始 X」可开启项目记录）" % (cat, path.name)
    if card:
        reply += "\n这条内容命中技能卡《%s》，已挂双链（Obsidian 里会连线）。" \
                 "想按卡里的步骤做，发「用 %s」。" % (card["title"], card["title"])
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
