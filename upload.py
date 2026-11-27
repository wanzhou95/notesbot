"""附件上传后的处理：提取文件文本、整理归档、图片 OCR 归档。"""
import base64
import hashlib
import html as _html
import io
import re

import storage
from llm import LLMRouter

llm = LLMRouter()

LEDGER_NAME = "整理台账.md"

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "py", "log", "yaml", "yml", "html"}
# HTML 原样保存、不提取文字：剥标签剥出来的是「AI创作工作台 创作 画布Hot 视觉设定库…」
# 这种残渣，既不能读也不能用，反倒把原文件带走了。用户要的是能直接复制/转发/下载的源文件，
# 所以 html 走「仅存原文件」这条路（原始字节在上传时就已落 _assets）。
RAW_EXTS = {"html", "htm"}
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


def is_raw(name):
    """HTML/HTM：只存原文件，不抽取文字（见 RAW_EXTS 的说明）。"""
    return ext_of(name) in RAW_EXTS


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


def html_to_text(data: bytes):
    """HTML → (标题, 纯文本)。**已不在上传主链路上用**（html 现在原样保存）。

    保留原因：早期把 html 剥成纯文本存笔记用的，历史笔记是它生成的；
    剥标签会丢掉布局/样式/脚本，交互页只剩一堆零散词（「创作 画布 视觉设定库…」），
    所以 2026-11-27 起 html 上传不再走这里。真要提取文字请单独开接口，别复用上传链路。

    标题优先取 <title>，其次第一个 <h1>；正文剥掉 script/style/注释/head 后
    再去标签得到可见文字。以前 HTML 直接当纯文本存，导致文件名变成
    `!doctype-html`、正文是一整段 HTML 源码，归类也因为只看 CSS 而乱跳。
    """
    raw = data.decode("utf-8", "replace")

    def _strip(tag_text):
        return _html.unescape(re.sub(r"<[^>]+>", "", tag_text)).strip()

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = _strip(m.group(1))
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S | re.I)
        if m:
            title = _strip(m.group(1))

    body = re.sub(r"(?is)<script.*?</script>", "", raw)
    body = re.sub(r"(?is)<style.*?</style>", "", body)
    body = re.sub(r"(?is)<!--.*?-->", "", body)
    body = re.sub(r"(?is)<head.*?</head>", "", body)
    # 常见块级结束标签转成换行，其余标签直接剥掉
    body = re.sub(r"(?i)<(?:br\s*/?|/\s*(?:p|div|li|h[1-6]|tr|blockquote))\s*>", "\n", body)
    body = re.sub(r"<[^>]+>", "", body)
    text = _html.unescape(body)
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln).strip()
    return title, text


def _ledger_path(user):
    return storage.user_dir(user) / "_index" / LEDGER_NAME


def load_ledger(user):
    """读整理台账：{原始文件 sha256: {time, src, note, category}}。

    台账存在的意义：同一份文件（哪怕改了名）重复点「整理成笔记」时，
    直接返回上次的结果，不再新建第二份、也不重新跑一次归类。
    """
    p = _ledger_path(user)
    out = {}
    if not p.exists():
        return out
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln.startswith("|") or set(ln) <= set("|-: "):
                continue
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("内容哈希", "---"):
                continue
            out[cells[0]] = {"time": cells[1], "src": cells[2],
                             "note": cells[3], "category": cells[4]}
    except Exception:
        pass
    return out


def _append_ledger(user, h, src, note, category):
    p = _ledger_path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    # 表格单元格里不能出现竖线，源文件名里偶尔会有
    src = (src or "").replace("|", "/")
    with open(p, "a", encoding="utf-8") as f:
        if new:
            f.write("# 整理台账\n\n> 上传文件整理成笔记的去重记录：同一内容只整理一次。\n\n")
            f.write("| 内容哈希 | 时间 | 源文件 | 笔记 | 分类 |\n|---|---|---|---|---|\n")
        f.write("| %s | %s | %s | %s | %s |\n" % (
            h, storage.now().strftime("%Y-%m-%d %H:%M"), src, note, category))


def organize(user, name, data: bytes, source="upload"):
    """文件 → 提取文本 → AI 归类 → 存为笔记。返回 (分类, 笔记相对路径, 是否重复)。

    去重口径是「原始文件字节」的 sha256：同一份文件重复整理只留一份笔记。
    如果上次那份笔记已经被你删/移走，则允许重新整理。
    """
    h = hashlib.sha256(data).hexdigest()
    old = load_ledger(user).get(h)
    if old and (storage.user_dir(user) / old["note"]).is_file():
        return old["category"], old["note"], True

    ext = ext_of(name)
    if ext in RAW_EXTS:
        # 兜底防线：正常路径在 /api/process 就被拦下了（直接返回「已原样保存」），
        # 走到这里说明有人绕过接口硬调，宁可报错也不产出残渣笔记。
        raise ValueError("HTML 文件按原样保存，不做文字提取")
    text = extract_text(name, data)
    if not text:
        raise ValueError("文件里没有可提取的文字")
    cat, rel = categorize_and_write(user, text, source=source)
    _append_ledger(user, h, name, rel, cat)
    return cat, rel, False


def ocr_image(user, data: bytes):
    """图片 → GLM 视觉识别文字。返回识别文本。"""
    b64 = base64.b64encode(data).decode()
    data_url = "data:image/jpeg;base64," + b64
    prompt = ("请把这张图片里的文字完整、准确地识别出来，按原排版输出为纯文本；"
              "没有文字就输出「（无文字）」；不要补充解释。")
    return llm.vision(data_url, prompt)


def categorize_and_write(user, text, source="bot", title=None):
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
    path = storage.write_note(user, text, title=title, category=cat, source=source)
    rel = str(path.relative_to(storage.user_dir(user))).replace("\\", "/")
    return cat, rel
