"""攻略导出：Markdown → Word (python-docx) / PDF (weasyprint)。"""
import re
from pathlib import Path

from config import DATA_DIR, user_dir

EXPORT_DIR = DATA_DIR / "exports"

FONT = "Noto Sans CJK SC"


def guide_path(user, slug):
    return user_dir(user) / "01-Projects" / slug / "行动攻略.md"


def read_guide(user, slug):
    """读取攻略内容，去掉 frontmatter。"""
    p = guide_path(user, slug)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\n", text, re.S)
        if m:
            text = text[m.end():]
    return text.strip()


def export_dir(user):
    d = EXPORT_DIR / user
    d.mkdir(parents=True, exist_ok=True)
    return d


def export(user, slug, fmt):
    """导出攻略，返回生成的文件路径。fmt: docx / pdf"""
    from storage import strip_md_fence
    content = read_guide(user, slug)
    if content is None:
        raise FileNotFoundError("该项目还没有行动攻略")
    content = strip_md_fence(content)
    out = export_dir(user) / ("%s.%s" % (slug, fmt))
    if fmt == "docx":
        _to_docx(content, out)
    elif fmt == "pdf":
        _to_pdf(content, out)
    else:
        raise ValueError("不支持的格式: %s" % fmt)
    return out


# ---------------- Word ----------------

def _docx_font(run):
    from docx.oxml.ns import qn
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


def _add_runs(par, text):
    """处理 **加粗** 和 `代码` 行内标记。"""
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`", text):
        if m.start() > pos:
            _docx_font(par.add_run(text[pos:m.start()]))
        if m.group(1) is not None:  # 加粗
            r = par.add_run(m.group(1))
            _docx_font(r)
            r.bold = True
        else:  # 行内代码
            r = par.add_run(m.group(2))
            _docx_font(r)
            r.font.name = "Consolas"
        pos = m.end()
    if pos < len(text):
        _docx_font(par.add_run(text[pos:]))


def _to_docx(content, out: Path):
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    for line in content.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if re.match(r"^#{1,6}\s", line):
            level = min(len(line) - len(line.lstrip("#")) - 1, 3)
            par = doc.add_heading("", level=level)
            _add_runs(par, line.lstrip("#").strip())
        elif re.match(r"^\s*[-*]\s+\[[ xX]\]\s", line):
            done = "x" in line.lower().split("[")[1].split("]")[0]
            par = doc.add_paragraph()
            r = par.add_run("☑ " if done else "☐ ")
            _docx_font(r)
            _add_runs(par, re.sub(r"^\s*[-*]\s+\[[ xX]\]\s*", "", line))
        elif re.match(r"^\s*[-*]\s+", line):
            par = doc.add_paragraph(style="List Bullet")
            _add_runs(par, re.sub(r"^\s*[-*]\s+", "", line))
        elif re.match(r"^\s*\d+[.、]\s+", line):
            par = doc.add_paragraph(style="List Number")
            _add_runs(par, re.sub(r"^\s*\d+[.、]\s+", "", line))
        elif line.strip().startswith(("---", "***", "___")) and not line.strip().strip("-*_"):
            continue  # 分隔线
        else:
            par = doc.add_paragraph()
            _add_runs(par, line.strip())
    # 表格、代码块等复杂元素由 PDF 侧完整支持；docx 保留为普通段落
    doc.save(str(out))


# ---------------- PDF ----------------

def _to_pdf(content, out: Path):
    import markdown as md_lib
    from weasyprint import HTML
    body = md_lib.markdown(content, extensions=["tables", "fenced_code", "sane_lists"])
    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><style>
@page { size: A4; margin: 2cm 1.8cm; }
body { font-family: 'Noto Sans CJK SC', sans-serif; font-size: 11pt; line-height: 1.7; color: #1f2937; }
h1 { font-size: 20pt; border-bottom: 2px solid #4f46e5; padding-bottom: 6px; }
h2 { font-size: 15pt; color: #4f46e5; margin-top: 18px; }
h3 { font-size: 12.5pt; }
code { font-family: monospace; background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
pre { background: #f3f4f6; padding: 10px; border-radius: 6px; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%%; margin: 10px 0; }
th, td { border: 1px solid #d1d5db; padding: 5px 8px; font-size: 10pt; }
th { background: #eef2ff; }
blockquote { border-left: 3px solid #c7d2fe; margin-left: 0; padding-left: 10px; color: #6b7280; }
ul { padding-left: 1.4em; }
</style></head><body>%s</body></html>""" % body
    HTML(string=html).write_pdf(str(out))
