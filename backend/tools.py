"""
工具层（Tools / Function Calling）
==================================

LLM 本身只会"生成文字"，改不了文件、跑不了命令。
工具就是给 Agent 装上的"手脚"：

  1. schema —— JSON 格式的"说明书"，随每次请求发给 LLM。
     LLM 靠它知道有哪些工具、各自做什么、参数怎么填。
  2. 实现   —— 真正干活的 Python 函数，由 Agent 在【本地】执行，
     再把执行结果作为消息塞回对话，LLM 下一轮就能"看到"结果。

一个工具什么时候被调用、传什么参数，是 LLM 决定的；
但真正执行的一定是我们本地的 Python 代码 —— 这就是 Function Calling 的本质。

工具面收敛原则（对齐 Claude Code / ZCode 的"少而强"）：能被 run_bash 覆盖的
小工具不单列（早期 demo 工具 calculator / current_time / get_weather 已移除，
时间改为 system 消息注入，见 agent._system_content）——每个工具的 schema 都
随每次请求全量发送，砍掉冗余工具就是省上下文。
"""

import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 第一部分：工具实现（普通 Python 函数，返回值统一转成字符串）
# ---------------------------------------------------------------------------


# 任务清单工具（todo_write）：长任务的可见进度条
# 对齐 Claude Code TodoWrite / ZCode todo.ts 的设计：模型把多步任务拆成清单，
# 随进度更新状态（pending → in_progress → done）。清单内容存在 ToolContext
# （会话隔离），同时 Agent 依据返回值产出 todo_update 事件推给前端渲染清单卡——
# 用户不用翻执行过程就能看到"现在做到第几步、还剩什么"。
# ---------------------------------------------------------------------------

_TODO_STATUS = ("pending", "in_progress", "done")


def todo_write(todos: list, ctx: "ToolContext" = None) -> str:
    """整体替换当前会话的任务清单。todos：[{content, status}]。

    每次都发全量清单（而不是增量改动）——模型端"一次写全"比"记住上次再改"
    更不易漂移，这也是 CC TodoWrite 的取舍。校验：status 必须合法、content
    非空；同一时刻至多一条 in_progress（多出的自动降为 pending，不报错——
    提示即可，别让格式小错打断任务流）。
    """
    if not isinstance(todos, list) or not todos:
        return error_result("todos 必须是非空数组",
                            "示例：{\"todos\": [{\"content\": \"读代码\", \"status\": \"done\"}]}；"
                            "任务全部完成或不再需要清单时传 [{\"content\": \"(cleared)\", \"status\": \"done\"}] 或说明已清空")
    clean, in_progress_seen = [], False
    for i, t in enumerate(todos):
        if not isinstance(t, dict) or not str(t.get("content") or "").strip():
            return error_result(f"第 {i + 1} 项缺少 content",
                                "每项须为 {\"content\": 任务描述, \"status\": pending|in_progress|done}")
        status = str(t.get("status") or "pending")
        if status not in _TODO_STATUS:
            return error_result(f"第 {i + 1} 项 status 非法: {status}",
                                "status 只允许 pending / in_progress / done")
        if status == "in_progress":
            if in_progress_seen:
                status = "pending"  # 多个 in_progress 自动降级（首个保留）
            in_progress_seen = True
        clean.append({"content": str(t["content"]).strip()[:200], "status": status})
    if ctx is not None:
        ctx.todos = clean
    done = sum(1 for t in clean if t["status"] == "done")
    return json.dumps({"ok": True, "todos": clean,
                       "progress": f"{done}/{len(clean)}"}, ensure_ascii=False)


def error_result(error: str, hint: str = "") -> str:
    """统一失败信封：{ok:false, error, hint?}。

    所有工具的失败（参数错误/执行异常/权限拒绝 permissions.rejection_result）
    共用这一个结构，模型只需要学一次「ok:false → 读 error 与 hint 改道」。
    error 说明为什么失败；hint 给下一步建议（换路径/换工具/先侦察），
    让模型改道而不是原样重试。
    """
    payload = {"ok": False, "error": error}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 第二部分：工具 schema（发给 LLM 的"说明书"，格式与 OpenAI 兼容接口一致）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = []

# ---------------------------------------------------------------------------
# 第二部分：注册表 + 统一执行器（Agent 只跟这里打交道）
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """工具执行上下文：一次工具调用能"看见"的全部环境。

    图片和看图后端曾经是本模块的两个模块级全局变量——两个会话并发执行
    analyze_image 时，后设置的图片列表会覆盖先设置的，A 会话的模型可能
    拿到 B 会话的图。教训：工具层不持有任何"当前请求"状态，状态挂在
    调用方（Agent 实例）上，随每次 execute_tool 显式传入。
    """

    workspace: Path | None = None               # 本会话的工作区（文件/命令工具的边界）
    images: list = field(default_factory=list)  # 本轮用户消息附带的图片（OpenAI content 部分）
    todos: list = field(default_factory=list)   # 本会话任务清单（todo_write 维护，会话隔离）
    vision_backend: object = None               # fn(image_parts, question) -> str，由 app.py 注入
    browser: object = None                      # BrowserManager（browser_tools），由 agent.py 按
                                                # 会话注入；工具层不持有实例（与会话生命周期同寿）
    session_id: str | None = None               # 本会话 id（文档工具据此确定文档归属）

TOOL_REGISTRY = {
    "todo_write": todo_write,
}

# ---- 合并 Coding 工具（code_tools.py）：读写工作区文件、执行命令 ----
from code_tools import CODE_TOOL_REGISTRY, CODE_TOOL_READ_ONLY, CODE_TOOL_SCHEMAS

TOOL_SCHEMAS += CODE_TOOL_SCHEMAS
TOOL_REGISTRY.update(CODE_TOOL_REGISTRY)

# ---- 图片识别工具（analyze_image）----
# 设计模式："用工具补偿模型短板"。主模型不支持视觉（或看不清细节）时，
# 由这个工具借一个标记了"视觉"的模型把图片转成文字描述。
#
# 依赖注入：工具层不该知道"有哪些模型、怎么构造客户端"，看图函数由 app.py
# 构造 Agent 时注入（ToolContext.vision_backend）。工具需要的图片数据也不
# 来自模型参数——模型看不见像素，图片列表由 Agent 每轮从用户消息里提取后
# 更新到 ToolContext.images。

def analyze_image(image_id: str = "", question: str = "请详细描述这张图片的内容", ctx: ToolContext = None) -> str:
    images = ctx.images if ctx is not None else []
    if not images:
        # 没有用户上传图片时回退浏览器截图：browser_screenshot 之后 agent 调
        # 本工具即可"看见"页面（browser_tools 截图后把 base64 挂在 ctx.browser 上）
        shot = getattr(getattr(ctx, "browser", None), "last_shot_b64", None) if ctx is not None else None
        if not shot:
            return error_result("当前这条消息没有附带图片，浏览器也没有最新截图",
                                "若想看网页内容：先调 browser_screenshot 再调本工具；用户图片则让用户重新上传")
        images = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot}"}}]
        index = 0
    else:
        # image_id：'1'/'2'/... 按用户消息中图片出现顺序；空值默认第一张
        digits = "".join(ch for ch in str(image_id) if ch.isdigit())
        index = (int(digits) - 1) if digits else 0
        if index < 0 or index >= len(images):
            index = 0
    backend = ctx.vision_backend if ctx is not None else None
    if backend is None:
        return error_result("图片识别后端未配置（系统内部问题，请联系服务部署者）")
    try:
        description = backend([images[index]], question)
    except RuntimeError as e:
        # 视觉模型调用失败：把原因交回主模型，让它告知用户怎么办
        return error_result(f"视觉模型调用失败：{e}",
                            "请在「管理模型」里给某个模型勾选'视觉'并确保其 Key 可用，然后重试")
    return json.dumps({"ok": True, "image_id": str(index + 1), "result": description}, ensure_ascii=False)


TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": "维护当前任务的待办清单（整体替换）。多步任务（≥3 步或需要跨多轮工具调用）"
                       "开始时先写全清单，每完成一步就更新状态：pending 待做 / in_progress 进行中"
                       "（同一时刻至多一条）/ done 已完成。简单问答、单步任务不要用。"
                       "示例：{\"todos\": [{\"content\": \"定位 bug\", \"status\": \"done\"},"
                       "{\"content\": \"修复并验证\", \"status\": \"in_progress\"}]}。",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "全量清单，每次调用都发完整列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "任务项描述（≤200 字）"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done"],
                                       "description": "状态"},
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    },
})

TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": "识别/分析用户消息中附带的图片：描述内容、定位细节或读出图中文字。"
                       "你（主模型）看不到图片像素，凡需要看图都必须调它；当前消息没带图片时会返回错误，"
                       "此时直接告诉用户重新上传即可。"
                       "image_id 按图片在消息中的顺序（'1' 是第一张，留空默认第一张）；"
                       "question 写具体想了解什么，问得越准答案越有用。"
                       "示例：{\"image_id\": \"1\", \"question\": \"图中的报错文字是什么\"}。",
        "parameters": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string",
                             "description": "图片编号：'1' 是用户消息中的第一张图，'2' 是第二张；留空默认第一张"},
                "question": {"type": "string",
                             "description": "你想从这张图里了解什么，例如'图中有什么文字'；不填则返回整体描述"},
            },
            "required": [],
        },
    },
})
TOOL_REGISTRY["analyze_image"] = analyze_image

# ---- 文档工具（doc_tools.py）：agent 生成 Markdown 文档 ----
from doc_tools import DOC_TOOL_REGISTRY, DOC_TOOL_READ_ONLY, DOC_TOOL_SCHEMAS

TOOL_SCHEMAS += DOC_TOOL_SCHEMAS
TOOL_REGISTRY.update(DOC_TOOL_REGISTRY)

# ---- 附件工具（attachment_tools.py）：读取用户上传的会话附件 ----
from attachment_tools import (ATTACH_TOOL_REGISTRY, ATTACH_TOOL_READ_ONLY,
                              ATTACH_TOOL_SCHEMAS)

TOOL_SCHEMAS += ATTACH_TOOL_SCHEMAS
TOOL_REGISTRY.update(ATTACH_TOOL_REGISTRY)

# ---- 浏览器工具（browser_tools.py）：内置 Chromium 验证/登录/调研 ----
# 可选依赖：playwright 未安装时工具返回可读安装指引，不影响其它功能
from browser_tools import (BROWSER_TOOL_REGISTRY, BROWSER_TOOL_READ_ONLY,
                           BROWSER_TOOL_SCHEMAS)

TOOL_SCHEMAS += BROWSER_TOOL_SCHEMAS
TOOL_REGISTRY.update(BROWSER_TOOL_REGISTRY)

# ---------------------------------------------------------------------------
# 工具元数据：read_only（是否只读、能否并行）
#
# 标记原则：只有"对工作区与会话状态零写入"的工具才标 True——
# read_file / list_dir / grep 只打开文件读，todo_write 只写 ToolContext 内存，
# 它们与同组其它只读工具并行执行的结果和串行完全一致。
# 其余一律 False（按写操作串行）：write_file / apply_patch / run_bash 真的
# 会写；analyze_image 虽不写工作区，但要出网/跨模型调用，保守起见也不并行。
# Agent 的分组调度完全依据这份表（见 agent.py）。
# ---------------------------------------------------------------------------

TOOL_READ_ONLY = {
    "analyze_image": False,
    # todo_write 只写 ToolContext 内存（不碰工作区/不出网），并行安全
    "todo_write": True,
}
TOOL_READ_ONLY.update(CODE_TOOL_READ_ONLY)  # 并入 coding 工具的标记（同样的合并方式）
TOOL_READ_ONLY.update(DOC_TOOL_READ_ONLY)   # 并入文档工具（create_doc 为非只读，走串行）
TOOL_READ_ONLY.update(ATTACH_TOOL_READ_ONLY)  # 并入附件工具（list/read_attachment 均只读）
TOOL_READ_ONLY.update(BROWSER_TOOL_READ_ONLY)  # 并入浏览器工具（全部非只读：出网/改页面状态）

def is_read_only(name: str) -> bool:
    """name 是否只读工具。未知工具返回 False——没有元数据就当写操作走串行，永远站在安全侧。"""
    return bool(TOOL_READ_ONLY.get(name))


def tool_schema(name: str) -> dict | None:
    """按名字查工具的 parameters 定义。

    仅在参数解析失败路径调用（失败是例外不是常态，线性扫一遍无所谓）：
    把期望参数形状随失败信封回传，模型在同一轮就能自行修正参数重试，
    不必"猜字段名 → 再错一轮 → 再猜"。未知工具返回 None（错误信封不带
    schema 字段）。
    """
    for s in TOOL_SCHEMAS:
        fn = s.get("function") or {}
        if fn.get("name") == name:
            return fn.get("parameters")
    return None


def execute_tool(name: str, arguments: dict, ctx: ToolContext | None = None) -> str:
    """按名字执行工具。

    注意：工具报错时【不抛异常】，而是按统一失败信封 {ok:false, error, hint?}
    返回给 LLM——模型看到原因与建议才能自行纠正（换参数重试 / 换个工具 /
    直接告知用户），绝不静默失败。

    ctx：本次调用的执行上下文（工作区、图片、看图后端），由 Agent 注入。
    声明了 ctx 形参的工具才拿到它。ctx 不出现在 schema 里——"在哪个工作区
    干活"是会话属性，由服务端决定，不该是模型可填的参数。
    """
    func = TOOL_REGISTRY.get(name)
    if func is None:
        return error_result(f"未知工具：{name}", "确认工具名是否在系统提供的工具清单里（区分大小写）")
    try:
        if "ctx" in inspect.signature(func).parameters:
            return func(**arguments, ctx=ctx)
        return func(**arguments)
    except TypeError as e:  # 参数缺失/多传/类型不对：统一转信封，并附上期望 schema
        # 让模型在同一轮对照修正、原样重试（DeepSeek harness 的"错误带 schema
        # 回传"模式）。schema 可选：未知工具没有定义可附。
        payload = {"ok": False, "error": f"参数不匹配: {e}",
                   "hint": "对照 schema 核对参数名与类型后重试"}
        schema = tool_schema(name)
        if schema:
            payload["schema"] = schema
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        return error_result(f"{type(e).__name__}: {e}", "执行失败，可调整参数重试或换用其它工具")


def describe_tools() -> str:
    """列出工具清单（启动横幅用）。"""
    return "\n".join(f"  - {s['function']['name']}: {s['function']['description']}" for s in TOOL_SCHEMAS)
