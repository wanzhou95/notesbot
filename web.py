import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response

import config
import storage
import rag
import users as users_mod
from config import USERS, ENV, NOTES_ROOT, DATA_DIR
from graph import build_graph, llm

app = FastAPI(title="NotesBot-Web")
GRAPH = build_graph()

SESSIONS = {}  # token -> {"user": ..., "exp": ...}
SESSION_FILE = DATA_DIR / "sessions.json"


def _load_sessions():
    global SESSIONS
    if SESSION_FILE.exists():
        try:
            import json
            SESSIONS = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            now = time.time()
            SESSIONS = {k: v for k, v in SESSIONS.items() if v.get("exp", 0) > now}
        except Exception:
            SESSIONS = {}


def _save_sessions():
    try:
        import json
        SESSION_FILE.write_text(json.dumps(SESSIONS), encoding="utf-8")
    except Exception:
        pass


_load_sessions()

# ---- 鉴权 ----
# 首次启动：确保至少有一个账号（无则按 .env 自动建管理员并打印初始密码到日志）
_BOOT_MSG = users_mod.ensure_bootstrapped()
if _BOOT_MSG:
    print(_BOOT_MSG)


def check_login(request: Request):
    token = request.cookies.get("nb_token", "")
    s = SESSIONS.get(token)
    if not s or s["exp"] < time.time():
        return None
    return s["user"]


def make_session(user):
    token = secrets.token_hex(24)
    SESSIONS[token] = {"user": user, "exp": time.time() + 7 * 86400}
    _save_sessions()
    return token


# ---- 页面（从静态文件读取，避免 Python 字符串转义问题）----

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _html(filename: str):
    p = STATIC_DIR / filename
    if not p.exists():
        return "<h1>页面缺失</h1>"
    r = HTMLResponse(p.read_text(encoding="utf-8"))
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root(request: Request):
    if not check_login(request):
        return _html("login.html")
    return _html("index.html")


@app.post("/api/login")
def login(body: dict):
    config.reload_users()  # 动态感知 adduser 新增的账号（无需重启）
    user = body.get("user", "")
    pw = body.get("pass", "")
    if user not in USERS:
        return JSONResponse({"ok": False, "err": "用户名或密码错误"})
    if users_mod.verify(user, pw):
        token = make_session(user)
        r = JSONResponse({"ok": True, "user": user})
        r.set_cookie("nb_token", token, httponly=True, samesite="lax", max_age=7 * 86400)
        return r
    return JSONResponse({"ok": False, "err": "用户名或密码错误"})


@app.get("/api/me")
def me(request: Request):
    u = check_login(request)
    if not u:
        return JSONResponse({"user": None})
    return {"user": u}


def require(request: Request):
    u = check_login(request)
    if not u:
        raise HTTPException(401, "未登录")
    return u


@app.post("/api/msg")
def msg(request: Request, body: dict):
    u = require(request)
    text = body.get("text", "").strip()
    if not text:
        return {"reply": ""}
    state = {"user": u, "channel": "web", "text": text,
             "intent": "", "cmd": "", "arg": "", "project": "", "reply": ""}
    out = GRAPH.invoke(state)
    return {"reply": out.get("reply", "")}


@app.get("/api/search")
def search(request: Request, q: str = ""):
    u = require(request)
    if not q.strip():
        return {"hits": []}
    # 优先语义检索，失败降级关键词检索
    hits = rag.search(u, q, limit=10)
    if hits:
        items = [{"path": h["path"], "snippet": h["text"][:200]} for h in hits]
    else:
        items = storage.search(u, q, limit=10)
    return {"hits": items}


@app.post("/api/reindex")
def reindex(request: Request):
    u = require(request)
    n = rag.reindex_user(u)
    return {"ok": True, "chunks": n}


MEDIA_TYPES = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "pdf": "application/pdf"}


@app.get("/api/export")
def export_api(request: Request, slug: str = "", fmt: str = "docx"):
    u = require(request)
    if fmt not in MEDIA_TYPES:
        raise HTTPException(400, "格式只支持 docx / pdf")
    st = storage.load_state(u)
    info = st.get("projects", {}).get(slug)
    if not info:
        raise HTTPException(404, "项目不存在")
    if info.get("version", 0) <= 0:
        raise HTTPException(400, "该项目还没有行动攻略")
    import export as export_mod
    try:
        out = export_mod.export(u, slug, fmt)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, "导出失败：%s" % e)
    from urllib.parse import quote
    filename = quote("%s-行动攻略.%s" % (info.get("name", slug), fmt))
    return FileResponse(str(out), media_type=MEDIA_TYPES[fmt],
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % filename})


@app.get("/api/recent")
def recent(request: Request):
    u = require(request)
    root = storage.user_dir(u)
    files = []
    for p in sorted(root.rglob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if ".git" in p.parts or "_index" in p.parts or ".stversions" in p.parts or ".obsidian" in p.parts or ".trash" in p.parts:
            continue
        if p.name in ("_project.md", "欢迎.md"):
            continue
        import datetime
        m = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        body = ""
        try:
            body = " ".join(storage.strip_frontmatter(p.read_text(encoding="utf-8")).split())
        except Exception:
            pass
        files.append({"name": p.name, "mtime": m.strftime("%m-%d %H:%M"),
                      "rel": str(p.relative_to(root)).replace("\\", "/"),
                      "snippet": body[:90]})
        if len(files) >= 20:
            break
    return {"files": files}


@app.get("/api/note")
def note(request: Request, path: str = ""):
    """读取单篇笔记全文（仅限当前用户库内的 .md）。"""
    u = require(request)
    root = storage.user_dir(u).resolve()
    if not path.strip():
        raise HTTPException(400, "缺少 path")
    p = (root / path).resolve()
    if not str(p).startswith(str(root)) or p.suffix.lower() != ".md" or not p.is_file():
        raise HTTPException(404, "笔记不存在")
    text = p.read_text(encoding="utf-8")
    import datetime
    m = datetime.datetime.fromtimestamp(p.stat().st_mtime)
    body = storage.strip_frontmatter(text)
    body = storage.strip_md_fence(body)
    return {"path": path, "name": p.name, "content": body,
            "mtime": m.strftime("%Y-%m-%d %H:%M")}


# ---- 附件上传与整理 ----

@app.post("/api/upload")
def upload_api(request: Request, body: dict):
    """上传图片/文件：base64 存到用户库 _assets，返回相对路径与类型。"""
    u = require(request)
    name = (body.get("name") or "file").strip()
    data_b64 = body.get("data") or ""
    if not data_b64:
        raise HTTPException(400, "缺少文件内容")
    import upload as upload_mod
    try:
        raw = upload_mod.decode_upload(data_b64)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not raw:
        raise HTTPException(400, "文件为空")
    kind = "image" if upload_mod.is_image(name) else "file"
    rel = storage.save_asset(u, name, raw)
    return {"ok": True, "name": name, "path": rel, "kind": kind}


@app.post("/api/process")
def process_api(request: Request, body: dict):
    """对已上传附件执行动作：
    - 文件 + action=organize：提取文本 → AI 归类 → 存笔记
    - 图片 + action=ocr：识别图片文字 → 归类 → 存笔记
    """
    u = require(request)
    rel = (body.get("path") or "").strip()
    action = (body.get("action") or "").strip()
    p = storage.asset_path(u, rel)
    if not p:
        raise HTTPException(404, "附件不存在")
    import upload as upload_mod
    try:
        data = p.read_bytes()
        if upload_mod.is_image(p.name):
            if action == "ocr":
                text = upload_mod.ocr_image(u, data)
                text = (text or "").strip()
                if not text or text in ("（无文字）", "(无文字)"):
                    return {"ok": True, "action": "ocr", "note": None, "text": "", "msg": "图片里没识别到文字"}
                cat, nrel = upload_mod.categorize_and_write(u, text, source="ocr")
                return {"ok": True, "action": "ocr", "category": cat, "note": nrel, "text": text[:800]}
            # 默认：图片已在上传时保存
            return {"ok": True, "action": "save", "msg": "图片已存入图库：%s" % rel}
        # 文件
        if action == "organize":
            cat, nrel = upload_mod.organize(u, p.name, data)
            return {"ok": True, "action": "organize", "category": cat, "note": nrel}
        text = upload_mod.extract_text(p.name, data)
        return {"ok": True, "action": "extract", "text": text[:2000]}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, "处理失败：%s" % e)


@app.get("/api/projects")
def projects(request: Request):
    u = require(request)
    st = storage.load_state(u)
    return {"list": st.get("projects", {}), "active": st.get("active")}


@app.get("/api/ctx")
def ctx(request: Request):
    u = require(request)
    st = storage.load_state(u)
    active = st.get("active")
    name = st.get("projects", {}).get(active, {}).get("name", active) if active else None
    return {"active": name}


@app.post("/api/ask")
def ask(request: Request, body: dict):
    u = require(request)
    q = (body.get("question") or "").strip()
    if not q:
        return {"reply": ""}
    history = body.get("history") or []
    msgs = []
    for m in history[-10:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": q})
    # 增强模型白名单（付费旗舰，勾选才用；顺序即优先级）
    PREMIUM = ("glm-5.3", "glm-5.2", "glm-5.3-flash", "glm-5")
    premium = [m for m in (body.get("models") or []) if m in PREMIUM]
    ans = llm.chat_messages(msgs, tier="main", max_tokens=1500, models=premium)
    return {"reply": ans}
