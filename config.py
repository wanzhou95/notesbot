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
| 01-Projects | 任务专用目录（进行中的项目/学习任务）。普通笔记不要放这里，请用「开始 X」建任务 |
| 02-Areas | 处理好的、长期维护的知识 |
| 03-Resources | 资源：读的书、参考资料、网上下载的资料 |
| 04-Archive | 暂时用不到、先保存归档的内容 |
| 05-Skills | 重复性工作归纳成的技能，方便复用（回答"怎么做"） |
| 06-Wiki | 定型的知识词条：概念的定义、要点、关联（回答"是什么"） |

## 归类规则
1. 突发灵感、一闪而过的想法 → 00-灵感库
2. 正在推进的项目/学习过程 → **不要归类到这里**，改用「开始 X」/「想做 X」建任务（本目录由任务体系管理）
3. 读完的书、下载的资料、参考链接 → 03-Resources
4. 已成型的技能、可复用的操作流程（怎么做）→ 05-Skills
5. 定型的知识词条：某概念的定义、要点、关联（是什么）→ 06-Wiki
6. 长期维护、已整理好的知识 → 02-Areas
7. 暂时不用但想留着的 → 04-Archive
8. 拿不准时，默认放 00-灵感库，并在回复中说明，等用户手动整理

> 用户可随时修改本文件来调整归类规则，AI 每次归类前都会读取本文件。
"""

AGENTS_CONTRACT = """---
标题: Agent 工作契约
tags: [系统, 契约]
---

# AGENTS.md — 本库的 AI 工作契约

> 你（AI / Agent）每次读写这个知识库前，必须先读本文件。它定义你在这里的边界和工作流。
> 用户可随时修改本文件来调整你的行为。

## 三层边界
- 原始资料（Raw）：用户写下的任务步骤记录、行动攻略、外部材料。**只读，不篡改。**
- 正式页（Wiki）：05-Skills / 06-Wiki / 02-Areas 里已审核转正的卡片与词条。**只新建，不改写。**
- 元数据（Meta）：_index/ 下的台账、日报、日志、目录。由系统维护，你只读不写。

## 归类规则
1. 突发灵感、一闪而过的想法 → 00-灵感库
2. 正在推进的项目/学习过程 → **不要归类**，改用「开始 X」/「想做 X」建任务（01-Projects 由任务体系管理）
3. 读完的书、下载的资料、参考链接 → 03-Resources
4. 已成型的技能、可复用的操作流程（怎么做）→ 05-Skills
5. 定型的知识词条：概念的定义、要点、关联（是什么）→ 06-Wiki
6. 长期维护、已整理好的知识 → 02-Areas
7. 暂时不用但想留着的 → 04-Archive
8. 拿不准时，默认放 00-灵感库，并在回复中说明

## 审核闸门（最重要）
- 你产出的技能卡/知识页，只能写进 `<目录>/_待审/`，**禁止直接写正式目录**。
- 用户点「通过」后才会转正；「否定」会移进 .trash/。
- 你的产出**只新建、不改写**已有页；要更新已有页，先给修订建议，等用户确认。

## 三个操作
- Ingest（摄入）：写笔记时自动归类；命中技能卡自动挂双链；只有根任务结束才归纳出卡。
- Query（提问）：带问号的问题，先查技能卡 → 再查任务实时状态 → 最后 RAG 检索，回答带来源。
- Lint（体检）：定期扫描孤儿页、断链、矛盾、待审积压，只写报告不改页面。

## 命名与 frontmatter 约定
- 双链用 [[完整文件名|标题]]；frontmatter 用 aliases / sources / tags。
- 任务目录已物理嵌套：子任务目录在父任务目录里，slug 是相对路径「父/子」。

## 已拍板的硬规矩
- 只有最顶层（无 parent）的根任务结束后才归纳，素材汇总整棵子树。
- 草稿模式：产出进 _待审/，人工通过才转正。
- 视频字幕不进向量检索。
- 01-Projects 是任务体系专用，禁止自然语言移动/改名。
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
    # AI 工作契约：只在不存在时写入，存在则保留用户手改（与目录说明同策略）
    agents = d / "_index" / "AGENTS.md"
    if not agents.exists():
        agents.write_text(AGENTS_CONTRACT, encoding="utf-8")
    return d


# 平台身份 -> 用户 key（拿到飞书/企微 openid 后填）
IDENTITY_MAP = {
    # "ou_xxxx": "你的账号名",
}


def resolve_identity(platform_id: str):
    return IDENTITY_MAP.get(platform_id)
