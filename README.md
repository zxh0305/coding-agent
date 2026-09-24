# 豆沙包coding — 本地 Coding Agent

一个跑在本机的 AI 编程助手：你在网页里用自然语言提任务，它在指定的本地文件夹里读代码、改文件、跑命令，并把执行过程实时展示给你看。

后端纯 Python 标准库（不用任何 Web 框架），前端原生 HTML/CSS/JS（无构建步骤），零第三方依赖。前后端分离，数据全部落在本机。

```
 浏览器 frontend/                        后端 backend/
┌──────────────────┐        ┌─────────────────────────┐      ┌──────────────┐
│ 任务列表（多会话） │        │  app.py (API)           │      │   LLM API    │
│ 执行过程时间线     │ ◀────▶ │  多会话 Agent + 工具循环  │◀────▶│ OpenAI 兼容  │
│ 上下文容量/模型/   │  SSE   │  agent.py · code_tools  │      │ / Anthropic  │
│ 工作区切换        │  JSON  │                         │      └──────────────┘
└──────────────────┘        └─────────────────────────┘
                                          │
                                   workspace/  ← Agent 的工作区（可切换）
```

---

## 一、它能做什么

给 agent 一个自然语言任务，它会自己拆解、调用工具、验证结果：

| 任务示例 | 触发的完整循环 |
|---|---|
| `工作区里的 demo.py 有个 bug，2+3 应该等于 5，修复并验证` | 读代码 → `apply_patch` 改 → `run_bash` 跑验证 |
| `在工作区新建一个猜数字小游戏，写完自己玩一轮验证` | 自建项目并自测 |
| `帮我看 workspace 里哪个文件最长` | `grep` / `list_dir` 类侦察任务 |

输入框还支持**附件**：点 📎 或直接选择文件，图片（≤4MB）作为视觉输入发给模型，文本/代码文件（≤5MB）存入会话附件区、模型用 `read_attachment` 工具按需分页读取；历史任务里也会回放。附件按会话隔离存放在 `data/attachments/<会话id>/`（在 `data/` 下，不进 git、不污染工作区），会话删除时一并清理。

**界面构成**：左侧任务列表（多会话）、中间执行过程实时时间线、底部工具栏（上下文容量 / 权限模式 / 工作区 / 文档 / 模型切换）、右侧文档栏（agent 生成的 Markdown 汇报与方案，与主区并列、可拖拽调宽、可收起）。

---

## 二、快速开始

```bash
python3 backend/app.py
# 浏览器打开 http://127.0.0.1:8000
# 登录后，在「模型配置」填 API Key（可顺便改 Base URL / 模型名）→ 保存 → 直接对话
```

也可以继续用命令行版（与网页共用同一份 `.env` 配置）：

```bash
python3 backend/cli.py                    # 交互式多轮对话
python3 backend/cli.py "37*89+100 等于多少"  # 单次提问
```

配置方式二选一：网页「模型配置」面板（自动写回 `.env`），或手动编辑 `.env`（模板见 `.env.example`）。

---

## 三、系统架构

### 3.1 分层总览

```
┌─ 前端 frontend/ ─────────────────────────────────────────────┐
│  index.html   页面结构                                        │
│  app.js       fetch 命令接口 / EventSource 常驻事件流 / 渲染    │
│  style.css    样式（含文档栏、时间线、缩略导航条）              │
└──────────────────────────────────────────────────────────────┘
        │ POST 命令（提交输入）        ▲ SSE 事件流（过程事件）
        ▼                             │
┌─ 后端 backend/ ──────────────────────────────────────────────┐
│  app.py         HTTP 路由 / 会话锁 / 后台回合线程 / 静态托管    │
│  events.py      每会话事件总线（seq + 环形缓冲 + 补发计划）      │
│  agent.py       ★ Agent 核心循环（run() 生成器）               │
│  tools.py       工具注册表 + 统一执行器（错误兜成信封）          │
│  code_tools.py  ★ coding 工具集（读写 / patch / grep / bash）   │
│  doc_tools.py   文档工具（写会话文档，供右侧面板查看）           │
│  git_tools.py   Git 只读查询（供前端提交记录浮窗）              │
│  llm_client.py  LLM 客户端（OpenAI 兼容 + Anthropic 双协议）    │
│  permissions.py 最小权限闸门（allow / deny / ask 三态）         │
│  memory.py      持久记忆（索引 / 正文分离 + 轮末自动提取）       │
│  system_prompt.py  系统提示词（人格 + 工作守则 + 工具纪律）      │
│  db.py          SQLite 存储层                                 │
│  logger.py      日志（按天切分 / 自动清理）                     │
│  ui.py          终端彩色输出（CLI 用）                         │
└──────────────────────────────────────────────────────────────┘
        │                             │
        ▼                             ▼
   data/agent_data.db            workspace/（Agent 的工作区）
```

### 3.2 一次任务的完整生命周期

1. **提交**：前端 `POST /api/sessions`（新任务）或 `POST /api/sessions/<id>/messages`（已有任务），**立即返回** `{session_id, nonce}`，不等待执行结果。
2. **排队**：回合在后台线程执行，同一会话一把锁——同一任务的多次提交**自然串行排队**，不同任务**并行**互不干扰。
3. **执行**：`agent.py` 的核心循环 `run()` 是一个**生成器**，边跑边产出事件（模型思考、工具调用、工具结果、回答分片、用量统计）。
4. **推送**：所有过程事件经 `events.py` 的每会话事件总线统一分配 `seq`、写入环形缓冲，再由常驻 SSE 连接 `GET /api/sessions/<id>/events` 推给前端。
5. **收尾**：回合结束发 `done` / `turn_end`；回答完整落盘 SQLite；视条件触发上下文压缩与记忆提取。

### 3.3 命令与事件解耦（关键设计）

**POST 只入队、立即返回；过程事件全部走常驻 SSE 通道**——这是整套实时体验的地基：

- 前端用一条 `EventSource` 常驻连接，浏览器断线会自动重连并携带 `Last-Event-ID`；前端另在 `localStorage` 记每会话最近 `seq`，刷新后用 `?since=` 补发，**接上正在进行的回合**。
- 后端环形缓冲（500 条）补发缺口，缺口过大则发 `resync` 让前端全量刷新；每 15 秒发 `: ping` 心跳防代理掐连接。
- 回答分片（delta）按消息 `mid` 归并进同一气泡，不产生"多条回答"的视觉噪音。

### 3.4 一轮工具调用期间，消息历史长什么样

问 `37*89+100 等于多少` 后，`agent.history` 依次变为：

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

① 模型不再输出文字，而是输出一段结构化的"调用请求"（`arguments` 是 **JSON 字符串**，不是 dict）。② 请求不会被自动执行——是本地 `execute_tool` 跑完，把结果作为 `role=tool` 消息塞回历史（`tool_call_id` 必须与请求配对）。③ 模型看到结果后产出最终回答，循环结束。

每轮请求都会把**从 system 开始的完整历史**发给 API——LLM 无状态，"记忆"完全靠客户端重发历史实现。唯一的例外是**上下文压缩**（见 5.6）。

---

## 四、工具系统

工具采用**三段式**：实现函数 → schema（给模型看的说明书）→ 注册表。`tools.py` 统一执行：把 schema 暴露给模型，执行时注入 `ToolContext`（工作区路径、本轮图片、看图后端），并把任何异常兜成 `{ok:false, error, hint}` 信封返回给模型，而不是让循环崩掉。

| 工具 | 作用 |
|---|---|
| `read_file` | 读取工作区内文件（带行号） |
| `write_file` | 新建文件或整文件覆盖 |
| `apply_patch` | 锚定原文的精确编辑（首选改代码方式） |
| `list_dir` | 列出目录一层内容 |
| `grep` | 正则搜索文件内容，返回文件 + 行号 + 原文 |
| `run_bash` | 执行 shell 命令（黑名单 + 超时） |
| `calculator` | 四则运算（ast 白名单，不用 `eval`） |
| `analyze_image` | 借支持视觉的模型"代为看图"，把描述返回给主模型 |
| `create_doc` | 生成 / 更新会话 Markdown 文档（右侧面板查看） |
| `todo_write` | 维护多步任务的待办清单（状态 pending / in_progress / done，前端渲染清单卡） |

**权限闸门**（`permissions.py`）在工具执行前介入，三态判定：

- `allow` 放行（模型无感）；
- `deny` 拒绝，原因以工具结果回填给模型（模型据此改道，绝不静默失败）；
- `ask` 暂停回合，经 SSE 弹确认卡片请用户决定。

判定次序 **deny > ask > allow**（最严者胜）。前端工具栏的三种模式（只读 / 确认 / 完全访问）映射到这套规则。规则来源两级：代码内置默认 + 用户规则（存 `settings` 表，按工作区隔离）。

> 安全坦白：这是「防误操作 + 强制人工确认」的闸门，**不是沙箱**。`run_bash` 拿到的是真实 shell，工作区只是 `cwd` 不是牢笼；真正要铁桶就上 Docker。

---

## 五、核心设计点

### 5.1 工具报错不抛异常

工具失败时把错误字符串作为**正常工具结果**返回给 LLM，模型看到 `{"error": ...}` 后能自己纠正参数重试。实测：`python` 不存在时模型会自己换 `python3`。

### 5.2 apply_patch 的"锚定原文"编辑协议

参照 Aider 的 SEARCH/REPLACE 思路：`search` 必须逐字符匹配且唯一。失败模式只有"找不到"和"多处匹配"两种，都能反馈给模型自纠。比整文件重写省 token，比 diff 格式抗幻觉。

### 5.3 工作区隔离与边界保护

- **越界保护**：路径 `resolve()` 消解 `../` 后校验必须仍在工作区内，`/etc/passwd`、`../../.env` 一律被拒。
- **按任务隔离**：每个任务可单独切换工作区（工具栏切换，或设为"新任务默认"）。解析链为「任务自选 → 用户默认 → `.env` 的 `WORKSPACE_DIR` / 项目 `workspace/`」。
- **ToolContext 注入**：工作区路径不进全局环境变量，而是随 `ToolContext` 注入到每次工具调用——切换某任务的工作区不影响其他正在跑的任务，并发会话也不会串图片数据。

### 5.4 防死循环：提醒而非砍停

- **收尾轮**：`max_rounds`（默认 40）跑满后不再"强制砍停"，而是注入一条合成 user 指令（`_synthetic` 标记：发给模型保留、落库跳过、不发事件），以 `tools=None` 再请求一轮，回合以模型自己的真实总结收场（`done(stopped_reason="max_rounds")`）。
- **指纹提醒**：同一工具 + 相同参数连续 3 次注入"换做法"提醒；轮数接近上限时注入"收敛"提醒。每回合总预算 3 条。
- 哲学：防失控靠**模式检测 + 提醒让模型自纠**，不靠计数砍停。

### 5.5 多协议适配层

每个供应商可配 `api_format`：`openai`（`/chat/completions`）或 `anthropic`（`/v1/messages`）。适配器把 Anthropic 的请求 / 响应 / SSE **双向转换**成 OpenAI 消息格式，对外接口完全一致，`agent.py` 与前端零改动。

已处理的差异点：system 提示为顶层字段、工具参数用 `input_schema`（对象）而非 `arguments`（字符串）、`tool_use` / `tool_result` 是 content 块且工具结果挂在 user 消息上、user / assistant 必须交替（相邻合并）、`max_tokens` 必填、SSE 事件为 `message_start` / `content_block_delta` / `input_json_delta` / `message_delta`。要接 OpenAI Responses 等新协议，在 `create_client` 工厂加一个分支即可。

### 5.6 上下文压缩（两套视图，一个真相）

一轮回答完全结束后，若估算的 prompt tokens 超过窗口 80%，就把"除首条用户消息外的中段历史"交给同一个 LLM 压成一条中文摘要（必保：任务目标 / 已完成改动 / 关键文件路径 / 未完成事项 / 用户偏好），保留最近 6 条不总结。

- **数据库和前端时间线永远保留完整历史**——压缩只在 `_messages_for_model()` 构建的**模型视图**里生效：插入一条 `role=compact` 的边界标记，构建视图时遇边界就用摘要替代之前段落；首条用户消息逐字保留，连续压缩只认最后一条边界。
- 两个暗坑都有防护：① 切点必须落在完整对话回合之间（否则服务端 400），`_compact_split` 会左移到合法位置；② 压缩后 token 校准系数作废，退回粗略系数等下一轮重校准。
- 前端只收到 `compacted` 事件，渲染"以上已压缩"分隔卡片（点开可查摘要原文）。失败自动跳过、下轮重试。

### 5.7 上下文容量估算

没有本地分词器，用"服务商返回的真实 `prompt_tokens` ÷ 上次请求总字符数"校准出每字符 token 系数，再按 系统提示词 / 工具定义 / 用户消息 / 助手回复 / 工具结果 的字符占比分摊——是估算值，但量级和占比可信。缓存命中率直接用服务商返回的 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`。

### 5.8 持久记忆

参照 ZCode 的记忆设计：每条记忆一个 md 文件（frontmatter + 正文一段话），索引文件 `MEMORY.md` 一行一条。注入时**只注入索引**，模型判断需要正文时自己用 `read_file` 打开——索引常驻 system 的开销可控，正文按需读取不占上下文。

轮末自动提取：后台 daemon 线程跑，每会话一把提取锁实现单飞（上次未完成则本次跳过）；全程静默，不发 SSE 事件、不写数据库，失败只进日志、下轮自然重试。

### 5.9 视觉能力与 analyze_image

管理面板里每个模型可勾选"视觉"（`provider_models.vision`）。主模型没勾视觉时：发给它的消息自动剥离图片、替换成文字提示（否则不支持视觉的服务商会直接报 400）；同时 `analyze_image` 工具借任意勾选了视觉的模型"代为看图"，把文字描述返回给主模型——**用工具补偿模型短板**。

### 5.10 流式链路（四层各有关卡）

| 层 | 关卡 |
|---|---|
| ① LLM | `stream:true` 时工具调用**分片**到达，须按 `index` 累积拼接 `arguments`；`stream_options: include_usage` 拿用量（不支持时自动降级重试）；`tool_choice` 必须与 `tools` 成对出现；瞬态错误（429 / 5xx / 连接失败）在请求发出前退避重试（优先 `Retry-After` 封顶 30s，否则 2s/4s），等待期间可被「停止」打断 |
| ② Agent | 核心循环重构为 `run()` 生成器，边跑边产出事件，usage 跨轮累计 |
| ③ 后端 | SSE 推送，刻意用 HTTP/1.0"关闭连接即结束"语义，免写 chunked 分块；`wbufsize=0` 保证每次 write 直达网络 |
| ④ 前端 | `EventSource` 常驻连接（自动重连 + `Last-Event-ID`）；`localStorage` 记每会话最近 seq，刷新后 `?since=` 补发；delta 按 `mid` 归并 |

### 5.11 其他

- **assistant 的工具调用消息必须原样回填历史**——否则出现"没有请求却冒出工具结果"的悬空消息，服务端直接 400。
- **`calculator` 不用 `eval`**——ast 白名单只允许四则运算，防止执行任意代码。
- **前端一律用 `textContent` 渲染**——不拼 `innerHTML`，天然防 XSS。
- **发给模型前剥离 `_` 前缀内部字段**——部分服务商会拒绝未知字段。

---

## 六、数据存储

全部落盘在 **`data/agent_data.db`**（SQLite 单文件，标准库 `sqlite3`）：

| 表 | 存什么 |
|---|---|
| `sessions` | 任务列表：标题、创建/更新时间、归属用户、各自的工作区 |
| `messages` | 消息历史：**稳定身份 `mid`（uuid4）+ 显示序 `ord` + 正文**。每轮只增量写入新消息；压缩后额外含 `role=compact` 边界标记（原文不删）。单条超 64KB 时正文外置到 `data/artifacts/<会话>/<mid>.json`，行内只留 head/tail 摘要 |
| `message_usage` | 消息级统计（prompt/completion/cached tokens + 完整 `_stats` JSON），与正文分离存储 |
| `providers` | 模型供应商：名称 / Base URL / API Key / 启用状态 / 默认上下文窗口 |
| `provider_models` | 供应商下的模型：模型名 / 上下文窗口 / 启用状态 / 是否视觉 |
| `settings` | 键值设置（全局默认模型 `active_model`、权限规则等） |

- **schema 升级**用 `PRAGMA user_version` + 有序迁移列表（`db.MIGRATIONS`），启动时只补执行未到达版本。
- **会话恢复按窗口加载**：压缩边界之前的消息不进内存，内存占用与当前窗口成正比。
- **显示序 `ord` 留 1024 间隔**、压缩边界取两侧中点，实现零重写；间隔耗尽时自动整会话重编号兜底，并推 `history_renumbered` 让前端重拉时间线。
- 首次启动把 `.env` 里的 LLM_* 配置导入为"默认"供应商；此后网页「管理模型」的改动写入数据库（"默认"供应商的 Base URL/Key 改动会同步回 `.env`，保证命令行版一致）。
- API Key 以明文存本机库中，接口回显一律打码；`data/` 整体已加入 `.gitignore`。

---

## 七、HTTP API

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/sessions` | POST | **提交输入并创建任务**：立即返回 `{session_id, nonce}`，回合后台串行执行 |
| `/api/sessions/<id>/messages` | POST | **提交输入**（任务已存在时用） |
| `/api/sessions/<id>/events` | GET | **常驻事件流（SSE）**：回合过程事件的唯一出口，带 `seq`；支持 `Last-Event-ID` / `?since=` 补发，缺口过大发 `resync` |
| `/api/sessions` | GET / DELETE | 任务列表；`?session_id=` 删除任务 |
| `/api/sessions/<id>/messages` | GET | 历史消息（分页回放，默认最近 100 条；`?before_ord=&limit=` 向上翻页） |
| `/api/sessions/<id>/artifact` | GET | `?path=` 读取外置归档消息原文（realpath 白名单校验） |
| `/api/sessions/<id>/docs` | GET | 会话文档列表；带 `?name=` 读单个 md 原文（仅 .md，realpath 白名单校验） |
| `/api/context` | GET | `?session_id=` 上下文容量（token 数 + 构成占比 + 缓存命中率；窗口分母按该任务的模型） |
| `/api/models` | GET | 可用模型列表（供工具栏切换；带 `?session_id=` 时 `active` 返回该任务的模型） |
| `/api/active-model` | POST | 切换模型：带 `session_id` 只改该任务；不带改**全局默认**（新任务的初始模型） |
| `/api/providers` | GET | 供应商列表（含模型，Key 打码） |
| `/api/providers/save` `/delete` | POST | 新建/更新供应商；删除（"默认"不可删） |
| `/api/providers/models/save` `/delete` | POST | 供应商下的模型增改/删除 |
| `/api/providers/test` | POST | **测试链接**：发一次真实 ping，返回延迟或错误 |
| `/api/config` | GET / POST | 读取 / 修改配置（POST 写回 `.env`，留空字段保持不变） |
| `/api/workspace` | GET / POST | 查看/切换工作区（按任务隔离） |
| `/api/fs/dirs` | GET | 列出某目录的子目录，供选文件夹弹窗逐级浏览 |
| `/api/tools` | GET | 已注册工具的 schema |
| `/api/reset` | POST | 清空服务端对话历史 |

**几个值得注意的后端设计**：配置变更后按需重建 Agent 但**保留对话历史**；同一任务回合按会话锁串行（并发提交自然排队，不同任务并行）；命令与事件解耦（见 3.3）；未配置 Key 时返回 400 加友好提示，而不是让服务崩掉。

---

## 八、日志

终端输出之外，所有事件完整写入 **`data/logs/agent.log`**（Web 版和 CLI 版共用）：每次提问、每轮发给 LLM 的完整 payload、LLM 原始返回、工具调用、错误堆栈。

- **按天切分**：每天零点归档为 `data/logs/agent.log.2026-09-19`，当天日志始终在 `agent.log`；
- **自动清理**：默认保留最近 14 天（`.env` 里 `LOG_KEEP_DAYS` 可调）；
- **可调项**：`LOG_DIR` 换文件夹、`LOG_LEVEL=INFO` 关掉 payload 全量记录。

实时追看：`tail -f data/logs/agent.log`。日志包含对话内容，别原样贴到公开场合。

---

## 九、安全边界（务必了解）

- 路径越界（`../..`）会被拦截；`sudo`、全盘删除等高危命令被黑名单拒绝；bash 有 30 秒超时。
- **但这是防护，不是沙箱**——前端可把工作区切到任意本地文件夹（含重要数据的目录请三思）。
- 重要数据先备份；无人值守场景请上 Docker 隔离。
- Web 服务只监听 `127.0.0.1`，仅供本机使用，**不要暴露到公网**。
- API Key 明文存于本机数据库，接口回显打码，`data/` 不进 git。

---

## 十、目录结构

```
coding-agent/
├── backend/          # 后端源码平铺，tests/ 与 manual/ 分类收纳
│   ├── app.py        # Web 服务：API 路由 + SSE 事件流 + 静态托管（纯标准库）
│   ├── cli.py        # 命令行版入口
│   ├── agent.py      # ★ Agent 核心循环
│   ├── code_tools.py # ★ coding 工具集：工作区 + 读写/patch/grep/bash
│   ├── doc_tools.py  # 文档工具：写会话 Markdown（右侧面板查看）
│   ├── git_tools.py  # Git 只读查询（前端提交记录浮窗）
│   ├── llm_client.py # LLM 客户端（OpenAI 兼容 + Anthropic）+ .env 读写
│   ├── tools.py      # 工具注册与统一执行器
│   ├── db.py         # SQLite 存储层（data/agent_data.db，超大正文外置）
│   ├── events.py     # 每会话事件总线（SSE 常驻事件流 + 断线补发）
│   ├── memory.py     # 持久记忆（<workspace>/.agent-memory/，索引/正文分离）
│   ├── system_prompt.py # ★ 系统提示词（末尾 import 拼接记忆契约）
│   ├── permissions.py   # 最小权限闸门（allow/deny/ask 三态规则）
│   ├── logger.py     # 日志配置（data/logs/，按天切分，自动清理）
│   ├── ui.py         # 终端彩色输出（CLI 用）
│   ├── tests/        # 单元测试（cd backend && python3 -m unittest discover -s tests -t .）
│   └── manual/       # 真机手测脚本 + 迁移演练
├── frontend/
│   ├── index.html    # 页面结构：任务列表 + 对话区 + 文档栏 + 各种浮窗
│   ├── app.js        # 前端逻辑：fetch API、EventSource、渲染、交互
│   └── style.css     # 样式
├── data/             # 全部运行时数据（不进 git）
│   ├── agent_data.db # SQLite 数据库：任务、消息历史、供应商与模型配置
│   ├── artifacts/    # 超大消息正文的外置 JSON
│   ├── logs/         # 运行日志（按天切分）
│   └── backups/      # 手工备份
├── workspace/        # Agent 的默认工作区（每个任务可单独切换）
├── docs/             # 调研笔记（coding agent 选型报告等）
├── share.sh          # 一键启停：后端 + Cloudflare 公网隧道
├── .env              # 你的私密配置（不进 git）
├── .env.example      # 配置模板
└── .gitignore
```

---

## 十一、常见问题

**报 `SSL: CERTIFICATE_VERIFY_FAILED ... self-signed certificate in certificate chain`？**

不是网络被劫持。python.org 官方安装版 Python 不读 macOS 钥匙串，自带的可信根证书库是空的；信任库为空时 OpenSSL 会把证书链尾部的正常根证书（如 DigiCert Global Root G2）也误报成 "self-signed certificate in chain"。修复任选其一：

- 项目内：`.env` 加一行 `SSL_CERT_FILE=/etc/ssl/cert.pem`（项目会把它读入环境变量）
- 全局：运行 `/Applications/Python 3.12/Install Certificates.command`（需管理员权限）

验证方法：`curl -sI https://api.deepseek.com` 走的是系统证书库，若 curl 通而 Python 不通，基本就是这个问题。

---

## 十二、开发

```bash
# 单元测试
cd backend && python3 -m unittest discover -s tests -t .

# 真机手测脚本（临时库，可重复跑）
cd backend/manual
```

要扩展本项目，常见的三个入手点：

- **加工具**：在 `code_tools.py` 照抄三段式（实现 → schema → 注册表）。
- **改 agent 行为**：改 `backend/system_prompt.py`（人格与工作守则集中于此）。
- **接新协议**：在 `llm_client.py` 的 `create_client` 工厂加一个适配器分支。
