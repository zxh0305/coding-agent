# 事件协议（protocol.md）——命令接口 + 常驻 SSE 事件流

本文是前后端事件契约的单一来源。改动事件结构（新增/修改/删除事件、载荷字
段）必须同步更新本文、`frontend/blocks.js` 的消费逻辑与 `tests/test_sse.py`
（后者是本协议的可执行规范）。

## 1. 总览

前端**不**借 POST 响应流接收执行过程：`POST /api/sessions[...]` 只入队、立
即返回 `{session_id, nonce}`；回合由后台 worker 按会话锁串行执行，全部过程
事件经 `events.SessionEvents.publish()` 进【每会话一条】的事件总线，由
`GET /api/sessions/<sid>/events?since=<seq>` 接出的常驻 SSE 连接消费。

这是整套实时体验的地基：断线自动重连（浏览器 EventSource 自带 Last-Event-ID）、
刷新接上正在进行的回合、多标签页共用同一真相，全部建立在下述 seq/补发规则上。

## 2. 两套身份，绝不混用

| 身份 | 是什么的身份 | 生命周期 | 用途 |
|---|---|---|---|
| `seq` | 事件在会话内的流水号 | 单调递增；持久化到 `sessions.last_seq`（重启不归零） | SSE `id:` 行；断线重连游标；客户端去重闸门 |
| `mid` | 消息（存储层的一条） | 落库时分配的 uuid4 | 历史回放、delta 归并、补发去重 |
| `nonce` | 一次提交的随机数 | 仅本次提交 | turn_start 去重：前端自己画的气泡与服务端事件对齐 |
| `user_mid` | 本回合用户消息的 mid | 落库即定 | `turn_end.user_mid` 是补发去重的锚（见 §5） |

delta 归并规则：`answer_delta` 带 `mid` → 归并进同一气泡；`reasoning_delta`
**绝不带 mid**——带了就会把思考流冒充成回答正文（真实踩过）。

## 3. 三类流内容，只有第一类进缓冲

1. **业务事件**：只能经 `publish()` 进（统一分配 seq、写环形缓冲、分发订阅
   队列）。这是唯一写入路径，任何旁路直拼 SSE 帧都会破坏"重连不丢不重"。
2. **心跳** `: ping`（注释行，每 15 秒）：仅写在网络连接上防代理掐空闲连接。
   不分配 seq、不进缓冲、不进订阅队列——对重连者没有回放价值。
3. **连接态事件** `resync` / `caught_up`：只对当前这条连接的补发阶段有意义。
   不进缓冲、直写、**不带 `id:` 行**（不污染浏览器自动维护的 Last-Event-ID）。

## 4. 事件目录

### 4.1 回合生命周期（worker 线程发出）

| type | 载荷 | 时机与要点 |
|---|---|---|
| `turn_start` | `{nonce, input, atts, started_at}` | 回合开始。前端据此画用户气泡；补发场景与本地气泡靠 nonce 去重；started_at 是回合真起点——补发/刷新后的页面没有本地计时起点，靠它接上"已工作 N 秒" |
| `round` | `{round}` / `{round, wrap_up: true}` | 每轮开始；wrap_up=true 表示收尾轮（禁工具） |
| `done` | `{answer, elapsed_s, usage, cache_hit_rate, context, context_tokens, stopped?}` | 最终回答，循环结束信号之一；`stopped: true` = 用户手动停止 |
| `turn_end` | `{user_mid}` | 回合彻底收尾（落盘之后）。**user_mid 是补发去重键**；`user_mid: null` = 提交时已失效的回合。前端把 turn_end 当回合结束的唯一信号 |
| `error` | `{message, retryable}` | LLM 失败。retryable=true 时前端给"重试"按钮；错误同时落库为 assistant 消息（回放可见） |

### 4.2 回答与思考

| type | 载荷 | 要点 |
|---|---|---|
| `answer_delta` | `{mid, text}` | 回答增量；按 mid 归并进同一气泡 |
| `reasoning_delta` | `{text}` | 思考增量；**不带 mid**（§2）；同时触发 trace 节流快照落库 |

### 4.3 工具执行

| type | 载荷 | 要点 |
|---|---|---|
| `tool_call` | `{name, arguments}` | 模型请求调用；arguments 是 JSON 字符串原文 |
| `tool_result` | `{name, result}` | 执行结果信封 JSON 串（`{ok, result\|error, hint?, schema?}`） |
| `permission_request` | 闸门载荷 | ask 暂停时发；用户在 `POST /api/sessions/<sid>/permission/<pid>` 回答 |
| `todo_update` | `{todos}` | 清单变化；同时持久化（sessions.todos） |
| `doc_created` | `{name}` | create_doc 成功后（文档栏刷新） |
| `browser_shot` | `{url, note, shot}` | browser_screenshot 后（shot 是截图读取接口的相对 URL） |
| `usage` | `{prompt_tokens, completion_tokens, ...}` | 每轮真实用量；进容量缓存 `_ctx[sid]` |

**子代理（spawn_subagent）v0 契约**：子代理是父回合内部同步跑的只读侦察员，
其全部过程事件（round/answer_delta/tool_call/…）**就地消费、不外发**——父回合
时间线只见一对 `tool_call` / `tool_result`，后者是结论信封
`{ok, report, rounds, usage}`（失败为 `{ok:false, error, hint?}`）。不做任何
特殊前端处理。嵌套 trace（parent 标识、子代理折叠卡）留 v1。

### 4.4 压缩与历史

| type | 载荷 | 要点 |
|---|---|---|
| `compacted` | `{summary, prompt_tokens, context}` | 自动压缩（turn_end 前后）或 `/compact` 手动触发；前端插分隔卡 + 刷容量徽章 |
| `history_truncated` | `{}` | 回退编辑后；前端全量重拉时间线 |
| `history_renumbered` | `{}` | ord 间隔耗尽触发整会话重编号（罕见兜底）；分页游标失效须重拉 |

### 4.5 会话级（跨会话可见）

| type | 载荷 | 要点 |
|---|---|---|
| `session_title` | `{session_id, title}` | 自动起标题完成；列表刷新 |
| `session_deleted` | `{}` | 本会话被删除；本页所有订阅者收尾 |
| `api_retry` | `{error, attempt, max_attempts, wait}` | LLM 瞬态错误退避重试中；前端显示"正在重试(n/m)"徽标 |

### 4.6 连接态（不进缓冲，无 `id:` 行）

| type | 载荷 | 要点 |
|---|---|---|
| `caught_up` | `{running}` | 补发完成。`running: false` 而前端仍挂在生成中 → 回合死于断线间，提示重发 |
| `resync` | `{current_seq}` | 补不齐（§5）：客户端全量刷新后以 current_seq 为锚重连 |

## 5. 补发规则（replay_plan）

客户端带 `?since=<已见最大 seq>`（浏览器自动重连带 Last-Event-ID，前端另在
localStorage 记每会话最近 seq）。服务端按客户端状态推导，三态：

- `("live", [])` 无缺口，直接续实时流；
- `("replay", xs)` 先按 seq 升序补发 xs 再续实时流；
- `("resync", [])` 补不齐 → 发 resync，客户端全量刷新后重连。

推导要点（完整推导见 `events.py replay_plan` docstring）：

1. `position > current_seq`（服务重启后 seq 落后）→ resync；
2. 缓冲最老 seq > position+1（中间事件被挤掉）→ resync；缓冲为空且
   position == current_seq 视为没漏；
3. **回合进行中且 turn_start 仍在缓冲**：从 `min(position+1, turn_start)`
   补起——刷新页面接上正在输出的回合时，连回合开头一起补。**多补无害少补
   致命**：客户端有 seq 闸门会跳过已应用的事件，但缺 turn_start 就画不出
   用户气泡和过程时间线；
4. 全新观看者（position 为 None）：只补正在进行的回合；更早的"过去"由
   分页接口负责；
5. 超长回合把 turn_start 挤出缓冲：从最老 seq 尽力补尾巴，**绝不 resync**
   ——resync 后重连 turn_start 依然不在缓冲，会无限循环；丢回合开头的展示
   （done 仍带完整回答）比死循环轻。

## 6. 客户端消费契约

1. **seq 闸门**：应用事件前检查 `seq > lastSeq`（本页已应用的最大值）——
   补发段会故意整段重发与本页重叠的事件，不跳过会重复应用（turn_start 重
   复应用 = trace 被清空重建、秒数闪跳回 0）。
2. **turn_end 是回合结束的唯一信号**：worker 线程无声死掉时，所有订阅页
   会永远挂在"生成中"——任何把回合结束绑定在其他事件上的写法都是错的。
3. **刷新恢复**：localStorage 记每会话最近 seq；打开页面先拉历史分页接口
   （mid/ord 世界），再以 `?since=seq` 补发接活区。
4. **trace 快照**：进行中回合的执行过程按 user_mid 节流落库（session_traces
   表），历史接口发现回合仍在跑时带回 running_trace，切会话/刷新据此补画
   折叠条；正式轨迹在收尾时按 answer_mid 落库、临时快照行删除。

## 7. 演进规则

- 新增事件：先在本文目录登记（type、载荷、时机），再实现 publish 与前端
  消费，最后在 `tests/test_sse.py` 补行为断言。
- 载荷字段只增不改语义：旧前端可能还活着（EventSource 常驻连接跨服务重启）。
- seq/补发/心跳三块的语义变更属于破坏性变更：必须同步 replay_plan、
  HTTP 层写帧逻辑与本文，且补 test_sse 用例。
