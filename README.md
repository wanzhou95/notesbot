# NotesBot 知识库

> 👉 **给使用者（装什么软件、怎么连接、从哪进）看这里：[使用说明.md](./使用说明.md)**
> 👉 **给部署者（下载后怎么跑起来、怎么设账号）看这里：[部署指南.md](./部署指南.md)**

基于 Obsidian + Syncthing + LangGraph + RAG 的个人/家庭知识库服务端。多人各自独立知识库，通过飞书/企微/网页端三入口记录、检索、问答。**开箱即用**：`python manage.py init` 即可自动建账号和目录。

## 功能

- **记录**：文字 / 语音 / 图片 / 文件，AI 自动按 PARA 目录归类
- **项目会话**：`开始 X` → 逐条记步骤 → `结束` 生成《行动攻略》→ 按项目名/步骤号问答
- **RAG 语义检索**：笔记向量化（sqlite-vec + 智谱 embedding-2），语义问答
- **问 AI**：直接问大模型回答任何问题（多轮对话）
- **多用户隔离**：每人独立库；管理员可跨库只读
- **攻略导出**：Word / PDF（export.py）
- **三端同步**：Syncthing（服务器 + 电脑 + 手机）

## 目录结构

```
├── config.py           配置（路径、用户元数据、环境变量）
├── users.py            账号密码管理 + 首次启动初始化
├── manage.py           命令行管理工具（init/adduser/passwd/list/reindex）
├── llm.py              大模型调用（DeepSeek → GLM 自动降级）
├── storage.py          笔记读写、项目会话
├── graph.py            LangGraph 编排（意图识别/命令/问答）
├── web.py              网页端 + REST 接口
├── embedding.py        Embedding 封装
├── vector_store.py     sqlite-vec 向量库
├── rag.py              语义检索与索引
├── export.py           攻略导出 Word/PDF
├── upload.py           附件上传处理
└── static/             前端（index.html / login.html）
```

## 快速开始（3 步）

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 配置 .env（最少填一个模型 key，可选配 USERS 列表）
cp .env.example .env

# 3. 初始化账号和目录，然后启动
python manage.py init
uvicorn web:app --host 127.0.0.1 --port 18080
```

首次启动会自动建管理员账号（未配 `ADMIN_PASS` 时密码打印在日志里）。完整说明见 [部署指南.md](./部署指南.md)。

## 命令集

```
开始 X   建项目/切过去
切到 X   切换项目
离开     暂时离开当前项目
继续 X   重开已完成项目
结束     完结并生成攻略
列表 / 当前 / 导出 X
记 X     强制记笔记
```
