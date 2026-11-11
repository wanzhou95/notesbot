"""定期体检（Lint）：扫正式页与双链，找出待审积压 / 断链 / 孤儿页 / 缺来源 / 无别名。

只写 `_index/体检报告.md`，**绝不改任何页面**。默认走规则（零模型、零费用），
矛盾检测等库再大一点、有明确需求时再单独上（需要语义召回找同主题对 + 模型判断）。
"""
import re
from datetime import datetime

import tasks
from config import user_dir

SCAN_DIRS = ("05-Skills", "06-Wiki", "02-Areas")
DRAFT_DIR = "_待审"
REPORT_REL = "_index/体检报告.md"
SKIP_PARTS = {"_index", "_assets", ".trash", ".stversions", ".obsidian", ".git"}

_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?(?:\#[^\]]+)?\]\]")


def _formal_pages(user):
    """正式页列表：[(rel, stem, title, aliases, has_source, body)]。"""
    root = user_dir(user)
    out = []
    for folder in SCAN_DIRS:
        d = root / folder
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            parts = p.relative_to(root).parts
            if any(x.startswith("_") or x.startswith(".") for x in parts[:-1]):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            raw, body = tasks.split_fm(text)
            fm = tasks.parse_fm(raw) if raw else {}
            title = str(fm.get("title") or "").strip() or p.stem
            aliases = fm.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            has_source = bool(fm.get("source") or fm.get("sources"))
            rel = str(p.relative_to(root)).replace("\\", "/")
            out.append((rel, p.stem, title, [str(a).strip() for a in aliases if str(a).strip()],
                        has_source, body))
    return out


def _linkable_targets(user):
    """全库可链接目标：所有 md 的 stem + frontmatter 的 title + aliases。"""
    root = user_dir(user)
    targets = set()
    for p in root.rglob("*.md"):
        parts = p.relative_to(root).parts
        if any(x in SKIP_PARTS or x.startswith(".") for x in parts):
            continue
        targets.add(p.stem)
        try:
            raw, _ = tasks.split_fm(p.read_text(encoding="utf-8"))
            if raw:
                fm = tasks.parse_fm(raw)
                t = fm.get("title")
                if t:
                    targets.add(str(t).strip())
                for a in (fm.get("aliases") or []):
                    if isinstance(a, str) and a.strip():
                        targets.add(a.strip())
        except Exception:
            pass
    return targets


def _inbound_links(user):
    """全库其它文件里出现的 [[目标]] 集合（用于孤儿页判断）。"""
    root = user_dir(user)
    links = set()
    for p in root.rglob("*.md"):
        parts = p.relative_to(root).parts
        if any(x in SKIP_PARTS or x.startswith(".") for x in parts):
            continue
        try:
            body = tasks.split_fm(p.read_text(encoding="utf-8"))[1]
        except Exception:
            continue
        for m in _LINK_RE.finditer(body or ""):
            links.add(m.group(1).strip())
    return links


def _drafts(user):
    """待审草稿列表 [(folder, name)]。"""
    root = user_dir(user)
    out = []
    for folder in SCAN_DIRS:
        d = root / folder / DRAFT_DIR
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            out.append((folder, p.stem))
    return out


def run_lint(user):
    """扫描并写报告。返回报告摘要统计。"""
    pages = _formal_pages(user)
    targets = _linkable_targets(user)
    inbound = _inbound_links(user)
    drafts = _drafts(user)

    broken = []      # (rel, 目标)
    orphans = []     # (rel, title)
    no_source = []   # rel
    no_alias = []    # rel
    for rel, stem, title, aliases, has_source, body in pages:
        if not has_source:
            no_source.append(rel)
        if rel.startswith(("05-Skills/", "06-Wiki/")) and not aliases:
            no_alias.append(rel)
        if not inbound.intersection({stem, title} | set(aliases)):
            orphans.append((rel, title))
        seen = set()
        for m in _LINK_RE.finditer(body or ""):
            tgt = m.group(1).strip()
            if not tgt or tgt in targets or tgt in seen:
                continue
            seen.add(tgt)
            broken.append((rel, tgt))

    # 组装报告
    L = ["# 体检报告 %s" % datetime.now().strftime("%Y-%m-%d %H:%M"), "",
         "> 每周日自动跑，**只出报告、不改任何页面**。想手动跑：`python manage.py lint`。", ""]
    L += ["## 待审积压（%d 张）" % len(drafts), ""]
    L += (["- %s/%s/%s.md" % (f, DRAFT_DIR, n) for f, n in drafts] + [""]) if drafts else ["无。", ""]
    L += ["## 断链（%d 处，正文引用了不存在的页）" % len(broken), ""]
    L += (["- %s → [[%s]]" % (r, t) for r, t in broken] + [""]) if broken else ["无。", ""]
    L += ["## 孤儿页（%d 个，暂无任何页链接到它，可能只是还没被引用）" % len(orphans), ""]
    L += (["- %s（%s）" % (r, t) for r, t in orphans] + [""]) if orphans else ["无。", ""]
    L += ["## 缺来源（%d 个，frontmatter 无 source/sources）" % len(no_source), ""]
    L += (["- %s" % r for r in no_source] + [""]) if no_source else ["无。", ""]
    L += ["## 无别名（%d 个，影响检索命中，建议补）" % len(no_alias), ""]
    L += (["- %s" % r for r in no_alias] + [""]) if no_alias else ["无。", ""]

    p = user_dir(user) / REPORT_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L), encoding="utf-8")
    return {"drafts": len(drafts), "broken": len(broken), "orphans": len(orphans),
            "no_source": len(no_source), "no_alias": len(no_alias)}


def run_all():
    """对所有用户跑一遍（timer 入口）。"""
    import config
    out = {}
    for name in config.load_users():
        try:
            out[name] = run_lint(name)
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run_all(), ensure_ascii=False, indent=2, default=str))
