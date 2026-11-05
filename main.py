from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import config
import users as users_mod
from graph import build_graph

app = FastAPI(title="NotesBot")
GRAPH = None

# 首次启动：确保至少一个账号
_boot_msg = users_mod.ensure_bootstrapped()
if _boot_msg:
    print(_boot_msg)


@app.on_event("startup")
def startup():
    global GRAPH
    GRAPH = build_graph()


class Msg(BaseModel):
    user: str
    text: str
    channel: str = "debug"


@app.get("/health")
def health():
    config.reload_users()
    return {"ok": True, "users": list(config.USERS.keys())}


@app.post("/debug/msg")
def debug_msg(m: Msg):
    config.reload_users()
    if m.user not in config.USERS:
        raise HTTPException(400, "unknown user: %s" % m.user)
    state = {"user": m.user, "channel": m.channel, "text": m.text,
             "intent": "", "cmd": "", "arg": "", "project": "", "reply": ""}
    out = GRAPH.invoke(state)
    return {"reply": out.get("reply", ""), "intent": out.get("intent", "")}
