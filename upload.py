"""附件上传后的处理：提取文件文本、整理归档、图片 OCR 归档。"""
import base64
import io
import re

import storage
from llm import LLMRouter

llm = LLMRouter()

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "py", "log", "yaml", "yml", "html"}
MAX_BYTES = 8 * 1024 * 1024  # 8MB


def decode_upload(data_b64):
    """base64（可带 data:xxx;base64, 前缀）→ bytes，带大小限制。"""
    if "," in data_b64:
        data_b64 = data_b64.split(",", 1)[1]
    raw = base64.b64decode(data_b64)
    if len(raw) > MAX_BYTES:
        raise ValueError("文件超过 8MB 限制")
    return raw


def ext_of(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_image(name):
    return ext_of(name) in IMAGE_EXTS


def extract_text(name, data: bytes):
    """从上传文件提取纯文本（txt/md/csv 等直接读，docx 解析段落）。"""
    ext = ext_of(name)
    if ext in TEXT_EXTS:
        return data.decode("utf-8", "replace").strip()
    if ext == "docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paras)
    raise ValueError("暂不支持 .%s 文件，请上传 txt / md / docx / csv 等文本类文件" % ext)


def organize(user, name, data: bytes, source="upload"):
    """文件 → 提取文本 → AI 归类 → 存为笔记。返回 (分类, 笔记相对路径)。"""
    text = extract_text(name, data)
    if not text:
        raise ValueError("文件里没有可提取的文字")
    return categorize_and_write(user, text, source=source)


def ocr_image(user, data: bytes):
    """图片 → GLM 视觉识别文字。返回识别文本。"""
    b64 = base64.b64encode(data).decode()
    data_url = "data:image/jpeg;base64," + b64
    prompt = ("请把这张图片里的文字完整、准确地识别出来，按原排版输出为纯文本；"
              "没有文字就输出「（无文字）」；不要补充解释。")
    return llm.vision(data_url, prompt)


def categorize_and_write(user, text, source="bot"):
    """读契约的「归类规则」节让 AI 归类，写笔记并回传 (分类, 相对路径)。"""
    rules = storage.read_contract_section(user, "归类规则")
    cat = "00-灵感库"
    if rules:
        prompt = (
            "根据下面的归类规则，判断这条内容应该归到哪个分类。\n"
            "只输出分类名（精确匹配目录名），不要解释。\n\n"
            "归类规则：\n%s\n\n内容：%s" % (rules, text[:800])
        )
        out = llm.chat(prompt, tier="router", max_tokens=30)
        cat = out.strip()
        if cat not in storage.CATEGORIES:
            cat = next((c for c in storage.CATEGORIES if c in out), "00-灵感库")
    # 01-Projects 是任务体系的专用目录（<日期-任务名>/<任务名>.md），
    # 普通笔记直接写进根目录会变成任务页看不到的「散笔记」。
    # 想建任务请用「开始 X」/「想做 X」，这里把误判兜底到灵感库。
    if cat == "01-Projects":
        cat = "00-灵感库"
    path = storage.write_note(user, text, category=cat, source=source)
    rel = str(path.relative_to(storage.user_dir(user))).replace("\\", "/")
    return cat, rel
