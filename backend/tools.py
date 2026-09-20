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
import json
import operator

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
    value = _safe_eval(ast.parse(expression.strip(), mode="eval"))
    # 演示约定：工具返回值统一是 JSON 字符串（LLM 读起来最稳定）
    return json.dumps({"expression": expression, "result": value}, ensure_ascii=False)


def current_time() -> str:
    """返回当前本地时间。"""
    now = datetime.datetime.now()
    return json.dumps(
        {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": "周" + "一二三四五六日"[now.weekday()]},
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
        return json.dumps({"error": f"没有 {city} 的天气数据（模拟库只收录：{'、'.join(_FAKE_WEATHER)}）"}, ensure_ascii=False)
    sky, temp, wind = _FAKE_WEATHER[city]
    return json.dumps(
        {"city": city, "weather": sky, "temperature": f"{temp}℃", "wind": wind, "note": "模拟数据"},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 第二部分：工具 schema（发给 LLM 的"说明书"，格式与 OpenAI 兼容接口一致）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算四则运算表达式。任何数学计算都必须使用本工具，不要自己心算。",
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
            "description": "获取当前的日期和时间。凡是涉及'现在几点'、'今天几号'的问题都用它。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市今天的天气。",
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

TOOL_REGISTRY = {
    "calculator": calculator,
    "current_time": current_time,
    "get_weather": get_weather,
}

# ---- 合并 Coding 工具（code_tools.py）：读写工作区文件、执行命令 ----
from code_tools import CODE_TOOL_REGISTRY, CODE_TOOL_SCHEMAS

TOOL_SCHEMAS += CODE_TOOL_SCHEMAS
TOOL_REGISTRY.update(CODE_TOOL_REGISTRY)

# ---- 图片识别工具（analyze_image）----
# 设计模式："用工具补偿模型短板"。主模型不支持视觉（或看不清细节）时，
# 由这个工具借一个标记了"视觉"的模型把图片转成文字描述。
#
# 依赖注入：工具层不该知道"有哪些模型、怎么构造客户端"，所以 app.py 启动时
# 通过 set_vision_backend() 注入一个看图函数 fn(image_parts, question) -> str。
# 工具需要的图片数据也不来自模型参数——模型看不见像素，图片列表由 Agent
# 在执行工具时注入（见 execute_tool 的 images 形参）。

_vision_backend = None  # fn(image_parts: list[dict], question: str) -> str
_current_images: list = []  # 当前用户消息附带的图片（Agent 每轮执行工具时注入）


def set_vision_backend(fn) -> None:
    global _vision_backend
    _vision_backend = fn


def analyze_image(image_id: str = "", question: str = "请详细描述这张图片的内容") -> str:
    images = _current_images or []
    if not images:
        return json.dumps({"error": "当前这条消息没有附带图片。请让用户重新上传图片后重试。"}, ensure_ascii=False)
    # image_id：'1'/'2'/... 按用户消息中图片出现顺序；空值默认第一张
    digits = "".join(ch for ch in str(image_id) if ch.isdigit())
    index = (int(digits) - 1) if digits else 0
    if index < 0 or index >= len(images):
        index = 0
    if _vision_backend is None:
        return json.dumps({"error": "图片识别后端未配置（系统内部问题，请联系服务部署者）"}, ensure_ascii=False)
    try:
        description = _vision_backend([images[index]], question)
    except RuntimeError as e:
        # 视觉模型调用失败：把原因交回主模型，让它告知用户怎么办
        return json.dumps({
            "error": f"视觉模型调用失败：{e}",
            "hint": "请在「管理模型」里给某个模型勾选'视觉'并确保其 Key 可用，然后重试。",
        }, ensure_ascii=False)
    return json.dumps({"image_id": str(index + 1), "description": description}, ensure_ascii=False)


TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": "识别/分析用户消息中附带的图片。当你（主模型）不支持视觉输入、看不清图片细节，"
                       "或需要读取图片中的文字时，调用此工具获取图片的文字描述。",
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


def execute_tool(name: str, arguments: dict, images: list | None = None) -> str:
    """按名字执行工具。

    注意：工具报错时【不抛异常】，而是把错误信息作为字符串返回给 LLM ——
    这样模型有机会看到错误并自行纠正（换参数重试 / 换个工具 / 直接告知用户）。

    images：当前用户消息里附带的图片（OpenAI content 部分格式），由 Agent 注入。
    模型看不见像素，analyze_image 这类"看图"工具全靠它拿数据。
    """
    global _current_images
    func = TOOL_REGISTRY.get(name)
    if func is None:
        return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
    try:
        if name == "analyze_image":
            _current_images = images or []
            return func(**arguments)
        result = func(**arguments)
    except Exception as e:  # 参数缺失、类型不对、算式非法……都统一吞掉转成 error
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    return result


def describe_tools() -> str:
    """列出工具清单（启动横幅用）。"""
    return "\n".join(f"  - {s['function']['name']}: {s['function']['description']}" for s in TOOL_SCHEMAS)
