"""全局内容目录 index.md 与时间线 log.md。

给 LLM 一个「先读目录、再下钻」的全局视野（Karpathy LLM Wiki 的 index/log）。
两个文件都落在 `_index/` 下，`rag.SKIP_DIR_NAMES` 已排除该目录，不会进向量索引。

- index.md：全库内容目录，每页一行 `[[链接|标题]] — 摘要 ｜ 类别 ｜ 日期 ｜ 来源数`
- log.md  ：append-only 时间线，统一前缀 `## [时间] 动作 | 详情`，可用 grep 解析
"""
import re
from datetime import datetime

import tasks
from config import user_dir

INDEX_REL = "_index/index.md"
LOG_REL = "_index/log.md"
SCAN_DIRS = ("05-Skills", "06-Wiki", "02-Areas")
DRAFT_DIR = "_待审"


def _strip_md(text):
    """去 frontmatter 与 markdown 符号，压成可读单行。"""
    body = text
    if text.startswith("---"):
        _, body = tasks.split_fm(text)
    body = re.sub(r"(?m)^#+\s*", "", body or "")
    body = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", body)
    body = re.sub(r"[*_>`#\[\]()|]", "", body)
    body = re.sub(r"\s+", " ", body)
    return body.strip()


def _summarize(text, n=60):
    s = _strip_md(text)
    return s[:n] + ("…" if len(s) > n else "")


def _count_sources(fm):
    s = fm.get("sources")
    if isinstance(s, list):
        return len(s)
    if isinstance(s, str) and s.strip():
        return len([x for x in re.split(r"[,\s]+", s) if x])
    return 1 if fm.get("source") else 0


def _scan_pages(user):
    """扫描正式页，返回 [(folder, title, summary, date, sources, stem)]。"""
    root = user_dir(user)
    rows = []
    for folder in SCAN_DIRS:
        d = root / folder
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            rel_parts = p.relative_to(root).parts
            # 排除 _待审/ 等草稿目录与隐藏目录；只保留正式页
            if any(part.startswith("_") or part.startswith(".") for part in rel_parts[:-1]):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            raw, body = tasks.split_fm(text)
            fm = tasks.parse_fm(raw) if raw else {}
            title = (fm.get("title") or "").strip() or p.stem
            date = (fm.get("date") or fm.get("updated") or "").strip()
            rows.append((folder, title, _summarize(body), date, _count_sources(fm), p.stem))
    return rows


def rebuild_index(user):
    """重建 _index/index.md，返回条目数。纯生成，不碰任何页面。"""
    rows = _scan_pages(user)
    lines = ["# 全库内容目录", "",
             "> 由系统自动汇总。AI 问答前先读这里定位，再下钻到具体页面。", ""]
    for folder in SCAN_DIRS:
        items = [r for r in rows if r[0] == folder]
        if not items:
            continue
        lines.append("## %s" % folder)
        lines.append("")
        for _, title, summary, date, sources, stem in items:
            meta = " ｜ ".join(
                x for x in (date, ("%d 来源" % sources) if sources else "") if x)
            tail = (" ｜ " + meta) if meta else ""
            lines.append("- [[%s|%s]] — %s%s" % (stem, title, summary, tail))
        lines.append("")
    p = user_dir(user) / INDEX_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def read_index(user):
    """读全库目录（不存在返回空串）。给问答做导航层，不替代 RAG。"""
    p = user_dir(user) / INDEX_REL
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""


def log_event(user, action, detail):
    """append-only 写时间线。任何异常静默（日志不该拖垮主流程）。"""
    try:
        p = user_dir(user) / LOG_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = "## [%s] %s | %s\n" % (stamp, action, (detail or "").replace("\n", " "))
        if not p.exists():
            p.write_text("# 时间线\n\n> 全库大事记（append-only）。"
                         "可用 `grep \"^## \\[\" log.md | tail -5` 解析。\n\n",
                         encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
