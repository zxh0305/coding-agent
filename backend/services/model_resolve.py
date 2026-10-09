"""激活模型 / 客户端 / 上下文窗口 / 视觉后端的解析链
==================================================

"当前该用哪个供应商 + 哪个模型"这件事的全部推导逻辑。解析链刻意设计为**只
回退、不改写**：会话自选失效 → 全局默认 → 第一个可用供应商；用户的显式选择
永不被静默吞掉。

从原 app.py 顶层原样搬移，函数体未作任何改动（纯搬移）。
"""

import json
import os

import db
from llm_client import create_client

# create_client 允许被装配层（app）覆盖：历史上测试通过 `app.create_client = ...`
# 打桩，而 app 曾把该名字留在自己命名空间里。拆分后本模块成了真正调用点，
# 为保持"打 app.create_client 即生效"的既有测试契约，装配层会把本模块的
# create_client 指向 app 的同名属性（见 app.install_service_hooks 的绑定）。


def _context_window_fallback() -> int:
    return int(os.environ.get("CONTEXT_WINDOW", "262144"))


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def _match_active(active: dict | None) -> tuple[dict, str] | None:
    """把一条 {provider_id, model} 记录解析成具体的 (供应商, 模型名)；失效返回 None。

    "失效"= 供应商被删或被禁用、或该供应商下一个启用的模型都没有。模型名本身
    失效（改名/删除）但供应商还在时不算失效：回落到该供应商第一个启用的模型。
    """
    if not active:
        return None
    prov = db.get_provider(str(active.get("provider_id", "")))
    if prov is None or not prov["enabled"]:
        return None
    enabled_names = [m["name"] for m in prov["models"] if m["enabled"]]
    if not enabled_names:
        return None
    model = active.get("model", "")
    return prov, (model if model in enabled_names else enabled_names[0])


def _resolve_active(sid: str | None = None) -> tuple[dict, str]:
    """当前激活模型 → (供应商, 模型名)。

    解析链：**会话自选模型 → 全局默认**（settings.active_model，"新会话的初始
    模型"）。会话自选失效时静默回落全局默认，全局也失效再退回第一个可用供应商。
    sid 为 None（页面刚打开、新任务还没建会话）时只看全局默认。
    """
    hit = (_match_active(db.get_session_model(sid) if sid else None)
           or _match_active(db.get_setting("active_model")))
    if hit:
        return hit
    # 最后一档：全局默认也失效（没设过 / 供应商被删被停用）时，退回第一个启用的
    # 供应商 + 它第一个启用的模型。模型名绝不能在这里留空——留空会让调用方
    # 误判成"一个可用模型都没有"而拒绝服务，而实际上此刻是有模型的。
    provs = [p for p in db.list_providers() if p["enabled"]] or db.list_providers()
    prov = provs[0] if provs else {"id": "", "name": "", "api_key": "", "base_url": "", "models": []}
    enabled = [m["name"] for m in prov.get("models", []) if m["enabled"]]
    return prov, (enabled[0] if enabled else "")


def _resolve_client(sid: str | None = None) -> tuple[dict, object, str, str, bool]:
    """按当前激活模型（含其供应商的 API 格式与视觉标记）构建客户端，
    返回 (prov, client, model, 指纹, 是否支持视觉)。sid = 按该会话的模型解析。"""
    prov, model = _resolve_active(sid)
    if not model:
        raise SystemExit("没有已启用的模型，请在网页「管理模型」里添加并启用")
    client = create_client(prov.get("api_format", "openai"),
                           api_key=prov["api_key"], base_url=prov["base_url"], model=model,
                           reasoning_replay=bool(prov.get("reasoning_replay")))
    vision = _model_vision(prov, model)
    sig = json.dumps([prov["id"], model, prov["base_url"], prov["api_key"], prov.get("api_format"), vision],
                     ensure_ascii=False)
    return prov, client, model, sig, vision


def _active_window(sid: str | None = None) -> int:
    """激活模型的上下文窗口。解析链：模型自填 → 供应商默认列 → .env/全局默认。
    窗口既是前端容量显示的分母，也是自动压缩触发线（估算超 80% 即压缩）的基准。"""
    prov, model = _resolve_active(sid)
    for m in prov["models"]:
        if m["name"] == model and m["context_window"]:
            return m["context_window"]
    if prov.get("context_window"):
        return prov["context_window"]
    return _context_window_fallback()


def _model_vision(prov: dict, model: str) -> bool:
    """某模型是否被用户标注为"支持视觉输入"。"""
    for m in prov["models"]:
        if m["name"] == model:
            return bool(m.get("vision"))
    return False


def _resolve_vision_model(sid: str | None = None) -> tuple[dict, str]:
    """找一位"替主模型看图"的视觉模型，选择链：
    1. 当前激活模型自己标注了视觉 → 直接用它；
    2. 否则借用任意已启用且标注了视觉的模型；
    3. 都没有 → RuntimeError（analyze_image 工具会转成可读的错误给主模型）。
    sid = 按该会话的主模型判断（会话模型与全局默认可能不是同一个）。
    """
    prov, model = _resolve_active(sid)
    if model and _model_vision(prov, model):
        return prov, model
    for p in db.list_providers():
        if not p["enabled"]:
            continue
        for m in p["models"]:
            if m["enabled"] and m.get("vision"):
                return p, m["name"]
    raise RuntimeError("没有任何模型被标注为「视觉」。请在「管理模型」面板给支持看图的模型勾选视觉。")


def _vision_backend(image_parts: list, question: str, sid: str | None = None) -> str:
    """analyze_image 工具的看图后端（tools.py 启动时注入）。
    image_parts 是 OpenAI 格式的 image_url content 部分。"""
    prov, model = _resolve_vision_model(sid)
    client = create_client(prov.get("api_format", "openai"),
                           api_key=prov["api_key"], base_url=prov["base_url"], model=model)
    message = {"role": "user", "content": [*image_parts, {"type": "text", "text": question}]}
    reply = client.chat([message])
    return (reply.get("content") or "").strip()


def _vision_backend_for(sid: str | None):
    """把看图后端绑定到某个会话后交给 Agent（工具层仍只认两个位置参数）。
    不同会话可挂着不同主模型，"是否需要借视觉模型"必须各按各的算。"""
    def backend(image_parts: list, question: str) -> str:
        return _vision_backend(image_parts, question, sid)
    return backend
