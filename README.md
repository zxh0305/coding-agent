# Agent 问答 Demo → Coding Agent（前后端分离，从零实现，无框架）

一个用来学习 Agent 开发的迷你项目：从纯手写的 **LLM + 工具调用循环**起步，现已升级为一个界面形态对齐主流产品的 **coding agent**——左侧任务列表（多会话）、执行过程实时时间线、上下文容量显示、可切换工作区与模型。
后端纯 Python 标准库（连 Web 框架都没用），前端原生 HTML/CSS/JS（无构建步骤），零第三方依赖，每一步都看得见。

```
 浏览器 frontend/                        后端 backend/
┌──────────────────┐        ┌─────────────────────────┐      ┌──────────────┐
│ 任务列表（多会话） │        │  app.py (API)           │      │   LLM API    │
│ 执行过程时间线     │ ◀────▶ │  多会话 Agent + 工具循环  │◀────▶│ (OpenAI 兼容) │
│ 上下文容量/模型/   │  SSE   │  agent.py · code_tools  │      └──────────────┘
│ 工作区切换        │  JSON  │    └─────────────┘      │
└──────────────────┘        └─────────────────────────┘
                                          │
                                   workspace/  ← Agent 的工作区（前端可切换）
```

## 快速开始

```bash
python3 backend/app.py
# 浏览器打开 http://127.0.0.1:8000
# 左侧「模型配置」填 API Key（可顺便改 Base URL / 模型名）→ 保存 → 直接对话
```

也可以继续用命令行版（与网页共用同一份 `.env` 配置）：

```bash
python3 backend/cli.py                    # 交互式多轮对话
python3 backend/cli.py "37*89+100 等于多少"  # 单次提问
```

配置方式二选一：网页「模型配置」面板（自动写回 `.env`），或手动编辑 `.env`（模板见 `.env.example`）。

## 试试这些（coding agent 能力）

试试这些（coding agent 能力）之外，输入框还支持**附件**：点 📎 或直接选择文件，图片（≤4MB）作为视觉输入发给模型（需模型支持视觉），文本/代码文件（≤300KB）内容注入上下文；输入框上方会出现可删除的缩略图卡片，历史任务里也会回放。

Agent 的工作区是项目根目录的 `workspace/`（首次运行会自动生成两个练习文件），所有文件操作和命令执行都被限制在这个目录里：

- `工作区里的 demo.py 有个 bug，2+3 应该等于 5，修复并验证` → 感受 **读代码 → apply_patch 改 → run_bash 跑验证** 的完整循环
- `在工作区新建一个猜数字小游戏，写完自己玩一轮验证` → 让它自建项目并自测
- `帮我看 workspace 里哪个文件最长` → grep / list_dir 类任务

安全边界（务必了解）：路径越界（`../..`）会被拦截；`sudo`、全盘删除等高危命令被黑名单拒绝；bash 有 30 秒超时。**但这是学习级防护，不是安全边界——前端可把工作区切到任意本地文件夹（含重要数据的目录请三思），重要数据先备份，无人值守场景请上 Docker 隔离。**

## 学习路线（建议按此顺序精读）

| 文件 | 学什么 |
|---|---|
| `backend/tools.py` | 工具的 schema（说明书）与实现如何分离；统一执行器如何兜错误 |
| `backend/code_tools.py` | ★ coding agent 的核心工具集：工作区边界防护、apply_patch 锚定编辑、bash 黑名单/超时 |
| `backend/llm_client.py` | OpenAI 兼容 API 的请求格式；.env 的读写；为什么任意一家都能接 |
| `backend/llm_client.py`（AnthropicMessagesClient） | ★ 协议适配层：Anthropic Messages 的双向转换（system 顶层、tool_use/tool_result 块、input_json_delta 分片），对外暴露同样的接口，agent.py 零改动 |
| `backend/agent.py` | ★ **核心循环**：消息历史管理、工具调用回合、过程轨迹、防死循环 |
| `backend/app.py` | 不用框架怎么写 Web 服务：路由、JSON API、SSE 流式、静态托管、并发锁 |
| `frontend/app.js` | 原生 JS 怎么调 API：fetch、DOM 渲染、textContent 防 XSS、ReadableStream 手动解析 SSE |

## 一轮工具调用期间，消息历史长什么样

这是理解 Agent 最重要的一张图。问 `37*89+100 等于多少` 后，`agent.history` 依次变为：

```python
[
  {"role": "user",      "content": "37*89+100 等于多少"},
  {"role": "assistant", "content": None,
   "tool_calls": [{"id": "call_1", "type": "function",
                   "function": {"name": "calculator",
                                "arguments": "{\"expression\": \"37*89+100\"}"}}]},   # ①
  {"role": "tool",      "tool_call_id": "call_1",
   "content": "{\"expression\": \"37*89+100\", \"result\": 3393}"},                    # ②
  {"role": "assistant", "content": "37*89+100 = 3393。"}                                # ③
]
```

① 模型不再输出文字，而是输出一段结构化的"调用请求"（注意 `arguments` 是 **JSON 字符串**，不是 dict——模型输出本质都是文本，这是最常见的踩坑点）。
② 请求不会被自动执行！是我们的 `execute_tool` 在本地跑完，把结果作为 `role=tool` 消息塞回历史（`tool_call_id` 必须与请求配对）。
③ 模型看到工具结果后，产出最终文字回答，循环结束。

每轮请求都会把**从 system 开始的完整历史**发给 API——LLM 无状态，"记忆"完全靠客户端重发历史实现。

## 数据存储在哪

全部落盘在项目根目录的 **`agent_data.db`**（SQLite 单文件数据库，标准库 `sqlite3`，无额外依赖）：

| 表 | 存什么 |
|---|---|
| `sessions` | 任务列表：标题、创建/更新时间 |
| `messages` | 每个任务的完整消息历史（OpenAI 消息格式的 JSON，含工具调用） |
| `providers` | 模型供应商：名称 / Base URL / API Key / 启用状态 |
| `provider_models` | 供应商下的模型：模型名 / 上下文窗口 / 启用状态 |
| `settings` | 键值设置（当前激活的模型等） |

首次启动会把 `.env` 里的 LLM_* 配置自动导入为"默认"供应商；此后在网页「管理模型」里的改动都写入数据库（"默认"供应商的 Base URL/Key 改动会同步回 `.env`，保证命令行版一致）。API Key 以明文存本机库中（学习项目的务实选择），接口回显一律打码；`agent_data.db` 已加入 `.gitignore`。

## HTTP API 一览（`backend/app.py`）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/chat/stream` | POST | **流式问答（SSE）**：`session` 事件先行（返回任务 id），之后逐个推送 `round` / `answer_delta` / `tool_call` / `tool_result` / `usage`（token、耗时、上下文构成、缓存命中率）/ `done` / `error` |
| `/api/sessions` | GET / DELETE | 任务列表（id/标题/更新时间）；`?session_id=` 删除任务 |
| `/api/sessions/<id>/messages` | GET | 某任务的历史消息（切回任务时回放；助手消息附带当时的耗时/token 统计） |
| `/api/context` | GET | `?session_id=` 当前上下文容量（token 数 + 构成占比 + 缓存命中率） |
| `/api/models` | GET | 可用模型列表（各供应商已启用的模型，供工具栏切换） |
| `/api/active-model` | POST | 切换激活模型 `{"provider_id", "model"}` |
| `/api/providers` | GET | 供应商列表（含模型，Key 打码） |
| `/api/providers/save` / `delete` | POST | 新建/更新供应商；删除（"默认"供应商不可删） |
| `/api/providers/models/save` / `delete` | POST | 供应商下的模型增改/删除 |
| `/api/providers/test` | POST | **测试链接**：拿 Base URL/Key/模型名发一次真实 ping，返回延迟或错误 |
| `/api/config` | GET | 当前激活模型信息（供应商/模型/Key 打码/上下文窗口） |
| `/api/workspace` | GET / POST | 查看当前工作区；`{"path": "绝对路径"}` 切换（立即生效并写回 `.env`） |
| `/api/fs/dirs` | GET | `?path=...` 列出某目录的子目录，供选文件夹弹窗逐级浏览 |
| `/api/config` | GET | 当前配置，API Key 打码返回（只露前 3 后 4 位） |
| `/api/config` | POST | 修改配置并写回 `.env`，留空的字段保持不变，保存后立即生效 |
| `/api/tools` | GET | 已注册工具的 schema |
| `/api/reset` | POST | 清空服务端对话历史 |

几个值得注意的后端设计：配置变更后按需重建 Agent 但**保留对话历史**；`POST /api/chat` 全程持锁，避免并发请求弄乱历史；未配置 Key 时返回 400 加友好提示，而不是让服务崩掉。

## 关键设计点（都藏在代码注释里）

0. **多协议适配层**——每个供应商可配 `api_format`：`openai`（/chat/completions）或 `anthropic`（/v1/messages）。适配器把 Anthropic 的请求/响应/SSE 双向转换成 OpenAI 消息格式，对外接口完全一致，agent.py 与前端零改动。已支持的差异点：system 提示为顶层字段、工具参数 input_schema（对象）而非 arguments（字符串）、tool_use/tool_result 是 content 块且工具结果挂在 user 消息上、user/assistant 必须交替（相邻合并）、max_tokens 必填、SSE 事件为 message_start/content_block_delta/input_json_delta/message_delta。要接 OpenAI Responses 等新协议，在 `create_client` 工厂里加一个适配器分支即可。
0b. **视觉能力标记与 analyze_image 工具**——管理面板里每个模型可勾选"视觉"（`provider_models.vision`）。主模型没勾视觉时：发给它的消息会自动剥离图片、替换成文字提示（否则不支持视觉的服务商直接报 400），同时 `analyze_image` 工具借任意勾选了视觉的模型"代为看图"，把文字描述返回给主模型——用工具补偿模型短板。选择链：激活模型有视觉就直接用，否则找其他视觉模型，都没有则返回可读错误。

1. **assistant 的工具调用消息必须原样回填历史**——否则历史里出现"没有请求却冒出工具结果"的悬空消息，服务端直接报 400。
2. **工具报错不抛异常，而是把错误字符串返回给 LLM**——模型看到 `{"error": ...}` 后可以自己纠正参数重试。实测：`python` 不存在时模型会自己换 `python3`。
3. **apply_patch 用"锚定原文"的编辑协议**（Aider 的 SEARCH/REPLACE 思路）——search 必须逐字符匹配且唯一，失败模式只有"找不到"和"多处匹配"两种，都能反馈给模型自纠。比整文件重写省 token，比 diff 格式抗幻觉。
4. **工作区越界保护**——`resolve()` 消解 `../` 后校验必须仍在工作区内，模型传 `/etc/passwd` 或 `../../.env` 都会被拒。
5. **`max_rounds` 强制止损（默认 16）**——防止模型陷入"调工具→不满意→再调"的死循环烧钱；coding 任务一轮要多次往返，所以比普通问答的 8 大。
6. **calculator 不用 `eval`**——用 ast 白名单只允许四则运算，防止模型（或注入）执行任意代码。
7. **前端一律用 `textContent` 渲染**——不拼 `innerHTML`，天然防 XSS。
8. **流式链路（四层各有关卡）**——① LLM 层：`stream: true` 时工具调用是**分片**到达的，必须按 `index` 累积拼接 arguments；`stream_options: include_usage` 拿 token 用量（服务商不支持时自动降级重试）；② Agent 层：核心循环重构为 `run()` 生成器，边跑边产出事件，usage 跨轮累计；③ 后端：SSE 推送，刻意用 HTTP/1.0"关闭连接即结束"语义，免写 chunked 分块，且 `wbufsize=0` 保证每次 write 直接到网络；④ 前端：POST 不能用 EventSource，用 `fetch` + `ReadableStream` 按空行切分事件手动解析。
9. **工作区切换**——`WORKSPACE_DIR` 每次工具调用时动态读取（不是启动时定格），所以 Web 端改环境变量立即生效；选目录的接口只做最小校验（存在、非根目录），因为它的前提是"本机单人学习工具"。
10. **上下文容量估算**——没有本地分词器，用"服务商返回的真实 prompt_tokens ÷ 上次请求总字符数"校准出每字符 token 系数，再按 系统提示词/工具定义/用户消息/助手回复/工具结果 的字符占比分摊——估算值，但量级和占比可信；缓存命中率直接用 DeepSeek 返回的 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`。
11. **任务（多会话）**——每个任务一个独立 Agent 实例（独立对话历史），标题取第一条提问；模型配置变更后按需重建实例但保留历史。任务、消息（含每条助手消息的耗时/token 统计，存在消息的 `_stats` 内部字段里）都落盘 SQLite，重启不丢；发给模型前会剥离 `_` 前缀的内部字段（部分服务商会拒绝未知字段）。

## 日志

终端输出之外，所有事件完整写入项目根目录的 **`agent.log`**（Web 版和 CLI 版共用）：你的每次提问、每轮发给 LLM 的完整 payload、LLM 原始返回、工具调用、错误堆栈。`.env` 里可用 `LOG_FILE` 换路径、`LOG_LEVEL=INFO` 关掉 payload 全量记录。日志包含对话内容，别原样贴到公开场合；已加入 `.gitignore`。

## 常见问题

**报 `SSL: CERTIFICATE_VERIFY_FAILED ... self-signed certificate in certificate chain`？**

不是网络被劫持。python.org 官方安装版 Python 不读 macOS 钥匙串，自带的可信根证书库是空的，
信任库为空时 OpenSSL 会把证书链尾部的正常根证书（如 DigiCert Global Root G2）也误报成
"self-signed certificate in chain"。修复任选其一：

- 项目内：`.env` 加一行 `SSL_CERT_FILE=/etc/ssl/cert.pem`（项目会把它读入环境变量）
- 全局：运行 `/Applications/Python 3.12/Install Certificates.command`（需管理员权限）

验证方法：`curl -sI https://api.deepseek.com` 走的是系统证书库，若 curl 通而 Python 不通，基本就是这个问题。

**安全提醒**：Web 服务只监听 `127.0.0.1` 且无鉴权，仅供本机学习，不要暴露到公网。

## 动手练习（由易到难）

1. **加一个新工具**：在 `code_tools.py` 照抄三段式（实现 → schema → 注册表），比如 `run_python(code)` 直接执行一段代码，或 `tree(path)` 递归打印目录树。
2. **改系统提示词**：修改 `backend/agent.py` 的 `DEFAULT_SYSTEM_PROMPT`，比如要求"每次修改前必须先说明计划"，感受 prompt 对 agent 行为的塑造。
3. **审批门**：给 `run_bash` 加人工审批——Web 端先返回"待批准"状态，前端弹卡片，点同意后真执行（参考 Cline/Codex 的 approval 模型）。
4. **repo map**：用 `tree_sitter`（需要 pip 装包）抽取工作区所有函数/类签名，按引用频率排序，在 Agent 读文件前先给它"全库骨架"（Aider 的核心思路）。
5. **Docker 隔离**：把 `run_bash` 的执行从本机换成 `docker run` 容器内，体会真正的执行隔离。
6. **多会话支持**：现在全局只有一个 Agent，试着加 `session_id`，用字典管理多个会话的 history。
7. **过程事件实时推送的极限版**：把 SSE 升级成 WebSocket 双向通道，或加"停止生成"按钮（前端中断 fetch，后端感知断连后终止生成器）。

## 目录结构

```
agent_demo/
├── backend/
│   ├── app.py        # Web 服务：API 路由 + 静态托管（纯标准库）
│   ├── cli.py        # 命令行版入口
│   ├── agent.py      # ★ Agent 核心循环
│   ├── code_tools.py # ★ coding 工具集：工作区 + 读写/patch/grep/bash
│   ├── llm_client.py # LLM API 客户端（OpenAI 兼容）+ .env 读写
│   ├── tools.py      # 工具注册与统一执行器（合并通用工具和 coding 工具）
│   ├── logger.py     # 日志配置（写入项目根 agent.log）
│   └── ui.py         # 终端彩色输出（CLI 用）
├── frontend/
│   ├── index.html    # 页面结构：对话区 + 配置面板
│   ├── app.js        # 前端逻辑：fetch API、渲染、配置
│   └── style.css     # 样式
├── workspace/        # Agent 的工作区（自动生成，前端可切换到任意本地文件夹）
├── agent_data.db     # SQLite 数据库：任务、消息历史、供应商与模型配置
├── docs/             # 调研笔记（coding agent 选型报告）
├── agent.log         # 运行日志（自动生成）
├── .env              # 你的私密配置（不进 git）
├── .env.example      # 配置模板
└── .gitignore
```
