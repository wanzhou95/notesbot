import os
import secrets
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response

import config
import storage
import rag
import users as users_mod
from config import ENV, NOTES_ROOT, DATA_DIR
from graph import build_graph, llm
from llm import load_model_settings, save_model_settings

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
    # 必须走 config.USERS：reload_users() 会重新绑定模块属性，
    # 之前 from config import USERS 拿的是旧字典，新账号要重启才能登录。
    if user not in config.USERS:
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
    import tasks
    t = tasks.read_task(u, slug)
    if not t:
        raise HTTPException(404, "任务不存在")
    if not t.get("version"):
        raise HTTPException(400, "该任务还没有行动攻略")
    import export as export_mod
    try:
        out = export_mod.export(u, slug, fmt)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, "导出失败：%s" % e)
    from urllib.parse import quote
    filename = quote("%s-行动攻略.%s" % (t["name"], fmt))
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
    return {"list": storage.list_projects(u), "active": st.get("active")}


# ---- 任务体系（文件为唯一真相，Obsidian 勾完这里就能看到）----

def _task_node(t, kids):
    import tasks
    d = {
        "slug": t["slug"], "name": t["name"], "status": t["status"],
        "done": t["done"], "total": t["total"],
        "parent": t["parent"], "related": t["related"],
        "version": t["version"], "updated": t["updated"],
        "path": t["path"], "steps": t["steps"], "mtime": t["mtime"],
    }
    d["children"] = [_task_node(c, kids)
                     for c in sorted(kids.get(t["name"], []), key=lambda x: x["mtime"], reverse=True)]
    return d


@app.get("/api/tasks")
def api_tasks(request: Request):
    u = require(request)
    import tasks
    st = storage.load_state(u)
    ts = tasks.list_tasks(u)
    by_name = {t["name"]: t for t in ts}
    kids, roots = {}, []
    for t in ts:
        p = t.get("parent")
        if p and p in by_name and p != t["name"]:
            kids.setdefault(p, []).append(t)
        else:
            roots.append(t)
    groups = {}
    for key in ("todo", "doing", "done"):
        groups[key] = [_task_node(t, kids) for t in
                       sorted([x for x in roots if x["status"] == key],
                              key=lambda x: x["mtime"], reverse=True)]
    return {"groups": groups, "stats": tasks.stats(u, ts), "active": st.get("active")}


@app.post("/api/task/new")
def api_task_new(request: Request, body: dict):
    u = require(request)
    import tasks
    name = (body.get("name") or "").strip()
    status = body.get("status") or "todo"
    if status not in ("todo", "doing"):
        status = "todo"
    parent = (body.get("parent") or "").strip() or None
    slug, new = tasks.create_task(u, name, status=status, parent=parent)
    if not slug:
        return JSONResponse({"ok": False, "err": "任务名不能为空"})
    if status == "doing":
        st = storage.load_state(u)
        st["active"] = slug
        storage.save_state(u, st)
    return {"ok": True, "slug": slug, "new": new}


@app.post("/api/task/step")
def api_task_step(request: Request, body: dict):
    u = require(request)
    import tasks
    slug = (body.get("slug") or "").strip()
    try:
        n = int(body.get("n") or 0)
    except Exception:
        n = 0
    if not slug or n <= 0:
        raise HTTPException(400, "参数不全")
    ok = tasks.set_step_done(u, slug, n, bool(body.get("done", True)))
    return {"ok": ok}


@app.post("/api/task/status")
def api_task_status(request: Request, body: dict):
    u = require(request)
    import tasks
    slug = (body.get("slug") or "").strip()
    status = (body.get("status") or "").strip()
    if status not in ("todo", "doing", "done"):
        raise HTTPException(400, "状态不合法")
    ok = tasks.set_status(u, slug, status)
    if ok:
        st = storage.load_state(u)
        if status == "done" and st.get("active") == slug:
            st["active"] = None
            storage.save_state(u, st)
        elif status == "doing":
            st["active"] = slug
            storage.save_state(u, st)
    return {"ok": ok}


@app.post("/api/task/link")
def api_task_link(request: Request, body: dict):
    """设置上级 / 添加相关任务。"""
    u = require(request)
    import tasks
    slug = (body.get("slug") or "").strip()
    parent = (body.get("parent") or "").strip()
    related = [x for x in (body.get("related") or []) if x]
    if not slug:
        raise HTTPException(400, "缺少 slug")
    if parent:
        tasks.set_parent(u, slug, parent)
    if related:
        tasks.add_related(u, slug, related)
    return {"ok": True}


@app.post("/api/digest")
def api_digest(request: Request, body: dict):
    """手动触发一轮归纳（定时任务同一套逻辑）。"""
    u = require(request)
    import digest
    try:
        return digest.run(u, dry_run=bool(body.get("dry_run")))
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)})


@app.get("/api/skills")
def api_skills(request: Request):
    """技能卡列表：给网页「技能」页渲染，也保留旧的 list 字段兼容。"""
    u = require(request)
    import skills
    cards = skills.list_cards(u)
    return {
        "list": ["%s/%s" % (c["rel"].split("/")[0], c["stem"]) for c in cards],
        "cards": [{
            "rel": c["rel"], "title": c["title"], "kind": c["kind"],
            "aliases": c["aliases"], "when": c["when"],
            "steps": len(c["steps"]), "checks": len(c["checks"]),
        } for c in cards],
    }


@app.post("/api/skill/use")
def api_skill_use(request: Request, body: dict):
    """网页「用它」：把技能卡步骤导成任务骨架。"""
    u = require(request)
    import skills
    import tasks
    res = skills.use_card(u, (body.get("title") or "").strip())
    if not res.get("ok"):
        return JSONResponse({"ok": False, "err": res.get("err", "没找到技能卡")})
    card = res["card"]
    if not res.get("slug"):
        return {"ok": True, "slug": None, "title": card["title"],
                "reply": "《%s》是知识页，没有可执行步骤。" % card["title"]}
    t = tasks.read_task(u, res["slug"]) or {}
    return {"ok": True, "slug": res["slug"], "title": card["title"],
            "added": res["added"], "steps": [s["text"] for s in t.get("steps", [])]}


@app.get("/api/ctx")
def ctx(request: Request):
    u = require(request)
    st = storage.load_state(u)
    active = st.get("active")
    name = None
    if active:
        import tasks
        t = tasks.read_task(u, active)
        name = t["name"] if t else active
    return {"active": name, "slug": active}


@app.get("/api/videos")
def api_videos(request: Request):
    """已生成的视频速览（这些笔记不进向量检索）。"""
    u = require(request)
    import video
    return {"list": video.list_notes(u), "dir": video.VIDEO_DIR,
            "max_minutes": video.MAX_MINUTES}


@app.post("/api/video/url")
def api_video_url(request: Request, body: dict):
    u = require(request)
    import video
    url = (body.get("url") or "").strip()
    if not video.is_url(url):
        raise HTTPException(400, "请粘贴完整的视频链接（http/https 开头）")
    if not video.ffmpeg_exe():
        raise HTTPException(500, "服务器缺少 ffmpeg，无法转写")
    old = video.find_by_source(u, url)
    if old:
        return {"ok": True, "done": True, "note": old["rel"], "title": old["title"],
                "msg": "这个链接已经总结过了"}
    jid = video.start_job(u, url=url)
    return {"ok": True, "id": jid}


VIDEO_MAX_BYTES = 2 * 1024 * 1024 * 1024      # 2GB


@app.post("/api/video/upload")
def api_video_upload(request: Request, file: UploadFile = File(...)):
    """本地视频/音频上传：流式落盘（不走 base64，大文件才扛得住）。"""
    u = require(request)
    import video
    name = os.path.basename(file.filename or "video.mp4")
    if not video.is_media(name):
        raise HTTPException(400, "只支持视频/音频文件（mp4/mkv/mov/flv/webm/mp3/m4a/wav…）")
    jid = video.create_job(u, name)
    work = video.job_workdir(jid)
    work.mkdir(parents=True, exist_ok=True)
    dst = work / ("src." + video.media_ext(name))
    total = 0
    try:
        with open(dst, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > VIDEO_MAX_BYTES:
                    raise HTTPException(413, "文件超过 2GB，先压缩一下再传")
                out.write(chunk)
        if total < 1024:
            raise HTTPException(400, "文件是空的")
    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    video.launch(jid, file_path=dst)
    return {"ok": True, "id": jid, "size": total}


@app.get("/api/video/status")
def api_video_status(request: Request, id: str = ""):
    require(request)
    import video
    j = video.get_job(id)
    if not j:
        raise HTTPException(404, "任务不存在或已过期（服务重启过？）")
    return j


@app.get("/api/video-cookies")
def get_video_cookies(request: Request):
    """Cookie 配置状态（不返回明文）。"""
    require(request)
    import video
    return {
        "bili_set": video.BILI_COOKIE_FILE.exists(),
        "ytdlp_set": video.COOKIE_FILE.exists(),
        "douyin_ready": (video.DOUYIN_YDLP_DIR / "yt_dlp").is_dir(),
    }


@app.post("/api/video-cookies")
def set_video_cookies(request: Request, body: dict):
    """保存 B 站 / 通用平台 Cookie，落盘即生效（仅管理员）。"""
    u = require(request)
    if config.load_users().get(u, {}).get("role") != "admin":
        return JSONResponse({"ok": False, "err": "只有管理员可以修改"})
    import video
    bili = (body.get("bili") or "").strip()
    ytdlp = (body.get("ytdlp") or "").strip()
    if bili:
        video.BILI_COOKIE_FILE.write_text(bili.replace("\n", "").strip(), encoding="utf-8")
    elif video.BILI_COOKIE_FILE.exists():
        video.BILI_COOKIE_FILE.unlink()
    if ytdlp:
        video.COOKIE_FILE.write_text(ytdlp.strip() + "\n", encoding="utf-8")
    elif video.COOKIE_FILE.exists():
        video.COOKIE_FILE.unlink()
    return {"ok": True}


@app.post("/api/ask")
def ask(request: Request, body: dict):
    u = require(request)
    q = (body.get("question") or "").strip()
    if not q:
        return {"reply": ""}
    history = body.get("history") or []
    msgs = []
    # A 向召回：问 AI 时也带上库里已沉淀的技能卡（这是「自己的经验」而不是通用知识）
    import skills
    card_ctx = skills.context_block(u, q, limit=2)
    if card_ctx:
        msgs.append({"role": "system",
                     "content": card_ctx + "\n\n以上资料里有技能卡时，优先按卡里的步骤回答，"
                                           "并注明「按《卡名》」；卡里没写的别硬编。"})
    for m in history[-10:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": q})
    # 增强模型白名单（付费旗舰，勾选才用；顺序即优先级）
    PREMIUM = ("glm-5.3", "glm-5.2", "glm-5.3-flash", "glm-5")
    s = load_model_settings()  # 设置页关掉的增强模型也一并拦截
    premium = [m for m in (body.get("models") or []) if m in PREMIUM and s["premium"].get(m, True)]
    ans = llm.chat_messages(msgs, tier="main", max_tokens=1500, models=premium)
    return {"reply": ans}


# ===== 模型开关（设置页）=====
PREMIUM_MODELS = {  # 展示名：给前端渲染开关列表
    "glm-5.3-flash": ("GLM-5.3-Flash", "推荐·近免费（0.4/1.4元每百万tokens 五折）"),
    "glm-5.3": ("GLM-5.3", "旗舰（8/28元每百万tokens）"),
    "glm-5.2": ("GLM-5.2", "旗舰（8/28元每百万tokens）"),
    "glm-5": ("GLM-5", "上代旗舰·便宜（4/18元每百万tokens）"),
}


@app.get("/api/model-settings")
def get_model_settings(request: Request):
    require(request)
    s = load_model_settings()
    # premium 里没记录的模型默认开
    premium = {k: bool(s["premium"].get(k, True)) for k in PREMIUM_MODELS}
    return {"deepseek": s["deepseek"], "premium": premium, "models": PREMIUM_MODELS}


@app.post("/api/model-settings")
def set_model_settings(request: Request, body: dict):
    u = require(request)
    if config.load_users().get(u, {}).get("role") != "admin":
        return JSONResponse({"ok": False, "err": "只有管理员可以修改模型设置"})
    s = {
        "deepseek": bool(body.get("deepseek", True)),
        "premium": {k: bool(v) for k, v in (body.get("premium") or {}).items() if k in PREMIUM_MODELS},
    }
    save_model_settings(s)
    return {"ok": True}
