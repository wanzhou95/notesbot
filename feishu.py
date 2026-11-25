"""飞书 bot 接入层：飞书事件 → graph 管线 → 飞书回推。

只做三件事，业务逻辑一行不写：把飞书消息翻译成 graph 的输入、把 graph 的 reply
推回飞书、拦住不该进来的消息。记笔记 / 任务 / 视频 / 问答全部复用
`graph.build_graph()`，所以网页版能做的，飞书里都能做。

四个坑，每个对应一段代码：

1. **3 秒 ACK**。飞书要求回调 3 秒内响应，超时重推。而一次问答要 15~30 秒
   （`llm.py` 用的是同步 `httpx.Client`，会阻塞）。所以路由只做
   「验签 → 去重 → 返回 200」，真正的处理丢给 FastAPI 的 BackgroundTasks；
   传进去的是**同步函数**，Starlette 会把它扔进线程池，不堵事件循环。
   结果用 `im/v1/messages` 主动推回去。

2. **事件重推 = 重复写库**。按 `header.event_id` 落 sqlite 去重，TTL 24 小时。

3. **谁都能把自建应用加进群**。不做白名单，陌生人就能往你笔记库里写东西。
   未授权 open_id 只回一句提示（**附带它自己的 open_id**，方便你复制来绑），
   并单独记一行日志。

4. **加密策略**。飞书后台一旦开启「加密策略」，body 就是 AES 密文，解开要
   pycryptodome。本机没装，也不打算为它加依赖，所以这里显式拒绝并给出可操作
   的错误提示 —— 验证靠 Verification Token + open_id 白名单已经足够。

配置（全部走 .env，`config.load_env()` 里有 FEISHU_ 前缀白名单，每次调用现读，
改完不用重启）：

    FEISHU_APP_ID=cli_xxx
    FEISHU_APP_SECRET=xxx
    FEISHU_VERIFY_TOKEN=xxx

绑定关系放 `data/feishu_identity.json`（不进 git，含 open_id），格式：

    {"ou_xxxx": "wanzhou"}

也可以用 `manage.py feishu-bind <open_id> <账号>` 写，不用手改文件。
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

import config

FEISHU_BASE = "https://open.feishu.cn/open-apis"
TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
MSG_PATH = "/im/v1/messages"
APP_TOKEN_PATH = "/auth/v3/app_access_token/internal"
USER_TOKEN_PATH = "/authen/v1/access_token"
# 飞书客户端内用 index（已登录飞书即无感授权）；外部浏览器用扫码页
AUTHORIZE_IN_APP = "https://open.feishu.cn/open-apis/authen/v1/index"
AUTHORIZE_IN_BROWSER = "https://open.feishu.cn/open-apis/authen/v1/user_auth_page_beta"

MAX_CHARS = 3000          # 单条消息上限，超了按段落切
DEDUP_TTL = 24 * 3600     # 事件去重保留时长

# 飞书文本消息里 @ 会变成 @_user_1 这种占位符，回答前要剔掉
_MENTION_RE = re.compile(r"@_user_\d+\s*")

_client = httpx.Client(timeout=30)
_tok_lock = threading.Lock()
_tok: dict[str, Any] = {"token": "", "exp": 0.0}
_app_tok: dict[str, Any] = {"token": "", "exp": 0.0}


def log(msg: str) -> None:
    """记一行运行日志。

    **必须走 stderr 且 flush**：`notesbot.service` 没设 PYTHONUNBUFFERED，
    print 到管道是块缓冲，日志会压在缓冲区里直到写满或进程退出——线上排查时
    等于「日志不存在」。2026-11-25 首次联调就踩到了：笔记已经落库、回复已经
    发出，但 journalctl 里一行都看不到，白查了十分钟。
    """
    print("[feishu] " + msg, file=sys.stderr, flush=True)


# ==================== 配置 ====================

def cfg(key: str, default: str = "") -> str:
    """读 .env 里的配置。每次现读，改 .env 不用重启。"""
    try:
        return (config.load_env() or {}).get(key, default) or default
    except Exception:
        return default


def configured() -> bool:
    """三件套齐了才算能真正收发。缺了也不影响回调通过 challenge 校验。"""
    return bool(cfg("FEISHU_APP_ID") and cfg("FEISHU_APP_SECRET"))


def identity_path() -> Path:
    return config.DATA_DIR / "feishu_identity.json"


def bindings() -> dict[str, str]:
    """open_id -> 账号名。json 优先，回落 config.IDENTITY_MAP（历史预留位）。"""
    out: dict[str, str] = {}
    try:
        out.update(config.IDENTITY_MAP or {})
    except Exception:
        pass
    p = identity_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.update({str(k): str(v) for k, v in data.items()})
        except Exception:
            pass
    return out


def identity_of(open_id: str) -> Optional[str]:
    """飞书 open_id → 本地账号名。没绑定返回 None。"""
    if not open_id:
        return None
    return bindings().get(open_id)


def bind(open_id: str, user: str) -> None:
    """写入一条绑定关系（merge，不覆盖其他人）。"""
    p = identity_path()
    data: dict[str, str] = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                data = {str(k): str(v) for k, v in old.items()}
        except Exception:
            data = {}
    data[open_id] = user
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)   # 含 open_id，不给别人读
    except Exception:
        pass


# ==================== 飞书 API ====================

def tenant_token(force: bool = False) -> str:
    """租户令牌，有效期 2 小时，提前 5 分钟刷新。"""
    now = time.time()
    with _tok_lock:
        if not force and _tok["token"] and _tok["exp"] > now:
            return str(_tok["token"])
        app_id, secret = cfg("FEISHU_APP_ID"), cfg("FEISHU_APP_SECRET")
        if not app_id or not secret:
            raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        r = _client.post(FEISHU_BASE + TOKEN_PATH,
                         json={"app_id": app_id, "app_secret": secret})
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("取 tenant_access_token 失败: %s %s"
                               % (d.get("code"), d.get("msg")))
        _tok["token"] = d.get("tenant_access_token", "")
        _tok["exp"] = now + max(60, int(d.get("expire", 7200)) - 300)
        return str(_tok["token"])


def send_text(receive_id: str, text: str, id_type: str = "chat_id") -> bool:
    """发一条文本消息。失败返回 False 并记日志（不抛，避免拖垮后台任务）。"""
    if not receive_id or not text:
        return False
    if not configured():
        log("未配置应用凭据，跳过发送")
        return False
    try:
        r = _client.post(
            FEISHU_BASE + MSG_PATH,
            params={"receive_id_type": id_type},
            headers={"Authorization": "Bearer %s" % tenant_token()},
            json={"receive_id": receive_id, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
        d = r.json()
        if d.get("code") != 0:
            log("发送失败: %s %s" % (d.get("code"), d.get("msg")))
            return False
        # 成功也留一行 message_id，出事时能拿它去飞书后台「消息查询」对账
        mid = ((d.get("data") or {}).get("message_id") or "")[:18]
        log("已发送 %d 字 -> %s (msg %s)" % (len(text), receive_id[:12], mid))
        return True
    except Exception as e:
        log("发送异常: %s: %s" % (type(e).__name__, str(e)[:200]))
        return False


def _chunks(text: str, size: int = MAX_CHARS) -> list[str]:
    """按段落切分超长回复，尽量在换行处断开。"""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, buf = [], ""
    for line in text.split("\n"):
        while len(line) > size:            # 单行就超长，硬切
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) + 1 > size:
            out.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        out.append(buf)
    return out


# ==================== 网页应用免登录 ====================

def app_token(force: bool = False) -> str:
    """应用身份令牌，换 user_access_token 时用。和 tenant_token 是两个东西。"""
    now = time.time()
    with _tok_lock:
        if not force and _app_tok["token"] and _app_tok["exp"] > now:
            return str(_app_tok["token"])
        app_id, secret = cfg("FEISHU_APP_ID"), cfg("FEISHU_APP_SECRET")
        if not app_id or not secret:
            raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        r = _client.post(FEISHU_BASE + APP_TOKEN_PATH,
                         json={"app_id": app_id, "app_secret": secret})
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("取 app_access_token 失败: %s %s"
                               % (d.get("code"), d.get("msg")))
        _app_tok["token"] = d.get("app_access_token", "")
        _app_tok["exp"] = now + max(60, int(d.get("expire", 7200)) - 300)
        return str(_app_tok["token"])


def authorize_url(redirect_uri: str, state: str, in_app: bool = True) -> str:
    """拼授权跳转地址。

    `in_app` 决定用哪个页面：飞书客户端内用 `authen/v1/index`（用户已登录飞书，
    点一下就连带授权，扫码都省了）；外部浏览器只能用扫码页 `user_auth_page_beta`。
    这两条路径用错会白屏或提示「请在飞书中打开」，是免登最常见的坑。
    """
    base = AUTHORIZE_IN_APP if in_app else AUTHORIZE_IN_BROWSER
    q = urlencode({"app_id": cfg("FEISHU_APP_ID"),
                   "redirect_uri": redirect_uri,
                   "state": state})
    return base + "?" + q


def exchange_code(code: str) -> tuple[bool, dict]:
    """用登录预授权码换 open_id。返回 (ok, data)；成功时 data 至少含 open_id。

    文档确认：`authen/v1/access_token` 的响应体**直接带 open_id**，且该接口权限
    要求为「无」——所以不必再申请「获取用户信息」权限，也不必调
    `/authen/v1/user_info`。少一个权限就少一处配错的机会。
    """
    if not code:
        return False, {"err": "缺少 code"}
    try:
        r = _client.post(
            FEISHU_BASE + USER_TOKEN_PATH,
            headers={"Authorization": "Bearer %s" % app_token(),
                     "Content-Type": "application/json; charset=utf-8"},
            json={"grant_type": "authorization_code", "code": code},
        )
        d = r.json()
    except Exception as e:
        return False, {"err": "换 token 异常: %s: %s" % (type(e).__name__, str(e)[:160])}
    if d.get("code") != 0:
        return False, {"err": "飞书返回 %s %s" % (d.get("code"), d.get("msg"))}
    data = d.get("data") or {}
    if not data.get("open_id"):
        return False, {"err": "响应里没有 open_id"}
    return True, data


# ==================== 事件去重 ====================

def _db() -> sqlite3.Connection:
    """事件去重库。每次新开连接，避免线程池里跨线程复用。"""
    p = config.DATA_DIR / "feishu_events.db"
    c = sqlite3.connect(str(p), timeout=5)
    c.execute("create table if not exists seen(event_id text primary key, ts real)")
    return c


def mark_seen(event_id: str) -> bool:
    """首次见到返回 True；重复事件返回 False。

    飞书同一事件会重推 2~3 次，不去重就会把同一条笔记写两遍、把同一个视频
    排两次队。
    """
    if not event_id:
        return True
    try:
        c = _db()
        cur = c.execute("insert or ignore into seen(event_id, ts) values(?,?)",
                        (event_id, time.time()))
        c.commit()
        fresh = cur.rowcount > 0
        if fresh:
            c.execute("delete from seen where ts < ?", (time.time() - DEDUP_TTL,))
            c.commit()
        c.close()
        return fresh
    except Exception as e:
        # 去重库坏了也别把消息吞掉：宁可可能重复一次，也不能丢消息
        log("去重库异常: %s" % str(e)[:150])
        return True


# ==================== 事件解析 ====================

def _text_of(message: dict) -> str:
    """飞书 text 消息的 content 是 JSON 字符串，形如 {"text":"..."}。"""
    raw = message.get("content") or ""
    try:
        d = json.loads(raw)
        t = d.get("text") or "" if isinstance(d, dict) else ""
    except Exception:
        t = ""
    return _MENTION_RE.sub("", str(t)).strip()


def log_unknown(open_id: str, preview: str) -> None:
    """未授权的 open_id 单独记一行，方便取证和绑定。"""
    try:
        p = config.DATA_DIR / "feishu_unknown.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                      open_id, (preview or "")[:120]))
    except Exception:
        pass


def parse_event(raw: dict) -> tuple[dict, Optional[tuple[str, str, str]]]:
    """解析一次回调。

    Returns:
        (要回给飞书的 body, 待执行任务 (user, chat_id, text) 或 None)。
        任务由路由丢进 BackgroundTasks，本函数必须**不阻塞**——飞书只等 3 秒。
    """
    if not isinstance(raw, dict):
        return {"code": 400, "msg": "bad body"}, None

    # 坑 4：加密策略开着的话 body 是密文，我们解不了，明确要求关掉
    if raw.get("encrypt"):
        return {"code": 400,
                "msg": "已开启加密策略。请到飞书后台「事件与回调 → 加密策略」关闭后重试"}, None

    # URL 校验：飞书保存回调地址时的一次性请求。**故意放在验签之前**。
    # 它只是把对方自己发来的 challenge 原样回声，不碰任何数据、不泄露任何东西；
    # 而真正能写笔记的是事件，那一层照样严格验签。若先验签，一旦飞书这次
    # 校验请求的 token 字段与预期有出入，等于自己把回调地址堵死 —— 这是配置
    # 阶段最容易卡住的一步，不值得为它冒险。
    if raw.get("type") == "url_verification" or raw.get("challenge"):
        return {"challenge": raw.get("challenge", "")}, None

    # 验签：v1 是 body.token，v2 是 header.token
    token = str(raw.get("token") or (raw.get("header") or {}).get("token") or "")
    expect = cfg("FEISHU_VERIFY_TOKEN")
    if expect and not hmac.compare_digest(token, expect):
        return {"code": 401, "msg": "verification token 不匹配"}, None

    header = raw.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return {"code": 0, "msg": "ignored"}, None

    event_id = str(header.get("event_id") or "")
    if not mark_seen(event_id):
        return {"code": 0, "msg": "duplicate"}, None

    event = raw.get("event") or {}
    message = event.get("message") or {}
    open_id = str(((event.get("sender") or {}).get("sender_id") or {}).get("open_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not open_id or not chat_id:
        return {"code": 0, "msg": "no sender or chat"}, None

    # 坑 3：白名单
    user = identity_of(open_id)
    if not user:
        log_unknown(open_id, _text_of(message) or message.get("message_type", ""))
        send_text(chat_id, "未授权。\n你的 open_id：%s\n把它发给管理员加入白名单后即可使用。" % open_id)
        return {"code": 0, "msg": "unauthorized"}, None

    if message.get("message_type") != "text":
        send_text(chat_id, "目前只支持文字消息，图片/文件稍后再支持。")
        return {"code": 0, "msg": "unsupported type"}, None

    text = _text_of(message)
    if not text:
        return {"code": 0, "msg": "empty"}, None

    return {"code": 0, "msg": "ok"}, (user, chat_id, text)


# ==================== 后台处理 ====================

def run_job(graph, user: str, chat_id: str, text: str) -> None:
    """BackgroundTasks 里跑的同步任务（Starlette 会放进线程池）。

    注意：这里可能跑几十秒（视频转写更久），所以调用方必须已经返回 200。
    """
    t0 = time.time()
    try:
        state = {"user": user, "channel": "feishu", "text": text,
                 "intent": "", "cmd": "", "arg": "", "project": "", "reply": ""}
        out = graph.invoke(state) or {}
        reply = str(out.get("reply") or "").strip() or "（处理完成，但没有产生回复）"
    except Exception as e:
        log("处理异常: %s: %s" % (type(e).__name__, str(e)[:300]))
        reply = "处理时出错了：%s" % str(e)[:200]
    for part in _chunks(reply):
        if not send_text(chat_id, part):
            break
    log("%s 处理完成，%d 字，耗时 %.1fs" % (user, len(reply), time.time() - t0))


def status() -> dict:
    """给 manage.py feishu-status 用的自检信息。"""
    b = bindings()
    unknown: list[str] = []
    p = config.DATA_DIR / "feishu_unknown.log"
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[-50:]:
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] not in unknown:
                    unknown.append(parts[1])
        except Exception:
            pass
    return {
        "configured": configured(),
        "app_id_tail": cfg("FEISHU_APP_ID")[-6:],
        "verify_token_set": bool(cfg("FEISHU_VERIFY_TOKEN")),
        "bindings": b,
        "unknown_open_ids": unknown[-10:],
        "identity_file": str(identity_path()),
    }
