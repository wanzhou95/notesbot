# NotesBot · 个人 / 家庭知识库服务端

把「随手记」这件事彻底自动化：你在微信、飞书或网页里发一句话、一段语音、一张图，服务端自动理解、自动分类归档到你的 Obsidian 知识库，并且让 AI 能在你的全部笔记里做语义检索和问答。

- 多人使用，**各自独立的库**，互不可见（管理员可跨库只读）
- 数据落在服务器，用 Syncthing 同步到你电脑和手机的 Obsidian
- 全流程支持一句话部署，**不含任何个人私有信息**，可以直接拿去用

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心功能](#核心功能)
- [整体架构](#整体架构)
- [快速开始（3 步）](#快速开始3-步)
- [配置说明](#配置说明)
- [用户与权限](#用户与权限)
- [命令集](#命令集)
- [任务体系：文件是唯一真相](#任务体系文件是唯一真相)
- [知识库目录结构（PARA）](#知识库目录结构para)
- [定时归纳：把过程和笔记沉淀成技能](#定时归纳把过程和笔记沉淀成技能)
- [三个入口怎么接](#三个入口怎么接)
- [生产部署](#生产部署)
- [常见问题](#常见问题)
- [二次开发与私有定制](#二次开发与私有定制)
- [代码仓库怎么规划](#代码仓库怎么规划)
- [安全清单](#安全清单)

---

## 它解决什么问题

传统笔记的最大问题不是「记不下来」，而是**记完就找不到了**。分类靠手动、搜索靠关键词、整理靠自律，最后笔记变成垃圾场。

NotesBot 的做法：

1. **入口极简**——不用打开 App，在聊天窗口发出去就行
2. **分类自动化**——AI 读你的目录说明文件，判断该放哪个分类目录
3. **检索语义化**——你说「上次那个做饭的想法」，它能找到你写的「做菜的新点子」
4. **项目可回溯**——一句话开启项目会话，边做边记步骤，收尾时自动生成《行动攻略》

典型场景：

```
你：开始 装修厨房
Bot：✅ 项目已创建：装修厨房
你：量了橱柜 2.4 米
Bot：📌 已记录第 1 步
你：预算大概 1.2 万
Bot：📌 已记录第 2 步
你：结束
Bot：📋 已生成《行动攻略》，共 8 步……

（三天后）
你：装修厨房第 3 步是什么
Bot：第 3 步是「预约水电改造」……
```

---

## 核心功能

| 功能 | 说明 |
|---|---|
| **多模态记录** | 文字、语音（自动转写）、图片（OCR）、链接（抓正文）、文件附件 |
| **AI 自动归类** | 按 PARA 七分类归档，规则由你写的 `_index/目录说明.md` 决定 |
| **项目会话** | `开始 X` → 逐条记步骤 → `结束` 生成行动攻略；支持 `离开`（挂起）/ `继续 X`（恢复） |
| **RAG 语义检索** | 笔记自动切块向量化（sqlite-vec + 智谱 embedding-2，免费），支持自然语言找人话 |
| **问 AI** | 直接对模型提问，多轮对话，可选带自己的笔记上下文 |
| **多用户隔离** | 每人一个库目录，目录级隔离；管理员可跨库只读（不可写） |
| **攻略导出** | 《行动攻略》一键导出 Word / PDF |
| **三端入口** | 网页端 / 飞书机器人 / 企业微信机器人，共用同一后端 |
| **模型自动降级** | 主力 DeepSeek 余额耗尽时自动切到免费 GLM，服务不中断 |

---

## 整体架构

```
 手机 / 电脑 / 网页
        │
        ├── 网页端（FastAPI + 原生 JS）
        ├── 飞书机器人（Webhook）
        └── 企业微信机器人（回调）
                    │
                    ▼
        ┌───────────────────────┐
        │  LangGraph 意图编排层  │
        │  分类 → 命令/步骤/笔记/问答  │
        └───────────┬───────────┘
                    │
        ┌───────────┴───────────┬──────────────┐
        ▼                       ▼              ▼
   写入知识库              RAG 检索        LLM 调用
   (Markdown 文件)     (sqlite-vec)   DeepSeek → GLM
        │
        ▼
   /notes/<用户>/00-灵感库 … 06-Wiki
        │
        ▼
   Syncthing ──→ 你的电脑 Obsidian ──→ 你的手机 Obsidian
```

**为什么是文件而不是数据库？** 因为知识库的寿命比程序长。Markdown 文件二十年后的软件还能打开，专有格式不行。数据库只用来存索引和会话，随时可以从文件重建（`manage.py reindex`）。

---

## 快速开始（3 步）

前置：Python 3.11+，Linux（Windows/macOS 去掉 systemd 部分即可）

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 配置（最少填一个模型 API Key）
cp .env.example .env
vi .env

# 3. 初始化账号 + 目录，然后启动
python manage.py init
uvicorn web:app --host 127.0.0.1 --port 18080
```

打开 `http://127.0.0.1:18080` 登录。首次启动若未设置密码，随机密码会打印在日志里。

> `weasyprint` 用于 PDF 导出，需要系统库：CentOS/OpenCloudOS `yum install -y pango cairo gdk-pixbuf2`；Debian/Ubuntu `apt install -y libpango-1.0-0 libcairo2`。不用 PDF 导出可忽略，Word 导出不受影响。

---

## 配置说明

所有配置都在 `.env`（模板见 `.env.example`），代码通过环境变量读取，**不含任何硬编码的账号密码**。

### 大模型（至少配一组）

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | 二选一 | — | DeepSeek 密钥，主力模型 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | 兼容 OpenAI 协议的地址 |
| `MODEL_ROUTER` | 否 | `deepseek-flash` | 意图识别用的快模型 |
| `MODEL_MAIN` | 否 | `deepseek-v4-pro` | 正文生成用的强模型 |
| `GLM_API_KEY` | 二选一 | — | 智谱密钥，备用 + Embedding 复用 |
| `GLM_BASE_URL` | 否 | `https://open.bigmodel.cn/api/paas/v4` | |
| `MODEL_GLM` | 否 | `glm-4-flash` | 免费备用模型 |
| `MODEL_FALLBACK` | 否 | `glm-4-flash,glm-4.7` | 降级链，逗号分隔 |
| `GLM_DISABLE_THINKING` | 否 | `1` | **别关**。GLM 是混合思考模型，不关会导致 token 全耗在推理、正文为空 |

> 两家都配是最稳的：主模型欠费自动降级，服务不中断。只配一家也能跑。

### Embedding（语义检索）

| 变量 | 必填 | 说明 |
|---|---|---|
| `EMBED_API_KEY` | 建议 | 留空则复用 `GLM_API_KEY` |
| `EMBED_BASE_URL` | 否 | 留空则复用 `GLM_BASE_URL` |
| `EMBED_MODEL` | 否 | `embedding-2`（1024 维，免费） |

> 不配 Embedding 时，检索会自动退化为关键词分词匹配，功能可用但语义能力打折。

### 用户

| 变量 | 说明 |
|---|---|
| `USERS` | 初始用户列表：`alice:admin,bob:user,carol:user`。`admin`=可跨库只读，`user`=只见自己 |
| `PASS_<名字>` | 对应账号的初始密码，如 `PASS_alice=你的密码`。不填则随机生成并打印到日志 |
| `ADMIN_USER` / `ADMIN_PASS` | 单用户模式的默认管理员（不配 `USERS` 时生效） |

### 路径（换机器部署时改这里）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `NOTES_ROOT` | `/srv/notes` | 所有用户的库根目录 |
| `NOTESBOT_DATA` | `/srv/app/notesbot/data` | 运行时数据（用户表、会话、向量库） |
| `NOTESBOT_ENV` | `/srv/app/notesbot/.env` | 配置文件路径 |

---

## 用户与权限

### 三种角色

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| `admin` | 自己的库读写 + 其他所有人的库**只读**检索 | 不能修改、删除别人的笔记 |
| `user` | 只见、只能操作自己的库 | 看不到其他人的存在 |

隔离是**物理级**的：每个用户一个独立目录（`/srv/notes/<用户>/`），不是靠 SQL 条件过滤。权限判断在服务端完成，前端拿不到越权接口。

### 日常管理

```bash
python manage.py list                 # 看所有用户和角色
python manage.py adduser 张三 user     # 加人（自动建 7 个分类目录 + 目录说明）
python manage.py passwd 张三          # 改密码（交互式输入，不回显）
python manage.py reindex 张三         # 重建某人的向量索引
python manage.py reindex --all        # 全量重建
```

---

## 命令集

在聊天窗口直接发这些指令：

```
【记录】
开始 X          创建任务 X 并进入记录模式
记 X            强制当笔记记录（绕过意图识别）
离开            暂时离开当前任务（不结束，步骤保留）

【任务】
想做 X          只登记一句话想法，进「待办」，不动手开记
想做 X 属于 Y   建子任务，挂在任务 Y 下面
完成 N          把当前任务的第 N 步标记为完成
任务完成 X      整个任务标记完成
切到 X          切换到已有任务
继续 X          重新打开一个已完成任务
结束            完结当前任务，生成《行动攻略》
列表            按待办 / 进行中 / 已完成分组列出
当前            看当前任务的步骤和进度

【关联与沉淀】
关联 X          当前任务关联任务 X（Obsidian 里两张卡会连线）
上级 X          设置当前任务的上级
沉淀 X          把最近结束的任务提炼成技能卡，写入 05-Skills
技能列表        看已沉淀的技能卡 / 知识页（含步数与别名）
用 X            按技能卡 X 的步骤开一条任务，带你一步步走
怎么做 X        同「用 X」（X 必须对得上某张卡的标题或别名）
导出 X          把攻略导出为 Word / PDF
```

没有命中命令的普通陈述句，会被当作笔记自动归类；带问号或含「还差/进度/哪些没」的会被当作问答。**意图识别是规则优先的**，比纯模型判断稳定得多，也省钱。

---

## 任务体系：文件是唯一真相

这是本项目最核心的设计取舍：**任务状态不存数据库，只存 Markdown 文件**。

```
01-Projects/
├── 00-任务总览.md                    ← bot 自动生成的三段式任务表
└── 2026-11-05-剪辑视频学习/
    ├── 剪辑视频学习.md               ← ★ 主文件：frontmatter + checkbox 步骤
    ├── 步骤记录.md                   ← 时间流水（append-only，供检索）
    └── 行动攻略.md                   ← 结束后生成
```

主文件长这样：

```markdown
---
title: 剪辑视频学习
status: doing                    ← todo / doing / done
parent:                          ← 上级任务，写 [[任务名]]
related: ["[[摄影构图]]"]        ← 相关任务，可多个
---

# 剪辑视频学习

- [ ] 这件事已完成              ← 手机端一键完成入口

## 步骤
- [x] 1. 装剪映，界面分三块
- [ ] 2. 学关键帧

## 关联
- 上级：
- 相关：[[摄影构图]]
- 子任务：[[学调色]]
```

**为什么这样做**：你在 Obsidian 里勾一下 checkbox、或者改一下 `status:`，bot 下次读文件就知道了 —— 不需要任何同步逻辑，也不会出现「网页改了但 Obsidian 没变」。文件本身就是同步点。

**主文件名必须等于任务名**（不是 `_project.md`）。Obsidian 双链按文件名解析，多个同名文件会让 `[[学调色]]` 指向不明，星图和链接全废。

**总览文件** `00-任务总览.md` 由 bot 每次任务变更时重写，人工改会被覆盖。

---

## 知识库目录结构（PARA）

每个用户初始化后长这样：

```
/srv/notes/<用户名>/
├── 00-灵感库/          突发奇想、一闪而过的念头
├── 01-Projects/        正在做的事（任务都建在这里）
├── 02-Areas/           长期维护、已整理的知识
├── 03-Resources/       读的书、参考资料、下载的资料
├── 04-Archive/         暂时用不到但想留着的
├── 05-Skills/          可复用的操作技能（how-to：步骤+坑+检查项）
├── 06-Wiki/            定型的知识词条（what-is：定义+要点+边界）
├── _index/
│   ├── 目录说明.md      ★ AI 的分类依据，你可以随时改
│   ├── 归纳台账.md      归纳去重记录（源文件哈希 → 产出）
│   └── 归纳日报.md      每次自动归纳的处理结果
└── _assets/            图片、附件、语音原件
```

**05-Skills 与 06-Wiki 的分工**：前者回答「下次怎么做这件事」，后者回答「这个概念是什么」。skills 里有 `aliases` 别名列表，搜「补间」也能命中「关键帧操作」。

**`_index/目录说明.md` 是整个系统的灵魂。** 每次 AI 归类前都会读它。你觉得分类不准，不用改代码，改这个文件就行——加一句「所有和宠物相关的都放 02-Areas」，立刻生效。

---

## 定时归纳：把过程和笔记沉淀成技能

每天凌晨 3 点自动跑一轮（systemd timer），做这件事：

```
已完成的任务 ─┐
              ├─→ AI 归纳 ─→ 05-Skills/<技能名>.md（技能卡，带别名）
标记沉淀的笔记 ┘
```

**候选池是受控的**，只处理两类，不做全库扫描：

1. `status: done` 的任务
2. frontmatter 里写了 `沉淀: true` 的笔记

**去重靠哈希台账**：`_index/归纳台账.md` 记录「源文件 → 内容哈希 → 产出路径」。内容没变就跳过，改过才重跑。

**首周默认草稿模式**（`DIGEST_DRAFT=1`）：产出落到 `05-Skills/_待审/`，人工确认后再挪进正式目录。觉得靠谱了改成 `0` 直接写入。

```bash
# 手动跑一轮（不调模型，只看候选）
python manage.py digest --dry

# 手动跑一轮（真实归纳）
python manage.py digest

# 单个用户、限 2 个
python manage.py digest alice --limit 2
```

**成本**：一次归纳约 4K 输入 + 1K 输出，一天 3 个，走免费额度基本为零。

---

## 技能卡怎么真的用起来（召回 + 执行）

归纳出来的卡如果没人调用，就只是躺在文件夹里的死文档。所以这里做了两条闭环：

### A 向：写笔记 / 问 AI 时自动带上技能卡

- **写笔记**：笔记正文里出现某张卡的标题或别名 → 自动在笔记末尾挂一行
  `> 相关技能：[[关键帧操作]]`，Obsidian 星图直接连线，回复里也会告诉你「发『用 关键帧操作』可以照着做」。
- **问 AI**（网页「问AI」和聊天问答都一样）：命中卡片时把卡里的**完整步骤**塞进 prompt，
  并明确要求「优先按卡里的步骤回答，注明按哪张卡」。所以它答的是**你自己总结的流程**，不是网上通用废话。

召回用的是本地打分，**不走向量检索**（省 embedding 钱，也更容易解释）：

| 证据 | 权重 | 说明 |
|---|---|---|
| 标题 / 别名出现在问句里 | 5 + 长度 | 强证据，`aliases` 就是为这个准备的 |
| 正文（标题+别名+适用+步骤）二元组重合 | 每命中 1 个 +1 | 弱证据，≥5 才算命中 |
| 卡片类型 | 技能卡 +0.8 | 同分时「怎么做」优先给可执行流程，不给名词解释 |

写笔记挂链接时用 **strict 模式**（只认标题/别名命中），避免正文偶然撞词就乱连线。

### B 向：发「用 X」，把卡变成任务

```
你：用 关键帧操作
Bot：按技能卡《关键帧操作》开了任务（共 4 步，已设为当前任务）：
     1. 在起点位置点「菱形」按钮，打第一个关键帧
     2. 拖动时间线到终点，改参数（位置/缩放/不透明度）
     ...
     做完一步发「完成 N」，或网页/手机 Obsidian 里直接勾。
     卡里记的坑：位置和缩放要分别打帧…
```

做的事就三件：建任务（`source_skill` 记录来源）→ 把「操作步骤」批量写成 checkbox 步骤 →
设为当前任务。之后就是任务体系那套：一句句记、勾选、结束出攻略、再沉淀成新卡。
**技能卡是「怎么做」，任务是「正在做」**，两者共用同一套文件格式，不需要额外同步。

`怎么做 X` 是同一个命令的别名。

**命令判定是「整句即名字」**：只有 `用 <卡名>` / `用 <别名>` 这种干净写法才当命令执行。
你在记录模式下说「用剪映导出素材」「用关键帧做过场再淡出」，仍然老老实实记成一步 ——
半句话想问东西的话，走问 AI 那条路照样能答（它也会召回卡片）。

### 网页「技能」页

列出所有技能卡 / 知识页，显示适用场景、步数、检查项、别名，两个按钮：

- **用它开任务** → 直接导成任务骨架（等价于发「用 X」）
- **看全文** → 打开卡片原文

### 防止「自我喂养」

「用卡开出来的任务」如果一步没记就完成，归纳它只会把同一张卡再抄一遍。所以
`source_skill` 有值且没有 `步骤记录.md` 的任务，**不进归纳候选池**；只要你记了哪怕一句，
它就重新变成有效素材。

---

## 视频 → 字幕 → 速览（不进检索）

给一个视频链接或丢一个视频文件，得到一份「一句话总结 + 章节 + 要点 + 完整字幕」，
**只给你看，不进向量库**（字幕动辄上万字，embed 又贵又把检索结果冲淡）。

```
B 站链接 ──→ 官方 API 取标题/时长 ──┬─ 有平台字幕 → 直接拉（0 成本）
                                    └─ 没字幕 → 下 DASH 音频 → 切 28 秒段 → GLM-ASR
其它链接 ──→ yt-dlp ──→ 同上
本地文件 ──→ 直接切片 → GLM-ASR
                ↓
        03-Resources/视频字幕/<标题>.md（速览在上，完整字幕在下）
```

### 为什么 B 站不用 yt-dlp

**机房 IP 抓 B 站网页会被风控直接返回 412 Precondition Failed**，换浏览器 UA、
加 Referer、带 buvid3 cookie 都救不了（实测）。能走通的是官方接口：

| 接口 | 用途 |
|---|---|
| `api.bilibili.com/x/web-interface/view` | 标题、时长、分P、CID |
| `api.bilibili.com/x/player/v2` | 字幕列表（多数视频匿名看不到，配 `BILI_COOKIE` 才行） |
| `api.bilibili.com/x/player/playurl`（`fnval=16`） | DASH 音频流（挑码率最低那条，够 ASR 用） |

CDN（`*.bilivideo.cn`）下载正常，所以「下音频 → 转写」这条路是稳的。

### 命令与入口

```
总结 <视频链接>    开始处理（粘贴 B 站/抖音链接也行，视频站链接会直接触发）
视频列表           看已生成的速览
看视频 <标题关键词> 看章节与要点（完整字幕在网页）
```

网页「视频」页：粘链接或**上传本地视频**（单文件 ≤ 2GB，流式落盘不进 base64），
进度条实时显示「转写中 12/48 段」，完成后卡片里直接看章节、要点，点「完整字幕」看原文。

### 成本与限制

- 平台自带字幕：0 成本；没有字幕才走 ASR，**约 0.015 元/分钟**（1 小时视频约 0.9 元）。
- GLM-ASR 单次上限 **30 秒 / 25MB**，所以按 28 秒切片，上一段结果当 `prompt` 回传，
  人名术语不会每段重猜。
- 默认单个视频上限 180 分钟（`VIDEO_MAX_MIN`），超了直接拒，防手滑。
- 同一链接第二次提交不会重复花钱（按 frontmatter 的 `source` 去重）。
- 抖音等平台经常要登录态：把浏览器导出的 `cookies.txt` 填到 `YTDLP_COOKIES` 再用。

### 依赖

```bash
pip install yt-dlp imageio-ffmpeg python-multipart
```

`imageio-ffmpeg` 自带静态 ffmpeg 二进制，服务器没装 ffmpeg 也能切片（代码里优先用系统 ffmpeg）。
国内服务器装依赖建议加镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ...`

---

## 三个入口怎么接

### 网页端（默认可用）

服务起来就能用，`http://<你的地址>:<端口>`。功能最全：记录、搜索、问 AI、导出。

### 飞书

1. [飞书开放平台](https://open.feishu.cn/) 创建企业自建应用
2. 开启「机器人」能力，订阅 `im.message.receive_v1` 事件
3. 事件订阅地址填 `https://<你的域名>/feishu/webhook`
4. 把 `App ID` / `App Secret` 填进 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
5. 在 `config.py` 的 `IDENTITY_MAP` 里把飞书 openid 映射到你的账号名

### 企业微信

1. 企业微信管理后台 → 应用管理 → 创建自建应用
2. 接收消息回调 URL 填 `https://<你的域名>/wecom/callback`
3. `WECOM_BOT_ID` / `WECOM_BOT_SECRET` 填进 `.env`

> 飞书 vs 企微怎么选：家里用、只做消息往来，**飞书**文档和 API 更友好，个人可以注册；已经有企业微信组织、要给同事用，选**企微**。两个都接也没问题，后端是同一套。

---

## 生产部署

### systemd 服务

```ini
# /etc/systemd/system/notesbot.service
[Unit]
Description=NotesBot 知识库服务
After=network.target

[Service]
Type=simple
User=notesbot
WorkingDirectory=/srv/app/notesbot/app
Environment=PYTHONPATH=/srv/app/notesbot:/srv/app/notesbot/app
Environment=NOTESBOT_ENV=/srv/app/notesbot/.env
ExecStart=/srv/app/notesbot/venv/bin/uvicorn web:app --host 127.0.0.1 --port 18080
Restart=always
RestartSec=5
MemoryMax=500M
# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now notesbot
journalctl -u notesbot -f          # 看日志
```

### nginx 反向代理

```nginx
server {
    listen 8080;
    server_name _;
    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;      # AI 生成慢，别用默认 60s
    }
}
```

> **域名要有备案**才能在境内服务器的 80/443 上正常访问，否则会被云厂商拦截页劫持。没备案就先用 `IP:端口`。

### 备份

```bash
# 知识库本体就是文件，直接打包
tar czf notes-$(date +%F).tar.gz -C /srv notes
# 运行时数据（含用户表和向量索引，可重建但重建费时）
tar czf notesbot-data-$(date +%F).tar.gz -C /srv/app/notesbot data
```

建议配个 cron 每天凌晨跑，保留最近 7 份。

---

## 常见问题

**Q：服务起来了但 502 / 打不开**
先看 `journalctl -u notesbot -n 50`。最常见是 `ModuleNotFoundError`——`PYTHONPATH` 没设对，必须同时包含 `/srv/app/notesbot`（上层）和 `/srv/app/notesbot/app`。

**Q：AI 回复是空的**
GLM 系模型没关思考。检查 `GLM_DISABLE_THINKING=1`。

**Q：主模型报余额不足**
正常，会自动降级到 `MODEL_FALLBACK`。日志里会有一行「降级到 glm-4-flash」，服务不中断。

**Q：搜索搜不到**
1. 先确认有没有建索引：`python manage.py reindex <用户>`
2. 检查笔记是否在排除目录里（`.git` / `_index` / `.obsidian` / `.trash` / `.stversions` 不索引）
3. 没配 Embedding 时会退化成关键词匹配，措辞不同就搜不到——配上 key 再试

**Q：登录后点搜索/项目没反应**
前端 JS 报错。打开浏览器控制台看，若是 `xxx is not defined`，通常是模板字符串里的 HTML/JS 转义炸了，改成独立静态文件加载。

**Q：重启后要重新登录**
会话没持久化。确认 `data/sessions.json` 能正常写入，且 `NOTESBOT_DATA` 指向的目录有写权限。

**Q：笔记分类不准**
改 `_index/目录说明.md`，加你自己的归类规则。这是设计上留的口子，不用改代码。

**Q：Obsidian 手机端占空间吗**
只同步 Markdown 文本，几十 MB 级别。图片附件取决于 `_assets` 大小，可以在 Syncthing 里排除 `_assets`。

**Q：家人不会用命令行怎么办**
不用会。他们只需要在网页上发消息，或者扫码用 Syncthing 同步到手机 Obsidian。所有管理操作你一个人做就行。

---

## 二次开发与私有定制

代码结构按职责分层，改哪块很清楚：

| 文件 | 职责 | 想改什么来这里 |
|---|---|---|
| `config.py` | 路径、分类目录、用户表 | 改目录结构、加分类 |
| `graph.py` | 意图识别 + 流程编排 | 加新命令、改判断规则 |
| `storage.py` | 笔记读写、项目会话 | 改文件名格式、元数据 |
| `llm.py` | 模型调用 + 降级 | 换模型、改 prompt |
| `rag.py` / `vector_store.py` | 检索与索引 | 改切块大小、换向量库 |
| `web.py` | 路由与接口 | 加 API |
| `static/` | 前端 | 改界面 |
| `manage.py` | 命令工具 | 加运维命令 |
| `deploy/` | systemd + nginx 部署模板 | 生产部署直接拷贝改路径 |

### 私有定制怎么加

如果你想要一些**不打算公开**的功能（比如对接自己家的设备、企业内部系统），不要 fork 一个私有分支——把定制放进 `extras/` 目录，它已经被 `.gitignore` 排除：

```python
# extras/__init__.py（不提交到公开仓库）
def register(app):
    """私有扩展入口：在这里挂路由、注册命令、改 prompt。"""
    @app.get("/api/my-private-hook")
    def _hook():
        return {"ok": True}
```

```python
# web.py 末尾（这行提交到公开仓库，是通用的扩展点）
try:
    from extras import register as register_extras
    register_extras(app)
except ImportError:
    pass          # 没有 extras 目录就正常跳过，公开版开箱即用
```

**判断标准**：差异能用配置或扩展点表达的，就别分叉。一个项目只有一条代码血缘，否则每个 bug 都要修两遍。

---

## 代码仓库怎么规划

一个常见的误解是「私有版和公开版 = 两个项目」。不是。

- **仓库**是分发单元，一个项目可以有多个仓库（代码 / 私有配置 / 文档站）
- **项目**是代码血缘，共享同一个 bug、同一次重构，就是一个项目

推荐结构：

| 仓库 | 可见性 | 内容 |
|---|---|---|
| `notesbot` | Public | 全部代码、脱敏文档、`.env.example`、部署模板、防泄漏钩子 |
| `notesbot-private` | Private | `.env`、私有信息说明、真实 nginx 配置、备份脚本 |

服务器上：

```
/srv/app/notesbot/
├── app/                    ← clone 公开代码仓库
├── private/                ← clone 私有配置仓库
├── .env -> private/.env    ← 软链，代码读固定路径，无需改配置
└── 私有信息.md -> private/私有信息.md
```

好处：公开仓库从出生就不含私有信息，是**结构上安全**而不是靠自觉；换服务器恢复只需 clone 两次 + `manage.py init`。

### 防泄漏钩子

`scripts/pre-commit` 会扫描暂存区，命中敏感串就拒绝提交：

```bash
cp scripts/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
cp scripts/sensitive-patterns.txt.example scripts/sensitive-patterns.txt
vi scripts/sensitive-patterns.txt          # 填入你自己的敏感串（此文件不入库）
```

> 钩子是兜底，不是主防线。主防线是私有信息根本不在仓库目录里。

---

## 安全清单

部署完对着这个表检查一遍：

- [ ] `.env` 权限 `600`，且确认 `git check-ignore .env` 有输出
- [ ] 服务只监听 `127.0.0.1`，对外由 nginx 代理
- [ ] 服务器 SSH 改为密钥登录，禁用密码登录
- [ ] 云厂商安全组只放行必要端口
- [ ] 密码用 `manage.py passwd` 设置，别写在明文文档里
- [ ] `data/` 和 `/srv/notes/` 不在任何 Git 仓库内
- [ ] 公开仓库推送前跑一次：`git grep -nE '<你的IP>|<你的域名>|<你的账号>'`，应无输出
- [ ] 配了每日备份，并且**验证过能恢复**

---

## 致谢与许可

依赖 [LangGraph](https://github.com/langchain-ai/langgraph)、[sqlite-vec](https://github.com/asg017/sqlite-vec)、[FastAPI](https://fastapi.tiangolo.com/)、[Obsidian](https://obsidian.md/)、[Syncthing](https://syncthing.net/)。

MIT License —— 拿去用，改，商用都行，不提供担保。
