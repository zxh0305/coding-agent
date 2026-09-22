# 文档生成与预览功能 · 设计文档

> 目标：让 agent 能在对话中把工作成果（汇报、总结、方案）生成 Markdown 文档，
> 并在右侧面板即时查看、随时回看该会话的全部文档。
> 本文档为实施前的方案确认稿，确认后按「实施顺序」逐步开发。

## 1. 需求

1. **对话驱动生成**：用户说「完成一项工作后生成一份汇报文档」，agent 通过**工具**把内容写成 Markdown 文档。
2. **增强渲染**：文档以渲染后的形式展示（不是纯文本），需支持表格、链接、图片等常见 Markdown 语法。
3. **右侧弹出查看**：文档生成后，右侧弹出可关闭的面板（tab 栏）展示内容。
4. **统一入口**：有一个入口能看到**当前会话生成的所有文档**。

## 2. 已定决策

| 项 | 决策 | 说明 |
|---|---|---|
| 存储路径 | `data/docs/<session_id>/<name>.md` | 随会话隔离；`data/` 已在 `.gitignore` |
| 面板形态 | 右侧抽屉，可关闭，与对话并存 | 不遮挡对话区 |
| 格式范围 | 仅 `.md`（本期） | 其他格式后续再拓 |
| 渲染 | 必须渲染，且增强表格/链接/图片 | 复用并扩展自研 `renderMarkdown` |
| 权限 | **文档生成视为低危，直接放行** | 不走写操作的确认闸门 |
| 入口位置 | 顶部工具栏（`composer` 的 `.toolbar`），放在 **📁 工作区 chip 之后、⚙️ 模型 chip 之前** | 与现有 chip 同级，始终可见、切会话自动跟随 |
| 写入入口 | **只走 agent 工具**，不开放手动上传接口 | 入口单一，权限统一 |
| 更新语义 | 同名即覆盖（=更新），不做单独删除工具 | 删除随会话删除清理 |

## 3. 架构总览

```
用户对话「生成汇报文档」
        │
        ▼
   Agent 调用 create_doc 工具 ──► db.write_doc(sid, name, content)
        │                                   │
        │ 写入 data/docs/<sid>/<name>.md ◄──┘
        ▼
   发 doc_created 事件（走现有 SSE 事件总线）
        │
        ▼
   前端收到 → 右侧抽屉自动展开 → 拉取文档 → 渲染展示
```

- **写入只有一条路径**：`create_doc` 工具 → `db.write_doc`。
- **读取**：前端通过 `GET /api/sessions/<id>/docs` 列目录、`.../docs/content?name=` 取原文。
- **事件驱动**：生成完成即推送，前端自动弹出，无需用户手动刷新。

## 4. 存储层（`backend/db.py`）

### 4.1 目录

- 新增 `_docs_dir()`，与 `_artifacts_dir()` 同构（跟随 `DB_PATH.parent`，测试可重定向）：
  `DB_PATH.parent / "docs"`。
- 单会话目录：`data/docs/<session_id>/`。

### 4.2 路径校验（安全关键）

复用 artifacts 的思路：`resolve()` 消解 `../` 与软链后，目标必须仍严格位于
`data/docs/<session_id>/` 之内。拦截：

- 路径逃逸（`../`、绝对路径）；
- 跨会话读取（sid 由服务端从 URL 取，不信任前端）；
- 非 `.md` 文件（本期只放行 `.md`）；
- 文件名中的分隔符 / 空名 / 超长名。

### 4.3 新增函数

| 函数 | 作用 |
|---|---|
| `list_docs(sid) -> list[dict]` | 列出该会话文档：`{name, bytes, mtime}`，按 mtime 降序 |
| `read_doc(sid, name) -> str` | 读单个 md 原文（校验后） |
| `write_doc(sid, name, content) -> dict` | 写入/覆盖，返回 `{name, bytes, lines}` |
| `_docs_dir() -> Path` | docs 根目录 |

### 4.4 会话删除清理

在 `delete_session(sid)` 中，紧挨现有删除 `artifacts/<sid>/` 的那行，
增加 `shutil.rmtree(_docs_dir() / sid, ignore_errors=True)`，
保证会话删除时文档一并消失（避免孤儿文件）。

## 5. 工具层（新增 `backend/doc_tools.py`，并入 `tools.py` 注册表）

### 5.1 工具 `create_doc`

```
create_doc(name: str, content: str, ctx=None) -> str
```

- **schema 描述**：明确「当用户要求生成文档 / 汇报 / 总结 / 方案时用它」；
  `content` 是**完整 Markdown 正文**（含标题、列表、表格等），不是摘要。
  `name` 是文件名（自动补 `.md`，不含路径）。
- **session_id 来源**：通过 `ToolContext.session_id` 注入（新增字段），
  由 `app.py` 构造 Agent 时填入。**工具层不持有全局状态**——与现有
  「状态挂调用方、随 `execute_tool` 显式传入」的约定一致（见 `ToolContext` 注释）。
- **返回值**：成功走 `_ok({...})`，失败走 `error_result(...)` 统一信封，
  让模型能读 `error`/`hint` 改道。
- **无 session_id 时**：返回错误信封（提示内部问题），不静默写错地方。

### 5.2 权限

- 在 `TOOL_READ_ONLY` 中标记 `create_doc` 为 **低危放行**。
- 实现方式：`permissions.py` 现有闸门按 `is_read_only` 判定写操作是否需确认。
  低危放行 = 在权限判定里对 `create_doc` 单独豁免（或标记为「只读等效」），
  使其**在任何权限模式下都不弹确认卡**。具体落点实施时对齐 `permissions.py` 的判定函数。

### 5.3 并行标记

- `create_doc` 是写操作，`TOOL_READ_ONLY["create_doc"] = False`（串行），
  与其它写工具一致（保守起见）。

## 6. 后端接口（`backend/app.py`）

挂在 `/api/sessions/<id>/docs`，**复用现有归属校验**（`session_owner(sid) == 当前用户`，
否则 404）。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/sessions/<id>/docs` | 列出该会话文档（`{name, bytes, mtime}` 数组） |
| GET | `/api/sessions/<id>/docs/content?name=` | 读单个 md 原文，返回 `{name, content}` |

- 路由接入点在 `do_GET` 的 `/api/sessions/<id>/...` 分支（现有 `artifact` / `events`
  同级的 `sub` 分发处）。
- 不新增 POST：写入只走工具。

### 6.1 SSE 事件 `doc_created`

- 工具写入成功后，通过现有**每会话事件总线**（`events.py`）推一条
  `doc_created` 事件，载荷 `{name}`（或 `{name, bytes}`）。
- 前端 `onSSEEvent` / `applyEvent` 增加该类型处理：展开右侧抽屉并打开该文档。
- 需确认 `seq` 持久化与环形缓冲对自定义事件透明（现有机制对新事件类型应无侵入，
  实施时验证）。

## 7. 前端（`frontend/`）

### 7.1 入口（工具栏）

`index.html` 的 `.toolbar` 中，在 `#ws-pick`（📁 工作区）之后、`#model-chip`（⚙️ 模型）
之前插入：

```html
<button id="docs-chip" class="chip" title="查看本会话生成的文档">
  📄 文档 <span id="docs-count">0</span>
</button>
```

- `docs-count` = 当前会话文档数，切会话时刷新（`switchSession` 里拉一次 `list`）。
- 点击 = 打开/收起右侧抽屉。

### 7.2 右侧抽屉 `#docs-panel`

```html
<aside id="docs-panel" class="docs-panel hidden">
  <div class="docs-head">
    <b>📄 文档</b>
    <button id="docs-close" class="docs-x" title="关闭">✕</button>
  </div>
  <div class="docs-body">
    <ul id="docs-list" class="docs-list"></ul>   <!-- 左列：文档列表 -->
    <div id="docs-view" class="docs-view"></div> <!-- 右侧：渲染区 -->
  </div>
</aside>
```

- 关闭：✕ 或再次点 `#docs-chip`。
- 列表项：文件名 + 大小/时间；点选 → 拉 `content` → 渲染到 `#docs-view`。
- 空态：「本会话还没有文档，试试让 agent 生成一份」。
- 样式：与现有 `git-pop` / `model-pop` 同一套视觉语言（见 `style.css`）。

### 7.3 事件联动

- 收到 `doc_created` → 自动展开抽屉 → 刷新列表 → 打开新文档。
- 切会话：抽屉内容清空并重新拉取该会话列表（天然会话归属）。

### 7.4 Markdown 渲染增强（`renderMarkdown`）

现有已支持：代码块、`#` 标题、有序/无序列表、引用、分隔线、行内 `` `code` ``、
`**bold**`、`*italic*`。

**本期新增**：

| 语法 | 说明 |
|---|---|
| 表格 | `\| a \| b \|` + 分隔行，渲染为 `<table>` |
| 链接 | `[text](url)`，`target="_blank" rel="noopener"` |
| 图片 | `![alt](src)`，渲染为 `<img>` |

**安全约束**（沿用「绝不拼 innerHTML、全程 createElement」的既有原则）：

- 链接 href 只允许 `http:` / `https:`（其余如 `javascript:` 一律降级为纯文本）；
- 图片 src 同理白名单；
- 所有文本节点用 `createTextNode` / `textContent`，不引入注入面。

## 8. 实施顺序（每步可独立验证）

1. **存储层**：`db.py` 的 `_docs_dir` / `list_docs` / `read_doc` / `write_doc` +
   路径校验 + `delete_session` 清理 → 加单测（`backend/tests/test_docs.py`）。
2. **工具层**：`doc_tools.py` + `tools.py` 注册 + `ToolContext.session_id` +
   权限低危放行 → 单测（含无 session_id、越界名、同名覆盖）。
3. **接口 + 事件**：`app.py` 两个 GET + `doc_created` 事件 + Agent 注入 session_id。
4. **前端骨架**：工具栏入口 + 右侧抽屉 + 列表/渲染 + 事件联动。
5. **渲染增强**：`renderMarkdown` 补表格/链接/图片 + 安全校验；联调 + 边界
   （空态、超长文档、会话删除、非 md 拒绝）。

## 9. 验收标准

- [ ] 对话说「生成一份汇报文档」，agent 调 `create_doc`，右侧抽屉自动弹出并渲染。
- [ ] 文档落盘在 `data/docs/<sid>/`，重启服务后仍在。
- [ ] 工具栏入口能列出并打开该会话**全部**文档。
- [ ] 表格 / 链接 / 图片正确渲染；`javascript:` 链接被安全降级。
- [ ] 删除会话时该会话文档目录一并清除。
- [ ] 只读权限模式下生成文档不弹确认卡（低危放行）。
- [ ] 后端单测 + `node --check` 全过。

## 10. 待确认 / 风险

- `permissions.py` 低危豁免的**具体落点**（在判定函数里按工具名白名单）需实施时对齐，
  避免影响其它写工具的确认逻辑。
- `doc_created` 事件需确认与现有 `seq` 持久化 / 环形缓冲机制无冲突。
- 文档体积上限：是否设单文件上限（如 2MB）以防超大写入？建议设一个宽松上限并在
  工具层拦截。
