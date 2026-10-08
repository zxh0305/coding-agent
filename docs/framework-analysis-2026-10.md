# Agent 框架全景分析：用框架 vs 自己写

> 信息日期：2026-10-08。版本号、发布日期、依赖数、CVE 编号均为当日实时抓取（PyPI/npm/GitHub API/NVD）。
> 针对本项目：零第三方依赖的纯标准库 Python 后端（21,729 行）+ 免构建原生 JS 前端（8,342 行）。
> 本文回答四个问题：①用框架的好处与不用框架的好处；②LangChain/LangGraph 到底装了什么、能省掉哪些工作量、代价是什么；③市面上主流框架的整体盘点；④厂商 harness（DeepSeek harness、ZCode 等）与主流 coding agent 为什么用/不用框架。

---

## 0. 结论摘要

1. **框架卖的不是"稳定"，是四样东西**：可恢复的持久状态、跨进程中断恢复（HITL）、多租户/分布式部署、共享可观测性。前两样本项目已用自己的方式实现了，后两样单用户本地场景用不上。
2. **框架会替换的是你修得最少的那一层**。近 200 次提交里 41 次 fix 中，前端 121 次、`app.py` 53 次、`db.py` 35 次触碰，而框架要接管的 `agent.py` 只有 33 次、`llm_client.py` 10 次。
3. **决定该不该上框架的是部署拓扑，不是模型质量**。多租户云产品上框架（Qodo 用 LangGraph 是真实案例），单用户本地 CLI 全部自研——9 家模型厂商、13 个主流 coding agent，无一例外。
4. **模型厂商的 harness 和 agent 框架是两条独立产品线**。OpenAI 同时有 Codex（Rust 自研）和 Agents SDK，Codex 不用 Agents SDK；阿里同时有 Qwen Code（自研）和 Qwen-Agent，Qwen Code 不用 Qwen-Agent。
5. **"harness" 不等于 "framework"**。harness 是产品（循环+工具+权限+状态+UI），framework 是造循环的库。DeepSeek 的 dsh 确实有自己的"框架"，但那是一个**通用插件框架**（Cordis），不是 agent 框架——这个区别是理解整件事的钥匙。
6. **本项目建议**：不整体迁移。真要借，只有两块值得：provider 层（收益低，你已是 OpenAI 兼容 + Anthropic 双协议）和可观测性（单用户用不上）。稳定性的真实根因是"进行中回合状态机没有服务端单一真相"，框架碰不到那里。

---

## 1. 通用分析：用框架的好处 vs 不用框架的好处

### 1.1 用框架，你真正买到什么

| 收益 | 具体是什么 | 什么时候真的值 |
|---|---|---|
| **持久化状态 + 可恢复执行** | 检查点（checkpoint）+ 待写恢复：进程崩了，从最后一个超步恢复，已完成的兄弟节点不重跑 | 长流程、无人值守、分布式 |
| **HITL 中断恢复** | 暂停点被持久化成检查点，人能隔几小时/重启后再批准 | 需要审批且不能占着连接 |
| **多租户/部署基础设施** | 线程/运行/定时任务/Webhook/鉴权/自动扩缩（多为托管服务提供） | 云产品、团队协作 |
| **共享可观测性** | 统一 trace 模型，团队在同一处看所有 agent 的执行 | 多人协作排障 |
| **provider 广度 + 格式归一** | 一处接 60+ 模型；把 Anthropic `thinking` / OpenAI reasoning 归一成同一内容块 | 要接很多模型 |
| **生态现成件** | 中间件、评估器、工具适配器、MCP 集成 | 想快速堆出 demo |
| **团队标准化** | 大家用同一套抽象，换人不用重学 | 团队规模 > 3 |
| **少写胶水** | 重试、限流、流式聚合、参数校验、结构化输出 | 从零开始的新项目 |

### 1.2 不用框架，你真正买到什么

| 收益 | 具体是什么 |
|---|---|
| **可调试性** | 出 bug 时栈是你自己的代码。这是使用者对框架类库最大的抱怨点——"抽象汤让生产环境排障变成噩梦" |
| **prompt 与响应可见** | 没有中间层改写你的消息。Anthropic 官方原话：框架"常常多出一层抽象，遮蔽了底层的 prompt 和响应，让它们更难调试" |
| **零依赖 + 零供应链风险** | 无传递依赖、无遥测、无 CVE 面。本项目现在 100% 标准库 |
| **状态与存储的真相权归你** | 你的库表结构就是真相，不需要"框架的检查点 + 你的会话日志"双写 |
| **无版本债** | 不用跟发版节奏。Pydantic AI 约每周 4 次、Strands/ADK 接近日更，LangChain 生态一年从 1.0 走到 1.6.7 |
| **启动快、体积小** | 对比：`langchain` 39 个包/47MB，ADK 依赖条目 262 个，CrewAI 拉进 chromadb+lancedb |
| **产品自由度** | 事件协议、UI、权限语义、上下文策略完全自定义，不受框架世界观的约束 |

### 1.3 一句话判据

**部署拓扑决定该不该上框架，模型质量不决定。**

- 单用户、本地、单二进制 → 优化启动速度、可审计、可魔改 → 自研，只借 provider 层。
- 多租户、云、团队 → 优化可恢复、公平性、成本控制、按租户可观测 → 上框架 + 可能需要更底层的持久化执行引擎。

Dapr/Diagrid 的 CTO 在 2026-02 的文章里直接指出：框架的检查点**不等于**生产级持久化（需要你自己当编排者、无自动故障检测与恢复），生产需要真正的持久化执行运行时。这从反面说明：**即使上了框架，重活还是你的**。

---

## 2. LangChain / LangGraph 到底装了什么

### 2.1 LangChain 1.x 的包分解（2026-10-08 实测）

| 包 | 版本 | 里面是什么 |
|---|---|---|
| `langchain` | 1.4.3 | **壳**。整棵树只有 13 个模块，公开导出只有两个名字：`create_agent` 和 `AgentState`。真正的负载是 19 个内置中间件 |
| `langchain-core` | 1.6.7 | **182 个 py 文件 / 约 70,326 行**。真正的抽象层 |
| `langchain-classic` | 1.0.8 | 遗留链的坟场：39 个链子包、17 种 memory、经典 agent 动物园 |
| `langchain-community` | 0.4.2 | **正在下线**（官方标注 sunset） |
| `langchain-openai` / `-anthropic` 等 | 独立版本号 | 每个 provider 一个包，各自 pin `langchain-core<2.0.0` |
| `langsmith` | 0.14.4 | trace SDK，是 `langchain-core` 的**硬依赖**（不配就闲置） |
| `langserve` | 0.3.3 | **2025-10-17 后无发版，已冻结** |
| `langchain-cli` | 0.0.37 | **2025-08-30 后无发版，已冻结**，被 `langgraph-cli` 取代 |
| `deepagents` | 0.7.23 | **预 1.0**。生态里最像"开箱即用的 coding agent harness"：文件系统后端 + 权限 + 大对象落盘 + 摘要 + 子代理 + 技能 + 记忆 |
| `langgraph` | 1.2.14 | 见下 |

**关键事实**：`langchain 1.x` 只有两个导出，且 `create_agent` 建在 LangGraph 上（`langgraph>=1.2.11,<1.3.0`）。所以 "LangChain 还是 LangGraph" 这个二选一在 1.x 已经不成立了——**LangChain 是 LangGraph 上的一层薄壳 + 中间件**。

### 2.2 `langchain-core` 里的抽象（按模块）

- **Runnable / LCEL**：`invoke/batch/stream/astream_events/pipe/with_retry/with_fallbacks` + `RunnableParallel/Each/Lambda/Branch/Passthrough/Assign`
- **消息与内容块归一**（值得单独说）：`messages/content.py` 定义统一内容块联合 `TextContentBlock`/`ToolCall`/`ToolCallChunk`/`ReasoningContentBlock`/`Image`/`Video`/`Audio`/`File`/`Citation`，并有 `messages/block_translators/{anthropic,openai,google_genai,bedrock,groq}.py` 把 Anthropic `thinking`、OpenAI reasoning 摘要归一成 `type:"reasoning"`。**这是最值得偷的一块**
- **`BaseChatModel` + `model_profile`**（按模型的能力档案）
- **工具**：`BaseTool`/`@tool`/schema 转换
- **输出解析**：json/xml/pydantic/list/string/openai_tools
- **prompt**：`PromptTemplate`/`ChatPromptTemplate`/`FewShot...`/`MessagesPlaceholder`
- **检索/文档/向量库/embedding**（本项目完全不需要）
- **回调与 trace**：`callbacks/` + `tracers/` + langsmith
- **缓存**：`BaseCache`/`InMemoryCache`（键粒度粗，按 prompt+llm_string）
- **限流**：`BaseRateLimiter`/`InMemoryRateLimiter`（令牌桶）
- **重试/降级**：`runnable/retry.py`（依赖 tenacity）、`runnable/fallbacks.py`
- **序列化**：`load/{dumps,dumpd,load,loads}` —— 也是 2025–2026 那批 CVE 的攻击面所在

### 2.3 LangGraph 的运行时概念（这才是重头）

- **图原语**：`StateGraph`（状态 schema 可用 TypedDict/dataclass/Pydantic）、节点、`add_edge`/`add_conditional_edges`、`START`/`END`、`recursion_limit`（默认 1000）
- **通道 + reducer（核心机制）**：每个状态键背后是一个 channel，channel 有 reducer `f(left,right)`。默认覆盖；`operator.add` 追加；`add_messages` 按消息 id 去重追加；也可自定义。实现类包括 `LastValue`/`BinaryOperatorAggregate`/`Topic`/`DeltaChannel`（beta，只存增量以缩小检查点）。**并行分支在同一超步写同一 channel，reducer 就是合并契约**
- **Pregel 超步模型**：节点收到消息激活→执行→投票停机；`Send` 做 map-reduce 扇出；子图带自己的检查点命名空间；另有 `@entrypoint`/`@task` 函数式 API
- **检查点存什么**：`channel_values` + `channel_versions` + `versions_seen`（决定下一步跑什么）+ `pending_sends`。分两张表：检查点表（每超步一行）+ **写入表**（每节点输出一行）。写入表就是"待写恢复"的机制——同一超步里某个节点失败，成功的节点写入已落库，恢复时**不重跑**，只重试失败节点
- **配置键**：`thread_id`（必填）、`checkpoint_id`（回到某个具体快照）、`checkpoint_ns`（子图）
- **可用 saver**：`InMemorySaver`、`langgraph-checkpoint-sqlite`（`SqliteSaver`/`AsyncSqliteSaver`）、`postgres`、`mongodb`、cosmos。**Redis 不是官方检查点后端**
- **序列化**：`JsonPlusSerializer`（ormsgpack+JSON，带类型化 msgpack 扩展码）。`pickle_fallback=True` 是危险开关
- **`interrupt()` / `Command(resume=)`**：HITL 的核心。**已知陷阱（官方文档明写）**：恢复时"整个节点从头重新执行，不是从 `interrupt()` 那一行继续"，所以中断点之前的副作用必须幂等；不要在节点里 `while True:` + `interrupt()`（第 N 次恢复会重放 N 次迭代）；子图中断时父节点也从头跑
- **持久化模式**：`durability="sync"`（每步前同步落库）/`"async"`（默认，异步落库，崩溃可能丢一个检查点）/`"exit"`（只在退出时落库，无中途崩溃恢复）
- **流**：`stream_mode` ∈ `values`/`updates`/`messages`/`custom`/`checkpoints`/`tasks`/`debug`；`version="v2"` 返回类型化 `StreamPart`；`stream_events(version="v3")` 提供可并发消费的类型化投影（`stream.messages`/`stream.tool_calls`/`stream.interrupts`…）。`astream_events` 未废弃但已不是推荐面
- **时间旅行**：`get_state_history` 列快照；`invoke(None, prior_config)` **重放**（会重新执行节点、重新发 LLM 调用）；`update_state(..., as_node=)` **分叉**出新分支
- **OSS vs 托管的分界**：`langgraph` + saver + 你自己的 FastAPI 就是完整运行时。但**定时任务、后台运行、Webhook、双发消息（double-texting）、Studio、鉴权、TTL、自动扩缩都在 LangSmith 部署侧**。官方原文：双发"是 LangSmith 部署的功能，**在 LangGraph 开源框架中不可用**"

### 2.4 逐项对照：它会替换你项目里的什么

| 你已手写的东西 | 判定 | 说明 |
|---|---|---|
| 回合循环 + 轮次预算 | **部分** | `create_agent` 给循环和调用次数上限，但"预算"作为你自己定义的成本/轮次策略不是一等概念，要改写成中间件 |
| 裸流式客户端（SSE 解析、工具调用分片拼装、reasoning 透传、usage/缓存命中计费） | **替换（最大收益）** | `langchain-openai`/`-anthropic` 全包，`AIMessageChunk` 合并分片，`usage_metadata`/`input_token_details.cache_read` 归一，`message.reasoning` 暴露推理增量。**这是最干净的删除候选** |
| 工具注册表 + OpenAI schema + ToolContext 注入 | **替换（大）** | `@tool` 生成 schema，`ToolRuntime`/`InjectedState` 是 DI 缝，参数校验自动 |
| 工具结果截断 + 落盘外置 | **部分** | 核心框架**没有**通用的"截断并外置成句柄"原语；`deepagents` 里有（`FilesystemMiddleware` + `_blob_offload` + `_overflow_clip`） |
| SSE 事件总线（seq/环形缓冲/补发） | **不替换（应用协议）** | 框架给流式投影和托管侧的线程/运行 SSE 协议，但**不给**"带序号、可重连补发的可重放事件日志"——这是你的应用协议 |
| SQLite 会话存储（ord 插入 + 指纹增量落盘） | **部分** | 检查点给有序状态历史，但 schema 是它的（thread_id/checkpoint_ns）；"指纹增量落盘"没有对应物（最接近 `DeltaChannel`）。现实是**双写** |
| 分层上下文压缩 | **部分** | `SummarizationMiddleware`/`ContextEditingMiddleware`/`trim_messages` 给机制，"分层"是你自己组合，**"两视图一个真相"没有框架对应物** |
| 权限闸门（跨 HTTP 线程 ask/wait/resume） | **替换最难的那半** | `interrupt()` + 检查点让暂停能活过进程死亡，是真收益。**但**恢复要重跑节点 → 你的闸门需重构（副作用挪到中断之后的节点） |
| 子代理扇出 | **替换/部分** | `Send`/子图/`SubagentMiddleware`；"隔离上下文 + 回流父级"是 deepagents 的地盘 |
| Markdown 记忆系统 | **部分/无** | `Store` 给命名空间 KV（可选语义检索），deepagents 给文件系统记忆，但**都不实现你的 md 格式与选取策略** |

**净结论**：真正删得掉的是 ①provider 流式客户端及其归一 ②工具 schema/调度/校验/DI ③持久化 HITL ④检查点化会话状态。**删不掉**的是 ①SSE 事件协议 ②md 记忆 ③你的截断外置策略 ④你的预算策略——而且它把工作**迁移**到中间件/图重构里，不是消灭。

### 2.5 代价（实测）

| 项 | 数值 |
|---|---|
| `pip install langchain` | **39 个包 / 47 MB**（不含任何 provider） |
| `pip install langgraph` | 38 个包 / 46 MB |
| `pip install langchain-core` | 33 个包 / 40 MB |
| 硬依赖 | `pydantic`、`langsmith`、`tenacity`、`ormsgpack`+`orjson`+`zstandard`、`httpx` **和** `httpx2`、`uuid-utils`、`xxhash`、`websockets`、`langchain-protocol` |
| 冷启动 | `langgraph.graph` 导入约 214ms |
| 发版节奏 | langchain-core 一年从 1.0 → 1.6.7；langgraph 1.0(2025-10) → 1.2(2026-05) → 1.2.14(2026-10) |
| LTS 政策 | **是业内最好的**：semver，破坏性变更只在 major；1.0 为 LTS，2.0 后进维护 ≥1 年；0.3 维护到 2026-12 |

**安全检查（2025–2026，共 13 条 advisory）**：

`langchain-core` 9 条，其中：
- **CVE-2025-68664（严重 9.3，2025-12-23）**：序列化注入——`dumps()` 不转义带 `lc` 键的 dict，`load()` 把攻击者数据当 LangChain 对象处理（窃取密钥）
- CVE-2026-44843（高，2026-05-08）：`load()` 白名单过宽导致不安全反序列化
- CVE-2025-65106（高，2025-11-20）：prompt 模板注入，可达 `__globals__`
- CVE-2026-34070（高，2026-03-27）：`load_prompt` 路径穿越

`langgraph`/`langgraph-checkpoint` 4 条，**全部落在检查点序列化路径**：
- CVE-2025-64439（高，2025-11-05）：`JsonPlusSerializer` json 模式 **RCE**
- CVE-2026-27794（中，2026-02-25）：`BaseCache` 的 `pickle_fallback=True` → `pickle.loads` RCE
- CVE-2026-28277（中，2026-03-05）：加载检查点时不安全 msgpack 反序列化
- CVE-2026-48775（中，2026-06-25）：`JsonPlusSerializer` 再一条不安全反序列化

**这是本项目当前不存在的攻击面**：你的 SQLite 存的是自己的表结构，而 `JsonPlusSerializer` 检查点本质是"重建任意 Python 对象"，**检查点库等同代码执行权限**。对一个本地会跑 shell 的 agent，这笔账要算清楚。

---

## 3. 其他主流框架盘点

### 3.1 各自装了什么（组件清单）

**Pydantic AI 2.54.0**（2.0 于 2026-06-23，约每周 4 次发版，**本组最高churn**）
- `Agent[Deps, Output]`、`RunContext`（类型化依赖注入）、`output_type` 结构化输出
- `@agent.tool`/`tool_plain`/Toolsets、`Capabilities`（把 工具+指令+钩子+模型设置 打包）
- **`agent.iter()`** 暴露底层 `pydantic-graph` 的节点迭代（`UserPromptNode`/`ModelRequestNode`/`CallToolsNode`/`End`）——**这是"保留自己循环"的官方通道**
- `message_history` 一等公民（`new_messages()`/`all_messages()`，`ModelMessagesTypeAdapter` 可 JSON 化）。**没有内置 session store**，官方原话："这些字节存在哪里由你的应用决定"
- 延迟工具/审批：`requires_approval=True`、`DeferredToolRequests`/`DeferredToolResults`（可按 tool_call_id 批准/拒绝/改参）
- 持久化执行：Temporal/DBOS/Prefect/Restate/AWS Lambda/Kitaru/Airflow/Absurd 八种引擎（每个模型调用与工具调用各成一个持久单元），**一个 agent 只能挂一个引擎**
- 依赖：`pydantic-ai-slim` 17 个包；**无遥测**（不配 logfire 就什么都不发）
- 坑：找不到成文的弃用政策；harness 是 0.x；站点域名一年内搬过一次

**OpenAI Agents SDK 0.23.1**（**发布 16 个月后仍是 0.x**）
- `Agent`/`Runner.run|run_sync|run_streamed`/`RunState`/`RunConfig`；`max_turns`
- Sessions 多后端：SQLite（文件版持久）/SQLAlchemy/Redis/MongoDB/Dapr/OpenAI 服务端会话/加密包装
- Handoffs、agents-as-tools、guardrails（**输入护栏默认并行跑** → "被取消前可能已经烧了 token 并执行了工具"）
- HITL：`needs_approval` + `RunResult.interruptions` + `RunState.to_string()` 序列化恢复
- 钩子：`RunHooks`/`AgentHooks`、`call_model_input_filter`
- **供应商中立性是硬伤（官方文档自述）**：默认走 Responses API（很多 provider 不支持 → 404）；Chat Completions 模式**静默丢弃** `previous_response_id`/`conversation_id`/prompt 字段；LiteLLM 与 any-llm 集成标注 **"best-effort beta"**
- **tracing 默认开启并上传到 OpenAI 服务器**（`trace_include_sensitive_data=True` 默认），没 OpenAI key 会 401。必须 `OPENAI_AGENTS_DISABLE_TRACING=1`
- 发版：约每周~双周；**无弃用政策**；minor 版本带破坏性变更（0.20 改默认模型、0.22 改 provider 配置并迁移 MCP 依赖）

**Google ADK 2.11.0**（2.0 于 2026-05-19）
- `LlmAgent`；确定性工作流 agent：`SequentialAgent`/`ParallelAgent`/`LoopAgent`（"不咨询模型就决定执行顺序"）；ADK 2.0 新增 Graph Workflows
- `Runner`+`App`；`Session`/`State`/`Memory`/`Artifacts` + `SessionService`/`MemoryService`/`ArtifactService`（InMemory/Database/**VertexAi** 三种后端）
- 生命周期回调 before/after agent/model/tool；HITL `require_confirmation=True`（标注 **Experimental**）
- **恢复**：`ResumabilityConfig(is_resumable=True)`。官方限制原文：**"自定义 agent 默认不支持恢复"**；工具"至少执行一次，恢复时可能执行多次"；Web UI/CLI 恢复"当前不支持"
- 依赖：**23 个必装、含 extras 共 262 条目（本组最重）**；三个托管部署目标（Agent Engine/Cloud Run/GKE）**全部需要 GCP**

**AWS Strands 1.58.1**（仓库已改名/重构为 `harness-sdk` 单仓；Python 近每日发版）
- `Agent`（含 hooks、interventions、上下文管理）、模型 provider + **Model Router**、工具装饰器/MCP/executors
- **sessions & snapshots**、hooks、多 agent（Graph/Swarm/Workflow/Agents-as-Tools/A2A）、**backgroundTasks**、interrupt/resume
- 依赖：13 个必装，**含 boto3+botocore**（即使不碰 Bedrock 也装）+ OTel

**Microsoft Agent Framework**（Python 1.20.0，2026-10-02）
- 明确是 **AutoGen + Semantic Kernel 的合并继任者**（同一批人做，带双向迁移指南）
- 四块：Agents、**Harness Agent**（"电池全含：规划与待办、上下文压缩、文件访问与记忆、不再询问式工具批准、可观测性"）、Workflows（函数式+图）、Integrations
- **Python 侧仍是弱项**：发布列表里大量 `[BREAKING - experimental/beta]`；.NET 是一等公民

**Mastra 1.75.0**（TS，@mastra/core，约每周多次）
- Agent/Workflows（`createStep`/`createWorkflow` + **suspend/resume** + `restart()`/`listActiveWorkflowRuns()`）/Memory（`resource`+`thread`）/evals/Studio/observability
- 依赖 30 个，**含 `posthog-node`**；**坐在 Vercel AI SDK 上**（`@ai-sdk/provider-v5`/`-v6`）
- 只对 TS 有意义；Python 项目不看

**smolagents 1.26.0**（**事实上停更**：2026-05-29 后无发版，876 个 open issue）
- `CodeAgent`（写 Python 片段当动作）vs `ToolCallingAgent`；执行器 `LocalPythonExecutor`/E2B/Modal/Docker
- 官方安全告警原文：**"LocalPythonExecutor 不是安全沙箱……绝不能用它跑不可信代码"**
- **无持久化、无恢复、无 HITL、无 session**

**DSPy 3.4.0**（**不是 agent 运行时**）
- Signatures/Modules（`Predict`/`ChainOfThought`/`ReAct`/`Refine`/`BestOfN`…）/Optimizers（`GEPA`/`MIPROv2`/`BootstrapFinetune`/`SIMBA`…）
- 它**编译优化 prompt 程序**，没有 session、没有事件契约、没有工具审批。只能离线用来产出优化后的指令再塞进自己的循环
- 依赖 14 个（含 litellm）；弃用故事反而比多数框架清楚（"3.4 是过渡版，3.5 是迁移截止"）

**Haystack 3.3.0**
- `Pipeline`/`Component`（类型化 socket 校验）+ YAML 序列化；**`Agent` 是一个 Pipeline 组件**（可嵌进 Pipeline）
- Agent 参数：`tools`（含 `AgentTool`/`MCPTool`/`Toolset`）、`exit_conditions`、`max_agent_steps`（默认 100）、**`streaming_callback`**（逐 token）、`state_schema`；HITL 走 `before_tool` 钩子
- **无持久化执行、无恢复、无后台运行器**；依赖 19 个**含 posthog（遥测）**

**LlamaIndex Workflows 2.25.0**（**整个清单里最轻的包**）
- `@step` + 用户自定义 `Event`/`StartEvent`/`StopEvent` + `Context.store` + `ctx.send_event`/`collect_events` + `handler.stream_events()` + `Resource(...)`（不进序列化状态的依赖）
- **可持久化、可恢复**（"Writing Durable Workflows" 检查点 + 重启恢复）、HITL 用有状态步骤实现
- **只有 3 个必装依赖**（`llama-index-instrumentation`/`pydantic`/`typing-extensions`），无云、无遥测
- **但它没有 agent 循环、没有模型、没有工具抽象**——纯步骤/事件引擎，你得自己写循环体

**CrewAI 1.15.25**（`crewai`/`crewai-core`/`crewai-cli` 三包同版本）
- Agents（角色/目标/背景故事）+ Tasks + 流程（顺序/层级/混合）+ **Crews**（自主分工，烧 token）+ **Flows**（start/listen/router + 状态 + **可持久化 + 可恢复**）
- 依赖 31 个，**含 chromadb + lancedb + pdfplumber + openpyxl + tokenizers**（本组最重之一）；同一作者自己的"Flows 才是可持久化的那一半"

**持久化执行层（不是 agent 框架）**
- **Temporal 1.34.0**（SDK 只 5 个依赖，成本全在基础设施）：工作流/活动 + **确定性重放**（"从事件历史重放，不是内存快照"）+ 信号/查询/定时器。重放规则禁止工作流内直接调 `Date.now()`/随机数/网络。开发单个二进制，生产要 server + 数据库
- **DBOS 3.2.0**（12 个依赖）：`@DBOS.workflow()`/`step()`，步骤检查点存 **Postgres**，"程序失败重启后自动从最后一个完成的步骤恢复"，**无需额外基础设施**——但**要 Postgres，不是 SQLite**
- **Restate**（npm SDK 1 个依赖）：Basic Service / **Virtual Object**（按 key 单写者）/ **Workflow**（按 ID 恰好一次）；`ctx.run` 持久步骤，状态永久保留。天然的"按会话串行化"拟合，代价是前面加一个运行时
- **Prefect 3.8.8**：**57 个依赖**，是编排不是重放（任务级缓存/状态），需要 Cloud 或自建 server。**对"恢复半途 agent 回合"拟合最差**

### 3.2 能力矩阵

Y=一等公民，P=部分或有显著前提，N=无。备注写的是关键前提，不是功能描述。

| 框架 | 循环 | token 流 | 持久状态 | 恢复 | HITL | 子代理 | 后台 | 追踪 | 关键前提 |
|---|---|---|---|---|---|---|---|---|---|
| Pydantic AI | Y | Y | P | P（引擎可 Y） | Y | Y(harness) | P | Y | 无内置 store，状态化全在附加包 |
| OpenAI Agents SDK | Y | Y | Y | Y | Y | P | N | Y | **默认上传追踪到 OpenAI**；仍 0.x；非 OpenAI 丢字段 |
| Google ADK | Y | Y | P | Y（受限） | Y(实验) | Y | Y | Y | 恢复受 session 后端限制；托管=GCP |
| AWS Strands | Y | Y | Y | Y | Y | Y | Y | Y | boto3 必装；仓库改名；近每日发版 |
| MS Agent Framework | Y | Y | Y | Y | Y | Y | P | Y | Python 仍有 beta/实验性破坏模块 |
| Mastra（TS） | Y | Y | Y | Y | Y | Y | P | Y | 捆绑 posthog-node；近每周破坏性变更 |
| smolagents | Y | P | N | N | N | P | N | P | 本地执行器明确不是沙箱；4.5 个月无发版 |
| DSPy | N | N | N | N | N | N | N | P | 不是运行时，只是 prompt 优化器 |
| Haystack | Y | Y | P | N | Y | Y | N | P | 捆绑 posthog；无持久化执行 |
| LlamaIndex Workflows | N | N | Y | Y | Y | N | N | P | 没有循环/模型/工具，纯步骤引擎（仅 3 依赖） |
| CrewAI（Crews） | Y | P | P | P | Y | Y | P | Y | 31 依赖含 chromadb+lancedb+pdf 栈 |
| Temporal/DBOS/Restate | N | N | Y | **Y（最强）** | Y | N | Y | P | 确定性税 + 运行时/数据库运维 |

### 3.3 能不能"只借一块"

| 框架 | 可窄接？ | 借哪块 / 会被迫接受什么 |
|---|---|---|
| **Pydantic AI** | **可以，本组最佳** | 借工具层 + `RunContext` 依赖 + 延迟工具审批协议；`agent.iter()` 让你保留自己的循环与事件契约。代价：接受 Pydantic 的消息模型与类型系统 |
| **LlamaIndex Workflows** | **可以，最干净的"借引擎"** | 3 个依赖的步骤/事件 DAG 引擎 + 检查点恢复，对模型/工具/存储零主张 |
| **OpenAI Agents SDK** | 部分 | Sessions 后端与 `RunState`/中断可复用，但 Run* API 想接管循环，且默认回传追踪 |
| **AWS Strands** | 部分 | hooks + session/snapshot 可抽，但价值（Graph/Swarm/interrupt）依赖它的 Agent 循环；boto3 常在 |
| **MS Agent Framework** | 部分 | 中间件/上下文 provider/session 可组合，但 Python 面还在动 |
| **Haystack** | 部分 | `streaming_callback` 与类型化 socket 好用；Agent 状态不可持久化，且捆 posthog |
| **Google ADK / CrewAI / smolagents** | **不行** | Runner/Session/Event 或 Crew 就是循环与状态 schema；依赖过重或已停更 |
| **DSPy** | 可以（但它不是运行时） | 只离线借优化器 |
| **Temporal/DBOS/Restate** | 可以，正交设计 | 只拥有持久化，不拥有 agent。代价是确定性税；单机只有 DBOS/Restate 现实 |

---

## 4. 这个项目为什么应该自己写

### 4.1 数据（本仓库实测）

- 规模：后端 21,729 行 Python（56 个文件），前端 8,342 行 JS 无构建
- 测试：9,106 行 Python 测试 + 3 个零依赖 Node 测试，200+ 用例
- 提交结构（近 200 条）：**41 次 fix : 34 次 feat**，另 7 refactor / 6 ui / 2 perf
- **fix 触碰文件分布**：前端 `app.js` 121、`style.css` 78、`app.py` 53、`index.html` 43、`db.py` 35、**`agent.py` 33、`llm_client.py` 10**

### 4.2 推论

框架要接管的正是 `agent.py`（循环）+ `llm_client.py`（协议）——**修得最少的两块**（33 + 10 次）。而真正在漏的地方（前端渲染、`app.py` 编排、`db.py` 存储）**没有任何框架会碰**。

更具体：最大的 bug 簇是**"切回进行中回合"**，从早期到 HEAD 一直在修（`0253ddd` 就是 HEAD：切回进行中回合出现双思考窗）。这一簇（`ebf8d22`/`7d5e2ff`/`702a9ef`/`63ee231`/`0ae0d3b`/`95a0572`/`73d4047`/`bdb3f98`/`376215c`/`f19450d`…）的根因是同一个：**前端靠客户端旗标猜"这个回合现在处于什么状态"**，而不是把状态当服务端一等数据。每加一个旗标组合就漏一个。LangGraph/Pydantic AI/Agents SDK 都不管浏览器里怎么把事件流**幂等重绘**成 DOM。

### 4.3 会被框架拿走的资产

- `docs/protocol.md` 的 `seq`/`mid`/`nonce`/`user_mid` 四重身份 + 环形缓冲 + 补发/resync
- "两视图一个真相"：DB 是唯一真相，压缩只重写模型视图
- `db.py` 的 ord 中点插入不重编号、消息指纹增量落盘、巨型消息 artifact 外置
- 权限闸门在回合线程判定、跨 HTTP 线程暂停恢复、ask 键组整组生效
- 分层压缩 + 切点必须落在完整回合之间 + 压缩后文件重注入
- CJK 感知的 token 估算与按真实 `prompt_tokens` 校准的系数

上 LangGraph = 把 `Agent.history` 的真相权交给它的检查点（自带序列化格式），DB 要么双写要么废弃，事件契约要写桥接层。**付了框架成本，最难的"UI 与持久事件日志对账"还是自己写**，且迁移过程本身引入新 bug。同时新增"检查点反序列化"这类 CVE 面（§2.5）。

### 4.4 框架唯一真正的诱惑，以及为什么不成立

最诚实的反方论点：**LangGraph 的检查点 + `interrupt()` 恰好是"半途回合恢复"的一等公民**，这确实是本项目的痛点。

为什么不成立：

1. 本项目的恢复问题不是"恢复一次图计算"，而是**"把 UI 与一份持久事件日志对账"**，且带有两套身份体系。LangGraph 的检查点状态要**再桥接**成你的 SSE 契约——桥接层就是新 bug 来源。
2. 它的检查点**取代**你的 DB 成为真相源，而你的 DB 已经在正确工作（有 9,106 行测试覆盖）。
3. `interrupt()` 恢复要**重跑节点**（官方明写），你的权限闸门需要为幂等而重构——这是重构成本，不是删除成本。
4. 单用户本地场景**不需要**分布式持久化执行；而框架的检查点在真正的故障恢复上被持久化执行厂商公开批评为"不够"。

### 4.5 建议的替代路径（真正治稳定性）

1. **回合状态成为服务端一等数据**（落库 `idle/running/finished` + 当前段 mid），前端只渲染服务端给的状态，去掉客户端旗标推断。双卡/双窗/秒数不停走是同一个 bug 的三个表现。
2. **前端渲染改为 `(持久历史 + 事件流) → DOM` 的纯函数**，保证幂等重绘，替掉现在分散在 `blocks.js`/`render_blocks.js`/`app.js` 的局部补丁（`ensureTrace`、`data-livetrace` 标记、`subRunning` 之类）。
3. **给该场景建回放测试**（复用已有的 `test_replay_eval.py` 剧本回放与 `event_contract.test.mjs`），把这一簇钉住。

---

## 5. 厂商 harness 与主流 coding agent：为什么用/不用框架

### 5.1 先厘清概念：harness ≠ framework

- **harness（产品）**：循环 + 工具 + 权限 + 状态 + UI + 分发形态。用户直接用的东西。
- **framework（库）**：用来搭建循环的抽象层。被 harness 引用，用户不直接感知。

一个 harness **可以**引用一个通用插件框架而不引用 agent 框架——DeepSeek 就是这样，这是理解全盘的关键。

### 5.2 模型厂商的 harness：9 家 9 个自研循环

| 厂商 | harness 仓库 | 语言 | 许可 | 循环 | 用第三方 agent 框架？ |
|---|---|---|---|---|---|
| **DeepSeek** | `deepseek-ai/deepseek-harness`（dsh） | TS | MIT | 自研 `core/agent-loop`，turn/step 事件模型 | **否**（自研插件框架 Cordis） |
| Anthropic | `anthropics/claude-code` + Claude Agent SDK | TS/Python | 产品闭源；SDK MIT | 自持 harness；SDK 是驱动 CLI 二进制的薄客户端 | **否** |
| OpenAI | `openai/codex`（`codex-rs`） | **Rust** | Apache-2.0 | 自研 Rust 循环 | **否**（另有 Agents SDK，Codex 不用） |
| Moonshot | `MoonshotAI/kimi-code` | TS | MIT | 自研，单二进制，ACP | **否** |
| 智谱 | `zai-org/ZCode`（`apps/zcode-cli`） | TS | Apache-2.0 | 自研 CLI/运行时 | **否**（只把 Vercel AI SDK 当 provider 层） |
| MiniMax | `MiniMax-AI/minimax-code` | TS | MIT | 自研 TUI/ACP | **否** |
| 阿里 | `QwenLM/qwen-code` | TS | Apache-2.0 | Gemini CLI 分叉后独立 | **否**（另有 `Qwen-Agent` 框架，qwen-code 不用） |
| Google | `google-gemini/gemini-cli` | TS | Apache-2.0 | 自研 Ink 循环 | **否**（另有 LangGraph 示例，非产品） |
| xAI | `xai-org/grok-build` | **Rust** | Apache-2.0 | 自研 `xai-grok-*` crates | **否** |

**DeepSeek harness 细节**（这是"harness 可以自带框架但不是 agent 框架"的最佳样本）：
- `deepseek-ai/deepseek-harness`，TS，MIT，创建于 **2026-08-13**，pnpm 单仓，产品自述 **"developer preview"** 且明写 "THERE WILL BE COMPATIBILITY-BREAKING CHANGES"
- 设计哲学 **"Everything is a Plugin"**，建在**自带的通用插件框架 Cordis** 上（`cordiverse/cordis`），并把该范式写成论文《A Programming Paradigm for Spatiotemporal Composability》（arXiv 2608.25512）
- **其 `pnpm-lock.yaml`（757KB）grep `langchain|langgraph|openai-agents|pydantic-ai|crewai|autogen|litellm|smolagents|@ai-sdk|mastra|llamaindex|dify` 全部零命中**；直接依赖是 `@anthropic-ai/sdk`、`@modelcontextprotocol/sdk`、`zod`、`@opentelemetry/*`、`undici`、`@vscode/ripgrep`
- 术语：一个 **turn** = "零或多个 step"；一个 **step** = "一次模型请求 + 它调用的工具"。事件流 `turn/start → … → step/start → agent/request → stream → tool/call → … → turn/end`；扩展点是 Cordis 的类型化事件（`emit`/`waterfall`/`parallel`/`serial`/`bail`），注册是可逆副作用、插件卸载即回滚——"没有需要打补丁的特权内核"
- 工具面极大：`bash`/`pwsh`/持久 shell(PTY)、`read`/`edit`/`write`/`read_image`、`glob`/`grep`（自带 ripgrep）、六个 `terminal_*`、`str_replace_editor`、`subagent`/`subagent_fork`/`interrupt_agent`/`list_agents`/`send_message`、`workflow`、`ralph`、`skill`、`todo_write`、`web_fetch`/`web_search`、`present`、`plugin_manager`、MCP 资源、实验性 `stagehand_*` 浏览器、`lsp`、`job_*`、`schedule_*`、`goal_*`、`session_*_query`、plan mode、agent-teams
- 分发形态：npm `npx @deepseek-ai/dsh web`（Web UI :3080）、签名 Electron 桌面版、headless runner、**SDK JSON-RPC**、**ACP**，加 Python/TS SDK；profile：`web`/`headless`/`sdk`/`sdk-minimal`/`acp`
- 生态证据：GitHub topic `dsh-plugin` 报告 **18,062 个仓库**（2026-10-08）

**ZCode 细节**（本文作者在本机直接取证）：
- `zai-org/ZCode`，TS 单仓，Apache-2.0，v3.14.3；**agent CLI 与运行时源码就在仓内 `apps/zcode-cli/`**
- 它**确实依赖一个库**：`pnpm.patchedDependencies` 里 pin 了 `@ai-sdk/openai-compatible@2.0.60` 与 `@ai-sdk/anthropic@3.0.81`（Vercel AI SDK）——**这是 provider/流式层，不是 agent 循环框架**
- 本机取证（`/Applications/ZCode.app`）：
  - `app.asar`（327MB）中 `langgraph`/`pydantic_ai`/`@openai/agents`/`crewai`/`mastra` **全部 0 命中**；`@ai-sdk/` **955 命中**、`modelcontextprotocol` 90 命中
  - `langchain` 仅 6 命中，**原文是 Vercel AI SDK 自己的迁移提示**：``#### LangChain Adapter Moved to `@ai-sdk/langchain` `` ——即 AI SDK 把 `@ai-sdk/langchain` 适配器拆包后留下的弃用说明，不是 ZCode 在用 LangChain
  - `autogen` 57 命中**确认全是 `autogenerated`/`autogenerate` 的子串误报**
  - `glm/zcode.cjs`（14.8MB 压缩单文件，Node 入口）中 `langchain`/`langgraph`/`@ai-sdk`/`@anthropic-ai` **均 0 命中**，`plugin` 96 命中
  - 插件形态实证：`glm/packages/` 下是 `browser-use-plugin`/`documents-plugin`/`presentations-plugin`/`spreadsheets-plugin`/`pdf-plugin`/`skill-creator-plugin`/`zcode-cua-plugin`/`node-repl-host` 等，每个带 `.zcode-plugin/plugin.json` + `skills/` + `docs/` + `scripts/`
- **结论：ZCode = 自研循环 + 插件架构 + 仅把 Vercel AI SDK 当 provider 层，零 agent 框架**——与 DeepSeek dsh 是同一个模式，只是它连自己的插件框架都没额外立论文

### 5.3 其他主流 coding agent

| 产品 | 栈 | 依赖事实 |
|---|---|---|
| **Aider** | Python | `requirements.txt` 有 `litellm==1.82.3` + `openai`，**无 langchain/langgraph** |
| **mini-SWE-agent** | Python | agent 类约 190 行；README："就是大约 100 行 Python，没有花哨依赖"、"除 bash 外没有别的工具"、"完全线性的历史"、"用 `subprocess.run` 执行动作"；SWE-bench Verified **>74%** |
| **OpenHands** | Python `software-agent-sdk` 1.53.0 | `litellm>=1.93.0` + pydantic + tree-sitter + fastmcp + lmnr，**无 langchain**；有 `OpenHands/litellm` 分叉（每日 `litellm_staging_MM_DD_2026` 分支）——背景是 **2026-03-24 LiteLLM 1.82.7/1.82.8 在 PyPI 被投毒**（这也是 Aider pin 1.82.3、mini-SWE-agent 显式排除这两个版本的原因） |
| **Cline** | TS | 共享核心 `@cline/sdk`（自研 Agent 类 + `createTool`），CLI/桌面/VS Code/JetBrains 都调同一核心 |
| **Continue** | TS | 直接依赖 `openai` 与各 provider SDK，无框架 |
| **Amp（Sourcegraph）** | Go | Thorsten Ball《How to Build an Agent》(2025-04-15)：**"不到 400 行"、"就是一次 LLM 调用、一个循环，和足够的 token"** |
| **Cursor** | 闭源 | Composer 博客：模型被 RL 训练成"能调用 Cursor Agent harness 里的任意工具"；基础设施用 PyTorch+Ray；**未提及任何 agent 框架** |
| **Cognition（Devin）** | 闭源 | 《Don't Build Multi-Agents》(2025-06-12)：多 agent 库"指向错误的构建方式"，"协同多 agent 只会得到脆弱系统"，主张**单线程线性 agent** |
| **Claude Code / Agent SDK** | TS/Python | SDK 自述："给你驱动 Claude Code 的同一套工具、agent 循环和上下文管理"；文档**不提及任何第三方 agent 框架**；其他语言建议"把 CLI 当子进程跑" |

### 5.4 为什么**不**用框架（可引用的原文）

- **Anthropic《Building effective agents》(2024-12-19)**："最成功的实现用的是简单、可组合的模式，而不是复杂框架"；框架"常常多出一层抽象，遮蔽底层的 prompt 和响应，让它们更难调试"；"我们建议开发者先从直接使用 LLM API 开始"
- **sketch.dev《The unreasonable effectiveness of an LLM agent loop with tool use》(2025-05-15)**："核心就是上面那 9 行"；"我最惊讶的是主循环简单得离谱"
- **HumanLayer《12-Factor Agents》**："我试过市面上每一个 agent 框架……**大多数只是自己在从头搭栈。我在生产环境的面向客户 agent 里没看到多少框架的影子。**"；好 agent "主要由普通软件构成"，而不是"给你一个 prompt、一袋工具，循环到命中目标"
- **Simon Willison（2025-09-30、2026-03 前后）**："coding agent 就是一块为 LLM 充当 harness 的软件……用可调用的工具实现"；架构是"LLM + system prompt + 工具，放进一个循环"，"几十行代码"就能搭出来
- **用户侧抱怨**：HN "abstraction soup 让生产调试变成噩梦"、"更愿意自己掌握 prompt，而不是把它藏在五层库代码后面"；Codex 被反复称赞"精简到可以直接在上面堆工具"

### 5.5 为什么**用**框架（反例与条件）

- **Qodo《Why we chose LangGraph to build our coding agent》(2025-03-21)** —— 唯一可核实的、生产级用 LangGraph 做 coding agent 的案例。用到的收益：图/状态机灵活性、节点可复用、**PostgresSaver 几行搞定状态持久化**、检查点/分支做撤销与重放。其自述的困难："文档有时不完整或过时"。**关键限定：Qodo 是多租户的 AI 代码评审平台（IDE+PR+CLI+Git 集成），不是单用户本地 CLI。**
- **Google** 有官方 LangGraph 快速上手样例（`gemini-fullstack-langgraph-quickstart`），但**它自己的终端产品 Gemini CLI 是自研的**
- **持久化执行厂商（Diagrid/Dapr，2026-02-25）**：论证框架检查点**不等于**生产级持久化——"检查点是持久化执行，但持久化执行不等于检查点"，批评 LangGraph/CrewAI/ADK"让你自己当编排者"、无自动故障检测与恢复、无法防止重复执行、单进程。生产需要真正的持久化执行运行时（自动持久化、持久化提醒、分布式再平衡、"零恢复代码"）。注意这是持久化执行厂商的立场

### 5.6 经验规律

**模型厂商手搓 harness，框架是独立的（有时是竞争性的）产品线。** 9 家厂商、9 个自研循环，manifest 里没有任何第三方 agent 框架。两家厂商同时有框架和 harness，而 **harness 不消费框架**（OpenAI Codex vs Agents SDK；阿里 qwen-code vs Qwen-Agent）。

**语言跟随性能目标**：Rust 用于 Codex/grok-build（启动、CPU、内存），TS 用于其余（TUI/桌面/Web 复用），Python 只出现在研究/基准 agent 里。

**框架在哪真的赢**：多租户云产品、需要共享可观测性的团队、结构化状态很重的工作流、需要分布式持久化执行的场景。**这正是单用户本地工具的框架收益全部落空的理由**——它们优化的是启动速度、可审计性和可魔改性（"只要 bash"、"线性历史"、"几十行代码"）；多租户产品优化的是可恢复、公平、成本控制和按租户可观测。

---

## 6. 可操作结论

### 6.1 判定清单：什么情况下该上框架

上框架的判据（满足 ≥2 条才值得付出迁移成本）：
1. 多租户或云端部署，需要线程/运行/定时任务/Webhook/鉴权的托管基础设施
2. 团队 ≥3 人，需要共享 trace 与统一的抽象约定
3. 需要**跨进程死亡**的恢复，且愿意接受"节点从头重跑 + 副作用必须幂等"的重构
4. 需要接 10 个以上模型 provider，自己维护流式归一不划算
5. 主要成本是"从零搭"，不是"调已有系统"

本项目的实际情况：5 条全部不满足（单用户、无团队、已有可用的持久化与恢复语义、只需 2 个协议、已有 21,729 行在正常工作）。

### 6.2 如果仍然要窄接，各借一块（按对本项目的价值排序）

| 想要的东西 | 借什么 | 现实评估 |
|---|---|---|
| provider 广度 | LiteLLM（Aider/OpenHands/DSPy 的选择） | **收益低**：你已原生支持 OpenAI 兼容 + Anthropic 双协议；代价是 58 个传递依赖 + 有投毒前科（2026-03）需 pin |
| 内容块归一（reasoning/usage/缓存命中） | 偷 `langchain-core/messages/block_translators/` 的**设计**，自己实现 | 值得看，但你的 `llm_client.py` 已经处理了两套协议的 reasoning 与缓存计费 |
| 可观测性 | OpenTelemetry（`opentelemetry-api` + OTLP） | 唯一真正中立、可空操作、跨框架通用的缝；**单用户用不上** |
| UI 事件线格式 | AG-UI（Pydantic AI 支持的线格式） | 可作为格式规范，但你是自己两端，没必要换 |
| 会话状态/恢复 | 不借。用你自己的 append-only 日志 + 幂等按键工具执行 | 这是你已有的能力 |
| 分布式持久化 | **仅当**真的需要，用 Temporal（或用 DBOS，但要 Postgres） | 单用户本地不需要 |

### 6.3 一句话

**框架解决的是"多租户、跨进程、团队共享"的规模问题；本项目的问题规模在浏览器的一个 reducer 里。** 把 `interrupt()`/检查点带来的收益，扣掉"节点重跑要重构权限闸门"+"检查点反序列化 CVE 面"+"双写存储"+"事件契约桥接层"，净收益是负的。

---

## 附：数据来源与不确定性

**已由本文作者本机直接验证**：ZCode 应用包中框架/provider 字符串命中分布（`app.asar` / `glm/zcode.cjs` / `glm/packages/`）；本项目规模、提交结构、fix 文件分布、依赖状况（`backend/*.py` 仅 import 标准库 + 自身模块，无 `pyproject.toml`/`requirements.txt`）。

**来自同日 web 调研（含实测 PyPI/npm/GitHub API）**：所有版本号与发布日期；各包分解与 LOC；依赖数与安装体积；13 条 CVE/GHSA 编号；LangGraph 的检查点结构、`interrupt` 重跑语义、durability 模式、OSS/托管分界；各家 harness 仓库、锁文件 grep 结果；Aider/mini-SWE-agent/OpenHands 的依赖清单。

**调研者明确标注未能核实**（转述，勿当事实使用）：
1. OpenAI《Unrolling the Codex agent loop》正文（`openai.com` 返回 403）——仅标题、URL、日期与 HN 讨论可核实
2. Aider/Paul Gauthier 关于"避免框架"的第一手表述——未找到，仅有依赖事实
3. OpenHands 分叉 LiteLLM 的**官方理由**——分叉与投毒事件均属实，因果链未经第一手确认（且 `openhands-sdk` 依赖的是 PyPI 版 `litellm>=1.93.0`，不是 git 分叉）
4. 是否存在"Pydantic AI 在生产 coding 产品中"的案例——未核实
5. Strands 的 `SessionManager`/`FileSessionManager` 等具体类名（文档页 404）
6. Restate Python SDK 的 PyPI 包名；Haystack 遥测默认是否开启
7. `lightdust`/独立用户情绪未系统采样（部分调研环节无 WebSearch 访问），故厂商功能描述按"文档来源"对待
