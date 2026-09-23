"""
工具层（Tools / Function Calling）
==================================

LLM 本身只会"生成文字"，不能算数、不知道现在几点、查不了天气。
工具就是给 Agent 装上的"手脚"：

  1. schema —— JSON 格式的"说明书"，随每次请求发给 LLM。
     LLM 靠它知道有哪些工具、各自做什么、参数怎么填。
  2. 实现   —— 真正干活的 Python 函数，由 Agent 在【本地】执行，
     再把执行结果作为消息塞回对话，LLM 下一轮就能"看到"结果。

一个工具什么时候被调用、传什么参数，是 LLM 决定的；
但真正执行的一定是我们本地的 Python 代码 —— 这就是 Function Calling 的本质。
"""

import ast
import datetime
import inspect
import json
import operator
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 第一部分：工具实现（普通 Python 函数，返回值统一转成字符串）
# ---------------------------------------------------------------------------

# calculator 用"白名单"方式做安全求值：只允许数字和四则运算，
# 绝不能直接 eval() 用户/模型给来的字符串（会被注入任意代码）。
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"表达式中含有不允许的元素：{ast.dump(node)}")


def calculator(expression: str) -> str:
    """计算四则运算表达式，如 '37*89+100'。"""
    try:
        value = _safe_eval(ast.parse(expression.strip(), mode="eval"))
    except ZeroDivisionError:
        return error_result("除数为 0", "改写表达式避开除零，或先算分母确认非零")
    except (SyntaxError, ValueError) as e:
        return error_result(f"表达式不合法: {e}", "只允许数字、四则运算符（+ - * / // % **）和括号，检查后再试")
    # 演示约定：工具返回值统一是 JSON 字符串（LLM 读起来最稳定）
    return json.dumps({"ok": True, "result": f"{expression} = {value}"}, ensure_ascii=False)


def current_time() -> str:
    """返回当前本地时间。"""
    now = datetime.datetime.now()
    return json.dumps(
        {"ok": True,
         "result": f"{now.strftime('%Y-%m-%d %H:%M:%S')} 周{'一二三四五六日'[now.weekday()]}"},
        ensure_ascii=False,
    )


# 模拟数据：真实项目里这里往往是调外部 API（高德/和风天气等）
_FAKE_WEATHER = {
    "北京": ("晴", 22, "西北风 3 级"),
    "上海": ("多云", 26, "东南风 2 级"),
    "广州": ("阵雨", 31, "南风 2 级"),
    "深圳": ("雷阵雨", 30, "东风 3 级"),
    "杭州": ("晴转多云", 27, "微风"),
}


def get_weather(city: str) -> str:
    """查询某城市天气（本 demo 返回模拟数据）。"""
    if city not in _FAKE_WEATHER:
        return error_result(f"没有 {city} 的天气数据",
                            f"模拟库只收录：{'、'.join(_FAKE_WEATHER)}，请换这些城市之一")
    sky, temp, wind = _FAKE_WEATHER[city]
    return json.dumps(
        {"ok": True, "result": f"{city}：{sky}，{temp}℃，{wind}（模拟数据）"},
        ensure_ascii=False,
    )


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

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "精确计算四则运算（+ - * / // % ** 与括号）。任何算术都必须用它，禁止心算——多位数、小数、"
                           "大数的心算必错。什么时候不用：一眼可判的比较（3 和 5 谁大）不必调用。"
                           "示例：{\"expression\": \"(1024*768)/8/1024\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "要计算的表达式，例如 '37*89+100'"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "获取当前本地日期、时间与星期。凡涉及「现在几点 / 今天几号 / 截止日期还有几天」一律用它，"
                           "不要凭感觉报时间。无参数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市今天的天气（演示用模拟数据，非真实天气，回复用户时须说明）。"
                           "只支持：北京、上海、广州、深圳、杭州。示例：{\"city\": \"北京\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，例如 '北京'"},
                },
                "required": ["city"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# 第三部分：注册表 + 统一执行器（Agent 只跟这里打交道）
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
    vision_backend: object = None               # fn(image_parts, question) -> str，由 app.py 注入
    session_id: str | None = None               # 本会话 id（文档工具据此确定文档归属）

TOOL_REGISTRY = {
    "calculator": calculator,
    "current_time": current_time,
    "get_weather": get_weather,
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
        return error_result("当前这条消息没有附带图片", "请让用户重新上传图片后再试，不要凭空描述图片内容")
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

# ---------------------------------------------------------------------------
# 工具元数据：read_only（是否只读、能否并行）
#
# 标记原则：只有"对工作区与会话状态零写入"的工具才标 True——
# read_file / list_dir / grep 只打开文件读，calculator / current_time 是
# 纯函数，它们与同组其它只读工具并行执行的结果和串行完全一致。
# 其余一律 False（按写操作串行）：write_file / apply_patch / run_bash 真的
# 会写；get_weather / analyze_image 虽不写工作区，但要出网/跨模型调用，
# 保守起见也不并行。Agent 的分组调度完全依据这份表（见 agent.py）。
# ---------------------------------------------------------------------------

TOOL_READ_ONLY = {
    "calculator": True,
    "current_time": True,
    "get_weather": False,
    "analyze_image": False,
}
TOOL_READ_ONLY.update(CODE_TOOL_READ_ONLY)  # 并入 coding 工具的标记（同样的合并方式）
TOOL_READ_ONLY.update(DOC_TOOL_READ_ONLY)   # 并入文档工具（create_doc 为非只读，走串行）
TOOL_READ_ONLY.update(ATTACH_TOOL_READ_ONLY)  # 并入附件工具（list/read_attachment 均只读）

def is_read_only(name: str) -> bool:
    """name 是否只读工具。未知工具返回 False——没有元数据就当写操作走串行，永远站在安全侧。"""
    return bool(TOOL_READ_ONLY.get(name))


def execute_tool(name: str, arguments: dict, ctx: ToolContext | None = None) -> str:
    """按名字执行工具。

    注意：工具报错时【不抛异常】，而是按统一失败信封 {ok:false, error, hint?}
    返回给 LLM——模型看到原因与建议才能自行纠正（换参数重试 / 换个工具 /
    直接告知用户），绝不静默失败。

    ctx：本次调用的执行上下文（工作区、图片、看图后端），由 Agent 注入。
    声明了 ctx 形参的工具（文件/命令/看图类）才拿到它；calculator 这类
    纯函数工具不声明、也不感知。ctx 不出现在 schema 里——"在哪个工作区
    干活"是会话属性，由服务端决定，不该是模型可填的参数。
    """
    func = TOOL_REGISTRY.get(name)
    if func is None:
        return error_result(f"未知工具：{name}", "确认工具名是否在系统提供的工具清单里（区分大小写）")
    try:
        if "ctx" in inspect.signature(func).parameters:
            return func(**arguments, ctx=ctx)
        return func(**arguments)
    except TypeError as e:  # 参数缺失/多传/类型不对：execute_tool 统一转成信封
        return error_result(f"参数不匹配: {e}", "对照本工具 schema 核对参数名与类型后重试")
    except Exception as e:
        return error_result(f"{type(e).__name__}: {e}", "执行失败，可调整参数重试或换用其它工具")


def describe_tools() -> str:
    """列出工具清单（启动横幅用）。"""
    return "\n".join(f"  - {s['function']['name']}: {s['function']['description']}" for s in TOOL_SCHEMAS)
