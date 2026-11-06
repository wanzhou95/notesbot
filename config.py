import json
import os
from pathlib import Path

# ================= 路径配置 =================
# 所有路径都支持环境变量覆盖，未设置时用默认值（默认针对 Linux 部署）。
# 换机器部署时，可在 .env 里写 NOTES_ROOT / NOTESBOT_DATA / NOTESBOT_ENV 覆盖。
ENV_PATH = os.environ.get("NOTESBOT_ENV", "/srv/app/notesbot/.env")
NOTES_ROOT = Path(os.environ.get("NOTES_ROOT", "/srv/notes"))
DATA_DIR = Path(os.environ.get("NOTESBOT_DATA", "/srv/app/notesbot/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"       # 用户元数据（名字/角色/目录名）
PASSWD_FILE = DATA_DIR / "web_users.json"  # 登录密码哈希（兼容旧文件名）


def load_env():
    """读取 .env 文件；再叠加进程环境变量（后者优先，便于 docker 等场景）。"""
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    _prefixes = ("NOTESBOT_", "DEEPSEEK_", "GLM_", "FEISHU_", "WECOM_",
                 "EMBED_", "MODEL_", "ADMIN_", "PASS_", "WEB_")
    env.update({k: v for k, v in os.environ.items()
                if k == "USERS" or k.startswith(_prefixes)})
    return env


ENV = load_env()

# ================= 目录分类（PARA） =================
CATEGORIES = ["00-灵感库", "01-Projects", "02-Areas", "03-Resources",
              "04-Archive", "05-Skills", "06-Wiki"]

DEFAULT_CATEGORY_DESC = """---
标题: 目录结构说明
tags: [系统, 分类]
---

# 目录结构说明

本库采用以下 7 个分类目录，AI 保存内容时请按此归类：

| 文件夹 | 说明 |
|--------|------|
| 00-灵感库 | 灵感记录，记录突发奇想的灵感 |
| 01-Projects | 现在正在做的事情（进行中的项目、学习任务） |
| 02-Areas | 处理好的、长期维护的知识 |
| 03-Resources | 资源：读的书、参考资料、网上下载的资料 |
| 04-Archive | 暂时用不到、先保存归档的内容 |
| 05-Skills | 重复性工作归纳成的技能，方便复用（回答"怎么做"） |
| 06-Wiki | 定型的知识词条：概念的定义、要点、关联（回答"是什么"） |

## 归类规则
1. 突发灵感、一闪而过的想法 → 00-灵感库
2. 正在推进的项目/学习过程 → 01-Projects
3. 读完的书、下载的资料、参考链接 → 03-Resources
4. 已成型的技能、可复用的操作流程（怎么做）→ 05-Skills
5. 定型的知识词条：某概念的定义、要点、关联（是什么）→ 06-Wiki
6. 长期维护、已整理好的知识 → 02-Areas
7. 暂时不用但想留着的 → 04-Archive
8. 拿不准时，默认放 00-灵感库，并在回复中说明，等用户手动整理

> 用户可随时修改本文件来调整归类规则，AI 每次归类前都会读取本文件。
"""

# ================= 用户管理 =================

def env_users():
    """从 .env 的 USERS 解析初始用户：USERS=alice:admin,bob:user"""
    raw = ENV.get("USERS", "").strip()
    users = {}
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                name, role = item.split(":", 1)
            else:
                name, role = item, "user"
            name = name.strip()
            role = (role.strip() or "user").lower()
            if name:
                users[name] = {"dir": name, "role": role}
    return users


def load_users():
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_users(users):
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# 运行时用户表：优先 users.json，其次 .env 的 USERS；空则由首次启动 bootstrap 兜底
USERS = load_users() or env_users()


def reload_users():
    global USERS
    USERS = load_users() or env_users()


def user_dir(user: str) -> Path:
    if user not in USERS:
        raise KeyError("未知用户：%s（请先通过 manage.py adduser 创建）" % user)
    return NOTES_ROOT / USERS[user].get("dir", user)


def ensure_user_dirs(user: str) -> Path:
    """创建某用户的库目录结构（7 分类 + _index + _assets + 目录说明）。

    注意：不依赖全局 USERS（后者可能尚未 reload），直接从最新状态取目录名。
    """
    users = load_users() or env_users() or {user: {"dir": user}}
    d = NOTES_ROOT / users.get(user, {"dir": user}).get("dir", user)
    for c in CATEGORIES:
        (d / c).mkdir(parents=True, exist_ok=True)
    (d / "_index").mkdir(parents=True, exist_ok=True)
    (d / "_assets").mkdir(parents=True, exist_ok=True)
    desc = d / "_index" / "目录说明.md"
    if not desc.exists():
        desc.write_text(DEFAULT_CATEGORY_DESC, encoding="utf-8")
    return d


# 平台身份 -> 用户 key（拿到飞书/企微 openid 后填）
IDENTITY_MAP = {
    # "ou_xxxx": "你的账号名",
}


def resolve_identity(platform_id: str):
    return IDENTITY_MAP.get(platform_id)
