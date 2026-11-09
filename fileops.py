"""自然语言文件操作：意图识别 → 结构化动作 → 白名单校验 → 执行 → 留痕。

安全设计（用户拍板「全部直接执行、不做二次确认」，所以这里必须做硬）：

1. **规则预筛零成本**：`looks_like_op` 只是粗召回，只有明显含「建目录/移动/改名」的话
   才会调模型；其余一律不碰，保持原来的「写笔记 AI 自动归类」行为与费用不变。
2. **模型只输出 JSON，不碰文件**：调用明确指定 `glm-4.7-flash`（不受设置页 DeepSeek 开关
   影响），输出 action ∈ {mkdir, move, rename, none}。解析失败一律当 none。
3. **路径白名单**：所有目标相对用户库根；禁止 `..` / 绝对路径 / 盘符 / 隐藏目录 /
   系统目录（`_index`、`_assets`、`.obsidian` …）/ 任务区 `01-Projects`；再叠加
   `Path.resolve()` 与 `startswith(root)` 双保险。
4. **绝不覆盖**：目标已存在就拒绝；move/rename 的源必须唯一命中，多个或零个都拒绝并列出候选。
5. **操作日志**：每次写操作追加到 `_index/文件操作日志.md`（`_index` 不进 RAG），
   因为没有二次确认，这份日志是唯一可追溯 / 可手动回滚的凭据。
6. **RAG 同步**：移动 / 改名后删除旧路径 chunk 并重建新路径，避免搜索指向已不存在的文件。

返回约定：`run(user, text)` → `(kind, msg)`
- `("none", "")`：模型判定不是文件操作 → 调用方回落到原有分类 / 步骤 / 问答链路
- `("ok", 文本)`：执行成功
- `("err", 文本)`：判定是操作但被拒 / 失败 → 调用方把原话按笔记存下，保证零丢失
"""
import json
import re
from datetime import datetime
from pathlib import Path

from config import user_dir, CATEGORIES

# ---- 预筛：只做粗召回，命中才调模型；模型判 none 会完整回落，所以宁可宽一点 ----
# 拆成「动词 + 名词」两段分别找，中间夹目录名（「建个 AI学习 目录」）也能命中
_V_MK = re.compile(r"建|创建|新建|新增|加个|开个|加|开")
_N_DIR = re.compile(r"目录|文件夹")
_V_MV = re.compile(r"移动|移到|挪到|搬到|搬去|放进|放到|归档到|归档至|转存到")
_N_FILE = re.compile(r"笔记|文件|这条|这篇|刚才|目录|文件夹|素材|内容")
_V_RN = re.compile(r"改名|重命名|改叫|改名为|改成|命名为")
# 路径感：数字分类 / 英文目录名 / 中文分类名，避免「新建目录的方法」这类纯陈述句大量召回
_PATHY = re.compile(r"\d{2}[-－\s]|Resources|Areas|Archive|Skills|Wiki|灵感库|归档|分类|"
                    r"目录|文件夹|笔记|这条|这篇|刚才|下面|里")


def looks_like_op(text):
    """粗召回：像文件操作才返回 True。真正的判定交给模型。"""
    t = (text or "").strip()
    if not t or len(t) > 200:
        return False
    # 明确的命令 / 问句不抢：这些走原有链路
    if t.startswith(("记", "开始", "切到", "完成", "结束", "待办", "想做", "总结 http")):
        return False
    # 改名自带动作特征，不要求额外的路径词（「把 图文信息 改名成 图文资料」没有目录字样）
    if _V_RN.search(t):
        return True
    if not _PATHY.search(t):
        return False
    if _V_MK.search(t) and _N_DIR.search(t):
        return True
    # 移动动词本身够强：目标目录名（02-Areas / 03…）就算路径线索，
    # 不能要求句中一定出现「笔记」二字，否则「把 心流 移到 02-Areas」会被漏掉。
    return bool(_V_MV.search(t) and (_N_FILE.search(t) or _PATHY.search(t)))


# ---- 允许写入的顶层分类（01-Projects 是任务体系专用，禁止自然语言乱动）----
ALLOWED_TOP = set(CATEGORIES) - {"01-Projects"}
FORBIDDEN_PARTS = {"_index", "_assets", ".obsidian", ".git", ".stversions", ".trash",
                   ".sync", "_待审", "node_modules"}
# 目录下允许操作的顶层白名单提示，给模型当参考
TOP_HINT = "\n".join("- %s" % c for c in CATEGORIES if c in ALLOWED_TOP)

PROMPT = """你是一个知识库文件操作助手。判断用户这句话是不是在要求对笔记库做「建目录 / 移动笔记 / 改名」操作。

库是 Obsidian 风格的 Markdown 文件夹，顶层只有这些分类目录（可写）：
%s
其中 `01-Projects` 是任务体系专用目录，禁止在其中创建/移动/改名。用户说「03」指 `03-Resources`，「02」指 `02-Areas`，以此类推。

只输出一个 JSON 对象，不要解释、不要 Markdown 代码块：
- 不是文件操作（只是普通笔记内容、提问、闲聊）：{"action":"none"}
- 建目录：{"action":"mkdir","path":"03-Resources/AI学习"}
- 移动笔记：{"action":"move","src":"要移动的笔记关键词","dst_dir":"02-Areas"}
- 改名（笔记或目录）：{"action":"rename","src":"原名关键词","new_name":"新名字"}

严格要求：
- path / dst_dir 必须是相对库根的路径，以某个分类目录开头，用 / 分隔，不要绝对路径、不要 ..
- src 用用户提到的名字关键词即可，不要编造完整路径
- new_name 只是一个名字，不要带 / 或后缀
- 拿不准就输出 {"action":"none"}

用户原话：%s"""


def _client():
    """惰性创建，避免导入本模块就强依赖 httpx。"""
    from llm import LLMRouter
    return LLMRouter()


def _ask(text):
    """明确用 glm-4.7-flash：放进 models 首位，后面的降级链由 LLMRouter 兜底。"""
    prompt = PROMPT % (TOP_HINT, text[:400])
    raw = _client().chat_messages(
        [{"role": "user", "content": prompt}],
        tier="router", max_tokens=300, models=["glm-4.7-flash"])
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


# ---- 路径安全 ----

def _clean_rel(rel):
    """把模型给的相对路径规范化；不安全返回 None。"""
    if not rel or not isinstance(rel, str):
        return None
    s = rel.strip().replace("\\", "/").strip("/")
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        return None
    parts = s.split("/")
    for p in parts:
        p = p.strip()
        if not p or p in (".", "..") or p.startswith(".") or p in FORBIDDEN_PARTS:
            return None
        if re.search(r"[\x00-\x1f/\\:*?\"<>|]", p):
            return None
    if parts[0] not in ALLOWED_TOP:
        return None
    return "/".join(parts)


def _abs(user, rel):
    """路径解析 + 越界双保险。返回 Path 或 None。"""
    root = user_dir(user).resolve()
    p = (root / rel).resolve()
    if p == root or not str(p).startswith(str(root) + "/"):
        return None
    return p


# 「刚才那条 / 最近写的 / 这条」这类指代词：模型没法知道文件名，交给后端按 mtime 取最新
_VAGUE_SRC = re.compile(r"刚才|最近|上一条|刚写|刚记|这条|这篇|那条|那篇|当前|最新")


def _all_candidates(user):
    root = user_dir(user)
    out = []
    for top in ALLOWED_TOP:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if any(x in FORBIDDEN_PARTS or x.startswith(".") for x in p.relative_to(root).parts):
                continue
            if p.is_file() and p.suffix.lower() != ".md":
                continue
            out.append(p)
    return out


def _find_src(user, hint):
    """用关键词在可写分类里找源（笔记 .md 或目录）；要求唯一命中。"""
    root = user_dir(user)
    key = re.sub(r"[\s\-_《》「」\[\]]+", "", (hint or "")).lower()
    if not key:
        return [], "没给出要操作的对象"
    pool = _all_candidates(user)
    if _VAGUE_SRC.search(hint or ""):
        notes = sorted([p for p in pool if p.is_file()],
                       key=lambda x: x.stat().st_mtime, reverse=True)
        return (notes[:1], "") if notes else ([], "库里还没有可移动的笔记")
    hits = [p for p in pool
            if (key in re.sub(r"[\s\-_《》「」\[\]]+", "",
                              p.stem if p.is_file() else p.name).lower()
                or re.sub(r"[\s\-_《》「」\[\]]+", "",
                          p.stem if p.is_file() else p.name).lower() in key)]
    return hits, ""


def _log(user, action, detail):
    """操作日志：没有二次确认，这份记录是唯一回滚依据。"""
    try:
        p = user_dir(user) / "_index" / "文件操作日志.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        line = "- %s ｜ %s ｜ %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                       action, detail.replace("\n", " "))
        if not p.exists():
            p.write_text("# 文件操作日志\n\n> AI 执行的文件操作留痕。删除/移动前先查这里。\n\n",
                         encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _reindex(user, old_rel=None, new_rel=None):
    """移动/改名后同步 RAG：删旧路径 chunk，重建新路径。失败不影响文件操作本身。"""
    try:
        import rag
        if old_rel:
            rag.index_file(user, old_rel)   # 源文件已不在 → 内部会 delete_file
        if new_rel:
            rag.index_file(user, new_rel)
    except Exception:
        pass


def _do_mkdir(user, d, text):
    rel = _clean_rel(d.get("path"))
    if not rel:
        return "err", "没建：路径不安全或不在允许的分类目录里（只能是 %s）。" % "、".join(sorted(ALLOWED_TOP))
    p = _abs(user, rel)
    if p is None:
        return "err", "没建：路径超出你的知识库范围。"
    if p.exists():
        return "err", "没建：`%s` 已经存在了（不会覆盖）。" % rel
    p.mkdir(parents=True, exist_ok=False)
    _log(user, "mkdir", "%s ← 原话：%s" % (rel, text))
    return "ok", "已建目录：`%s`\nObsidian 里刷新就能看到。空目录不会进搜索，放一篇笔记或视频速览进去后，网页才会显示它。" % rel


def _do_move(user, d, text):
    hits, err = _find_src(user, d.get("src"))
    if err:
        return "err", "没移动：" + err
    dst_dir_rel = _clean_rel(d.get("dst_dir"))
    if not dst_dir_rel:
        return "err", "没移动：目标目录不安全或不在允许的分类目录里。"
    dst_dir = _abs(user, dst_dir_rel)
    if dst_dir is None:
        return "err", "没移动：目标目录超出你的知识库范围。"
    if len(hits) != 1:
        tip = "、".join("`%s`" % str(x.relative_to(user_dir(user))).replace("\\", "/")
                       for x in hits[:5]) or "无"
        # 典型同名冲突：恰好两个同名文件、其中一个已在目标目录。
        # 只用来给更准确的提示，依旧拒绝执行 —— 绝不因为「好像能唯一」就自动挑一个移动。
        if len(hits) == 2 and hits[0].name == hits[1].name:
            in_dst = [h for h in hits if h.parent == dst_dir]
            if in_dst:
                return "err", "没移动：目标目录已有同名文件 `%s/%s`，不会覆盖。" \
                              "请先给源文件改名，或把名字说得更具体。" % (dst_dir_rel, in_dst[0].name)
        return "err", "没移动：匹配到 %d 个对象，请说得更具体一点。候选：%s" % (len(hits), tip)
    src = hits[0]
    if not dst_dir.is_dir():
        return "err", "没移动：目标目录 `%s` 不存在，先建目录再移。" % dst_dir_rel
    dst = dst_dir / src.name
    if dst.exists():
        return "err", "没移动：目标位置已有同名文件（`%s`），不会覆盖。" % (
            str(dst.relative_to(user_dir(user))).replace("\\", "/"))
    if src.suffix.lower() != ".md":
        return "err", "没移动：目前只支持移动 Markdown 笔记（`.md`）。"
    old_rel = str(src.relative_to(user_dir(user))).replace("\\", "/")
    src.rename(dst)
    new_rel = str(dst.relative_to(user_dir(user))).replace("\\", "/")
    _log(user, "move", "%s → %s ← 原话：%s" % (old_rel, new_rel, text))
    _reindex(user, old_rel, new_rel)
    return "ok", "已移动：`%s` → `%s`" % (old_rel, new_rel)


def _do_rename(user, d, text):
    hits, err = _find_src(user, d.get("src"))
    if err:
        return "err", "没改名：" + err
    if len(hits) != 1:
        tip = "、".join("`%s`" % str(x.relative_to(user_dir(user))).replace("\\", "/")
                       for x in hits[:5]) or "无"
        return "err", "没改名：匹配到 %d 个对象，请说得更具体一点。候选：%s" % (len(hits), tip)
    src = hits[0]
    # new_name 只是名字，不能带路径；单独校验（_clean_rel 要求顶层是分类，不适用于纯名字）
    raw = str(d.get("new_name") or "").strip().replace("\\", "/")
    raw = re.sub(r"\.md$", "", raw, flags=re.I)   # 用户/模型带了 .md 后缀就去掉，避免 x.md.md
    if not raw or "/" in raw or raw in (".", "..") or raw.startswith(".") or \
            re.search(r"[\x00-\x1f:*?\"<>|]", raw):
        return "err", "没改名：新名字不合法（只能是一个名字，不能带路径或特殊字符）。"
    is_dir = src.is_dir()             # 必须先存下来：rename 之后 src 路径就不存在了
    dst = src.with_name(raw if is_dir else raw + ".md")
    if dst.exists():
        return "err", "没改名：`%s` 已经存在，不会覆盖。" % dst.name
    old_rel = str(src.relative_to(user_dir(user))).replace("\\", "/")
    before = sorted(src.rglob("*.md")) if is_dir else [src]
    src.rename(dst)
    new_rel = str(dst.relative_to(user_dir(user))).replace("\\", "/")
    _log(user, "rename", "%s → %s ← 原话：%s" % (old_rel, new_rel, text))
    if is_dir:
        for old in before:
            rel_old = str(old.relative_to(user_dir(user))).replace("\\", "/")
            rel_new = new_rel + rel_old[len(old_rel):]
            _reindex(user, rel_old, rel_new)
    else:
        _reindex(user, old_rel, new_rel)
    return "ok", "已改名：`%s` → `%s`" % (old_rel, new_rel)


ACTIONS = {"mkdir": _do_mkdir, "move": _do_move, "rename": _do_rename}


def run(user, text):
    """入口。返回 (kind, msg)：none / ok / err。"""
    if not looks_like_op(text):
        return "none", ""
    d = _ask(text)
    if not d:
        return "none", ""            # 模型失败 → 完全回落原链路，不改行为
    action = str(d.get("action") or "none").strip().lower()
    if action == "none" or action not in ACTIONS:
        return "none", ""            # 模型判定不是操作 → 回落
    fn = ACTIONS[action]
    try:
        return fn(user, d, text)
    except Exception as e:
        return "err", "执行失败（没改动任何文件）：%s" % str(e)[:150]
