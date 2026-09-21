"""
Web 服务
=========

把 Agent 包装成本地 HTTP API，并托管 frontend/ 目录下的静态页面。
纯标准库实现（http.server + sqlite3），无任何第三方依赖。

接口一览：
  GET  /                          前端聊天页面（静态文件托管）
  POST /api/auth/register         注册 {"username", "password"} → {token, username}
  POST /api/auth/login            登录 {"username", "password"} → {token, username}
  POST /api/auth/logout           退出登录（作废当前 token）
  GET  /api/auth/me               当前登录用户（校验 token 是否有效）
  GET  /api/config                当前激活模型信息（供应商/模型/Key 打码/上下文窗口）
  GET  /api/models                可用模型列表（各供应商已启用的模型，供工具栏选择）
  POST /api/active-model          切换激活模型 {"provider_id", "model"}
  GET  /api/providers             供应商列表（含模型，Key 打码）
  POST /api/providers/save        新建/更新供应商
  POST /api/providers/delete      删除供应商（"默认"供应商不可删）
  POST /api/providers/models/save 保存单个模型（新增/改名/窗口/启停）
  POST /api/providers/models/delete 删除模型
  POST /api/providers/test        测试链接：拿 Base URL/Key/模型 发一次真实请求
  GET  /api/workspace?session_id=  工作区路径（带 id = 该任务的；不带 = 用户默认，新任务将用的）
  POST /api/workspace               切换工作区 {"path", "session_id"?}（带 id 只改该任务；不带改用户默认）
  GET  /api/fs/dirs?path=         列出某目录的子目录（供选文件夹弹窗逐级浏览）
  GET  /api/sessions              任务列表（仅当前用户的）
  POST /api/sessions              提交输入，必要时创建任务 → 立即返回
                                    {session_id, nonce}（新任务的第一次发送走这里）
  POST /api/sessions/<id>/messages 提交输入（命令接口）：立即返回，回合在后台
                                    按会话锁串行执行，过程事件走 events 通道
  GET  /api/sessions/<id>/events?since=&token=  常驻事件流（SSE）：回合过程事件
                                    （delta/tool/done/…）的唯一出口。每事件带会话内
                                    自增 seq（SSE id: 行）；?since=/Last-Event-ID
                                    断线补发，缺口过大发 resync 让前端全量刷新。
                                    token 参数鉴权：EventSource 无法带自定义头
  GET  /api/sessions/<id>/messages  某任务的历史消息（须是自己的任务；?before_ord=&limit=
                                    向上翻页，默认最近 100 条；归档消息带 artifact/path/head）
  GET  /api/sessions/<id>/artifact?path=  读取外置归档消息的完整原文（路径白名单校验）
  DELETE /api/sessions?session_id=  删除任务（须是自己的任务）
  GET  /api/context?session_id=   该任务当前上下文容量
  POST /api/chat/stop             停止指定任务的生成 {"session_id"}

命令与事件解耦：POST 只入队（HTTP/1.0 时代的"每轮一个流"被替换掉），回合
由后台线程按会话锁串行执行，全部过程事件经统一发布口（events.SessionEvents
的 publish）进每会话一条的环形缓冲——常驻 SSE 连接、断线补发、多标签页
共用同一个真相。seq 是事件流水号（sessions.last_seq 持久化，重启不归零），
与消息的 mid 是两套身份。

登录与鉴权：除 /api/auth/* 外的所有接口要求 Authorization: Bearer <token>。
登录只做身份区分与会话隔离（各用户只看到自己的任务列表）；供应商/模型是
全局共享的——所有登录用户共用服务端配置的 LLM Key，也都能进"管理模型"面板。
工作区按任务隔离：每个任务可有自己的工作区（解析链：任务自选 → 用户默认
→ .env 的 WORKSPACE_DIR / 项目 workspace/），切换互不影响，并发任务不互踩。

数据存储：任务、消息、用户、供应商配置全部落盘在项目根目录的 agent_data.db
（SQLite，见 db.py）；重启不丢。Agent 进程内只缓存实例，历史按需从库里恢复。

运行：python3 backend/app.py [端口]     默认端口 8000
安全提示：服务监听 0.0.0.0（供局域网/外网访问），只有一层简单登录，
模型供应商/工作区等管理功能对所有登录用户开放，请只分享给信任的人；
若要暴露公网，建议再加反向代理与 HTTPS。
"""

import base64
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db
from agent import Agent
from code_tools import prepare_workspace
from events import SSE_HEARTBEAT, SessionEvents, sse_frame
from llm_client import create_client, load_env_file, save_env_values
from logger import setup_logging
from memory import memory_dir, recent_user_texts, run_extraction_async
from tools import TOOL_SCHEMAS

log = logging.getLogger("app")

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
ENV_FILE = PROJECT_DIR / ".env"

# Agent 实例缓存：sid -> (Agent, sig)。sig 是"激活供应商+模型+密钥"的指纹，
# 指纹变了（用户切换模型/改配置）就重建实例，但历史从数据库恢复，不丢对话。
_agents: dict[str, Agent] = {}
_sigs: dict[str, str] = {}
_ctx: dict[str, dict] = {}  # sid -> 最近一次上下文统计（非关键数据，只存内存）
_lock = threading.Lock()    # 全局锁：只保护上面的共享 dict 和模型配置的短临界区
_session_locks: dict[str, threading.Lock] = {}  # 每个任务一把锁（见 _session_lock）


def _session_lock(sid: str) -> threading.Lock:
    """按任务粒度加锁：同一任务同时只允许一个生成过程（防止并发写乱消息历史），
    不同任务互不阻塞。锁对象随首个并发请求创建并常驻（数量级 = 任务数，可接受）。
    刻意不在停止接口用这把锁——生成卡住时，停止请求必须还能进来。"""
    with _lock:
        return _session_locks.setdefault(sid, threading.Lock())


# 每会话一条事件总线（常驻事件流的真相源）。惰性创建：seq 从 sessions.last_seq
# 恢复（重启不归零），persist 回调把最新 seq 写回去。删除任务时整体摘除。
_buses: dict[str, SessionEvents] = {}


def _event_bus(sid: str) -> SessionEvents:
    with _lock:
        bus = _buses.get(sid)
        if bus is None:
            bus = SessionEvents(last_seq=db.get_last_seq(sid),
                                persist=lambda seq, _sid=sid: db.set_last_seq(_sid, seq))
            _buses[sid] = bus
        return bus


def _spawn_memory_extraction(sid: str, agent: Agent) -> None:
    """轮末记忆提取的启动点（必须在 worker 线程内、turn_end 发布之后调用）。

    触发点选在这里而不是 POST handler：POST 立即返回，那里没有"回答完成"
    时机；turn_end 发布时落盘已完成，此刻的额外工作不再影响本回合。启动
    线程前先把输入快照成不可变值（用户发言的纯字符串列表）——提取线程绝不
    共享 agent.history（下一个回合立刻会改它），也不持有会话锁（worker 要
    拿锁跑下一回合）。启动失败只进日志，绝不影响回合收尾。
    """
    try:
        texts = recent_user_texts(list(agent.history))  # 快照：线程内只用字符串
        # 传 chat() 方法本身（memory 约定 chat(messages, temperature=0) 可调用）
        run_extraction_async(sid, memory_dir(agent.ctx.workspace), agent.llm.chat, texts)
    except Exception:
        log.exception("[会话 %s] 记忆提取线程启动失败（忽略，不影响回合）", sid)


def _run_round(sid: str, agent: Agent, plain: str, user_message: dict,
               nonce: str, atts: list) -> None:
    """回合执行体（POST 只入队，真正的生成在这里跑）。

    会话锁串行同一任务的回合（多个 POST 排队时各自线程等锁，等价于旧
    "请求内执行"的排队语义）；所有过程事件走统一发布口 bus.publish——
    禁止第二条写入路径，旁路写入不会进缓冲，重连客户端永远看不到。

    事件协议：在原有回合事件（round/reasoning_delta/answer_delta/tool_call/
    tool_result/usage/done/compacted）外增加两个回合边界事件：
      turn_start {nonce, input, atts}  回合开始：多标签页/刷新后的页面靠它
                                       补画用户气泡并进入"生成中"状态；nonce
                                       让发起方识别自己（不重复画）
      turn_end   {user_mid}            回合结束（落盘已完成）：前端驱动排队队
                                       列推进的唯一信号；user_mid = 本回合输入
                                       消息的 mid，前端补发去重靠它识别
                                       "这回合已在时间线里"
    回答类事件额外带 mid（本轮回答段落的身份）：前端把同一 mid 的 delta
    归并进同一个气泡——与历史消息的 mid 同一体系，补发与时间线才能对上。
    """
    bus = _event_bus(sid)
    with _session_lock(sid):
        # 排队期间任务可能已被删除：直接放弃（会话行没了，落盘也会跳过）
        if db.session_owner(sid) is None:
            return
        try:
            bus.publish({"type": "turn_start", "nonce": nonce, "input": plain, "atts": atts})
            log.info("[会话 %s] 用户提问: %s", sid, plain)
            seg_mid = None  # 当前回答段落的 mid（每个 round 事件换一段）
            error = None
            try:
                for kind, payload in agent.run(plain, user_message):
                    if kind == "round":
                        seg_mid = uuid.uuid4().hex[:12]
                        bus.publish({"type": "round", "mid": seg_mid, **payload})
                    elif kind in ("answer_delta", "reasoning_delta", "done"):
                        bus.publish({"type": kind, "mid": seg_mid, **payload})
                    else:
                        bus.publish({"type": kind, **payload})
                    if kind in ("usage", "compacted"):  # 最新上下文容量，供 /api/context
                        _ctx[sid] = payload
                    if kind == "done":
                        log.info("[会话 %s] 最终回答: %s", sid, str(payload.get("answer"))[:200])
            except RuntimeError as e:
                log.exception("LLM 请求失败")
                error = str(e)
            except Exception:
                # 未预期异常也必须转成 error 事件：前端把 turn_end 当回合结束的
                # 唯一信号，线程无声死掉会让所有订阅页永远挂在"生成中"
                log.exception("回合执行出现未预期异常")
                error = "服务器内部错误，详情见 backend 日志"

            # 收尾（正常/停止/出错共用）：增量落盘 → 重编号信号 → turn_end。
            # 落盘在前、turn_end 在后——turn_end 里的 user_mid 是"这回合已可
            # 从时间线读到"的承诺，顺序反了前端去重会误判。
            written = db.save_messages(sid, agent.history, agent.saved)
            db.touch_session(sid)
            log.info("[会话 %s] 本轮落盘 %d 行", sid, written)
            if error is not None:
                db.renumbered_sessions.discard(sid)  # 错误路径不带重编号信号（与旧行为一致）
                bus.publish({"type": "error", "message": error})
            elif sid in db.renumbered_sessions:
                # 间隔耗尽兜底触发过整会话重编号：分页游标（before_ord 指向旧
                # 序号空间）全部失效，推事件让前端重拉时间线
                db.renumbered_sessions.discard(sid)
                bus.publish({"type": "history_renumbered"})
            user_mid = next((m.get("_mid") for m in reversed(agent.history)
                             if m.get("role") == "user"), None)
            bus.publish({"type": "turn_end", "user_mid": user_mid})
            # 轮末自动提取（memory.py）：daemon 线程异步跑，绝不阻塞 worker
            # 返回与下一个回合。全程静默——不发 SSE 事件、不写数据库：记忆是
            # 后台维护动作，用户无需感知，推送事件反而会进环形缓冲、打扰所有
            # 订阅页的时间线；失败只进日志，下轮自然重试。出错的回合不走这里
            # （内容不完整，避免把半截对话提炼成错误记忆），最终兜底路径同理。
            _spawn_memory_extraction(sid, agent)
        except Exception:
            # 最后一道兜底：连收尾都炸了也要把回合关掉，绝不挂起订阅页
            log.exception("[会话 %s] 回合线程收尾异常", sid)
            try:
                bus.publish({"type": "error", "message": "回合执行异常，详情见 backend 日志"})
                bus.publish({"type": "turn_end", "user_mid": None})
            except Exception:
                pass


def _context_window_fallback() -> int:
    return int(os.environ.get("CONTEXT_WINDOW", "262144"))


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def _resolve_active() -> tuple[dict, str]:
    """当前激活模型 → (供应商, 模型名)。设置失效时自动回退到第一个可用项。"""
    active = db.get_setting("active_model") or {}
    prov = db.get_provider(active.get("provider_id", ""))
    model = active.get("model", "")
    if prov is None or not prov["enabled"]:
        provs = [p for p in db.list_providers() if p["enabled"]]
        prov = provs[0] if provs else db.list_providers()[0]
        model = ""
    enabled_names = [m["name"] for m in prov["models"] if m["enabled"]]
    if model not in enabled_names:
        model = enabled_names[0] if enabled_names else ""
    return prov, model


def _resolve_client() -> tuple[object, str, str, bool]:
    """按当前激活模型（含其供应商的 API 格式与视觉标记）构建客户端，
    返回 (client, model, 指纹, 是否支持视觉)。"""
    prov, model = _resolve_active()
    if not model:
        raise SystemExit("没有已启用的模型，请在网页「管理模型」里添加并启用")
    client = create_client(prov.get("api_format", "openai"),
                           api_key=prov["api_key"], base_url=prov["base_url"], model=model)
    vision = _model_vision(prov, model)
    sig = json.dumps([prov["id"], model, prov["base_url"], prov["api_key"], prov.get("api_format"), vision],
                     ensure_ascii=False)
    return client, model, sig, vision


def _active_window() -> int:
    """激活模型的上下文窗口。解析链：模型自填 → 供应商默认列 → .env/全局默认。
    窗口既是前端容量显示的分母，也是自动压缩触发线（估算超 80% 即压缩）的基准。"""
    prov, model = _resolve_active()
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


def _resolve_vision_model() -> tuple[dict, str]:
    """找一位"替主模型看图"的视觉模型，选择链：
    1. 当前激活模型自己标注了视觉 → 直接用它；
    2. 否则借用任意已启用且标注了视觉的模型；
    3. 都没有 → RuntimeError（analyze_image 工具会转成可读的错误给主模型）。
    """
    prov, model = _resolve_active()
    if model and _model_vision(prov, model):
        return prov, model
    for p in db.list_providers():
        if not p["enabled"]:
            continue
        for m in p["models"]:
            if m["enabled"] and m.get("vision"):
                return p, m["name"]
    raise RuntimeError("没有任何模型被标注为「视觉」。请在「管理模型」面板给支持看图的模型勾选视觉。")


def _vision_backend(image_parts: list, question: str) -> str:
    """analyze_image 工具的看图后端（tools.py 启动时注入）。
    image_parts 是 OpenAI 格式的 image_url content 部分。"""
    prov, model = _resolve_vision_model()
    client = create_client(prov.get("api_format", "openai"),
                           api_key=prov["api_key"], base_url=prov["base_url"], model=model)
    message = {"role": "user", "content": [*image_parts, {"type": "text", "text": question}]}
    reply = client.chat([message])
    return (reply.get("content") or "").strip()


MAX_IMAGE_B64 = 6_000_000   # 单张图片 base64 长度上限（约 4.5MB 原图）
MAX_TEXT_FILE = 200_000     # 文本附件解码后的字符上限


def _build_user_message(body: dict) -> tuple[str, dict | None]:
    """把 {message, attachments} 组装成 OpenAI 格式的用户消息。

    图片 → image_url 视觉输入（需要所用模型支持视觉）；
    文本文件 → 解码后以代码块注入消息正文，模型直接"读"到内容。
    返回 (纯文本预览, 完整消息)；预览用于任务标题。
    """
    text = (body.get("message") or "").strip()
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    names, file_notes = [], []
    for att in (body.get("attachments") or [])[:6]:
        name = str(att.get("name") or "file").replace("\n", " ")[:80]
        data = str(att.get("data") or "")
        if att.get("kind") == "image":
            if not data:
                continue
            if len(data) > MAX_IMAGE_B64:
                raise ValueError(f"图片 {name} 超过 4MB 限制")
            mime = att.get("mime") if str(att.get("mime", "")).startswith("image/") else "image/png"
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
            names.append(name)
        else:
            try:
                file_text = base64.b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                continue
            if len(file_text) > MAX_TEXT_FILE:
                file_text = file_text[:MAX_TEXT_FILE] + "\n...[文件过长已截断]"
            file_notes.append(f"### 附件文件：{name}\n```\n{file_text}\n```")
            names.append(name)
    if file_notes:
        parts.append({"type": "text", "text": "用户附带了以下文件内容：\n\n" + "\n\n".join(file_notes)})
    if not parts:
        return "", None
    plain = text or ("[附件] " + "、".join(names))
    if len(parts) == 1 and parts[0]["type"] == "text":
        return plain, {"role": "user", "content": parts[0]["text"]}
    return plain, {"role": "user", "content": parts}


def _resolve_workspace(user_id: int, sid: str) -> Path:
    """解析一个任务的工作区，优先级：任务自选 → 用户默认（新任务继承）→ .env/项目默认。

    工作区按任务隔离的关键：每个任务在构建 Agent 时各自解析，结果写进该任务
    的 ToolContext；切换某个任务的工作区不再影响其他任务（此前是全局环境变量）。
    sid 为空（前端还没开任务）时返回该用户的默认工作区，供工具栏展示。
    """
    ws = db.get_session_workspace(sid) if sid else None
    if not ws:
        ws = db.get_setting(f"default_workspace:{user_id}")
    return prepare_workspace(ws)


def get_session(session_id, user_id: int) -> tuple[str, Agent]:
    """取回（或创建）一个任务会话，返回 (id, Agent)。历史缺失时从数据库恢复。

    任务归属校验：传入的 session_id 必须存在且属于该用户，否则一律开新任务
    （防止拿着别人的任务 id 读/写别人的对话）。

    注意顺序：先解析客户端、确认模型可用，再创建会话行——否则模型配置有问题时
    会在数据库里留下"零消息、空标题"的孤儿任务。
    """
    client, model, sig, vision = _resolve_client()

    sid = None
    if isinstance(session_id, str) and session_id:
        # 任务是否存在、是否归当前用户，都以数据库为准
        if db.session_owner(session_id) == user_id:
            sid = session_id
    if sid is None:
        sid = uuid.uuid4().hex[:8]
        db.create_session(sid, user_id)
    workspace = _resolve_workspace(user_id, sid)
    # 工作区纳入配置指纹：任务的工作区被切换后，下次构建会重建 Agent（历史照旧从库里恢复）
    sig += "|" + str(workspace)

    # 锁只保护实例缓存的读写这一瞬间。生成过程可能持续几分钟，绝不能全程
    # 持锁——否则一条慢请求会把所有提问/删除/停止请求全部卡死（真实踩过的坑）。
    with _lock:
        if sid not in _agents or _sigs.get(sid) != sig:
            agent = Agent(llm=client, verbose=False, vision_supported=vision,
                          workspace=workspace, vision_backend=_vision_backend,
                          context_window=_active_window(),  # 压缩触发线的基准（切换模型后重建实例即更新）
                          artifact_reader=db.read_artifact)  # 外置大消息的还原器（模型视图用）
            # 窗口恢复：从未压缩 = 全量；压缩过 = 锚点 + 最后一条边界及其之后
            # （边界摘要是后续再压缩的输入）。内存占用与当前窗口成正比，而非
            # 全会话长度；模型视图与全量恢复逐字节一致。
            total = db.count_messages(sid)
            agent.history = db.restore_window(sid)
            # 增量落盘的指纹账本从恢复的历史重建（只覆盖窗口内即可——窗口外
            # 的行不会被 save_messages 触碰），重启后第一轮就是纯增量写。
            agent.saved = db.fingerprints(agent.history)
            _agents[sid] = agent
            _sigs[sid] = sig
            log.info("会话 %s Agent 就绪 model=%s 工作区=%s（恢复 %d/%d 条，视觉=%s）",
                     sid, model, workspace, len(agent.history), total, vision)
        return sid, _agents[sid]


class Handler(SimpleHTTPRequestHandler):
    """API 路由 + 静态文件托管（frontend/ 目录）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def log_message(self, fmt, *args):  # 访问日志降级为 DEBUG，不刷屏
        log.debug("HTTP %s", fmt % args)

    def end_headers(self):
        # 禁止缓存：浏览器对静态文件有启发式缓存，曾出现"新 index.html 配旧 app.js"
        # 的版本错配——旧脚本在新页面上找不到元素，执行中断，所有按钮集体失灵。
        # 本地开发页面每次都重新验证（文件没变时服务端回 304，开销极小）。
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    # ---------- 鉴权 ----------

    def _auth_user(self) -> dict | None:
        """从 Authorization: Bearer <token> 解析当前用户；无效返回 None。"""
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return db.user_for_token(header[7:].strip())

    def _require_auth(self) -> bool:
        """统一入口鉴权：/api/auth/* 放行，其余 /api/* 必须带有效 token。

        静态文件（前端页面本身）不拦——页面得先打开才能登录。
        例外：events 端点额外接受 ?token= 查询参数鉴权——浏览器原生
        EventSource 不支持自定义请求头，Authorization 带不进去。只对
        events 开这一个口子：token 出现在 URL 里存在被代理日志记录的
        暴露面，能窄则窄。
        通过后把用户挂在 self.user 上，后续接口直接用。
        """
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            self.user = None
            return True
        user = self._auth_user()
        if user is None and path.endswith("/events"):
            tok = (self._query().get("token") or [""])[0]
            if tok:
                user = db.user_for_token(tok)
        if user is None:
            self._json({"error": "未登录或登录已失效", "code": "unauthorized"}, 401)
            return False
        self.user = user
        return True

    # ---------- 响应工具 ----------

    def _json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _query(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _config_view(self) -> dict:
        prov, model = _resolve_active()
        return {
            "provider_id": prov["id"],
            "provider_name": prov["name"],
            "model": model,
            "api_key_masked": _mask(prov["api_key"]),
            "context_window": _active_window(),
            "vision": _model_vision(prov, model),  # 激活模型是否支持看图（前端附件提示用）
        }

    # ---------- 路由 ----------

    def do_GET(self):
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path  # 剥掉 ?query 后的纯路径
        if path == "/api/auth/me":
            user = self._auth_user()   # auth 路径不走 _require_auth，这里自己解析
            if user is None:
                return self._json({"error": "未登录"}, 401)
            self._json({"username": user["username"]})
        elif self.path == "/api/config":
            self._json(self._config_view())
        elif self.path == "/api/models":
            models = []
            for p in db.list_providers():
                if not p["enabled"]:
                    continue
                for m in p["models"]:
                    if m["enabled"]:
                        models.append({"provider_id": p["id"], "provider_name": p["name"],
                                       "model": m["name"], "context_window": m["context_window"]})
            self._json({"models": models, "active": _resolve_active()[1],
                        "active_provider": _resolve_active()[0]["id"]})
        elif self.path == "/api/providers":
            provs = []
            for p in db.list_providers():
                provs.append({**p, "api_key": None, "api_key_masked": _mask(p["api_key"])})
            self._json(provs)
        elif self.path.startswith("/api/workspace"):
            # 带当前任务 id 时返回该任务的工作区；不带 = 用户默认（新任务将用的）
            sid = (self._query().get("session_id") or [""])[0]
            if sid and db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            self._json({"path": str(_resolve_workspace(self.user["id"], sid))})
        elif self.path.startswith("/api/fs/dirs"):
            self._handle_fs_dirs()
        elif self.path == "/api/tools":
            self._json({"tools": [
                {"name": t["function"]["name"],
                 "description": t["function"]["description"],
                 "parameters": t["function"]["parameters"]}
                for t in TOOL_SCHEMAS
            ]})
        elif self.path == "/api/sessions":
            self._json(db.list_sessions(self.user["id"]))
        elif path.startswith("/api/sessions/"):  # /api/sessions/<id>/messages|artifact
            parts = [p for p in path.split("/") if p]  # ["api","sessions",<id>,<子资源>]
            sid = parts[2] if len(parts) > 2 else ""
            if not sid or db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            sub = parts[3] if len(parts) > 3 else ""
            if sub == "artifact":
                return self._handle_session_artifact(sid)
            if sub == "events":
                return self._handle_session_events(sid)
            return self._handle_session_messages(sid)
        elif self.path.startswith("/api/context"):
            sid = (self._query().get("session_id") or [""])[0]
            if sid and db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            stat = _ctx.get(sid, {})
            self._json({
                "tokens": stat.get("prompt_tokens", 0),
                "window": _active_window(),
                "breakdown": stat.get("context"),
                "cache_hit_rate": stat.get("cache_hit_rate"),
            })
        elif self.path.startswith("/api/"):
            self._json({"error": "未知接口"}, 404)
        else:
            super().do_GET()  # 其余路径一律当静态文件

    def do_POST(self):
        try:
            if self.path.startswith("/api/auth/"):
                return self._handle_auth(self.path.rsplit("/", 1)[-1])
            if not self._require_auth():
                return
            path = urllib.parse.urlparse(self.path).path  # 剥掉 ?query 再匹配
            if path == "/api/chat/stop":
                self._handle_chat_stop()
            elif path == "/api/sessions":
                # 新任务的第一次发送：创建任务 + 入队（老任务每次都带 id 走下面）
                self._handle_session_submit(None)
            elif re.fullmatch(r"/api/sessions/[^/]+/messages", path):
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_session_submit(sid)
            elif path == "/api/active-model":
                self._handle_active_model()
            elif self.path == "/api/providers/save":
                self._handle_provider_save()
            elif self.path == "/api/providers/delete":
                self._handle_provider_delete()
            elif self.path == "/api/providers/models/save":
                self._handle_model_save()
            elif self.path == "/api/providers/models/delete":
                self._handle_model_delete()
            elif self.path == "/api/providers/test":
                self._handle_provider_test()
            elif self.path == "/api/workspace":
                self._handle_workspace_set()
            else:
                self._json({"error": "未知接口"}, 404)
        except json.JSONDecodeError as e:
            self._json({"error": f"请求体不是合法 JSON：{e}"}, 400)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except SystemExit as e:
            self._json({"error": str(e).strip() or "配置缺失"}, 400)
        except Exception:
            log.exception("接口处理出错")  # 完整堆栈进 agent.log
            self._json({"error": "服务器内部错误，详情见 backend 日志"}, 500)

    def do_DELETE(self):
        if not self._require_auth():
            return
        if urllib.parse.urlparse(self.path).path == "/api/sessions":  # self.path 带 ?query，须剥掉再比较
            sid = (self._query().get("session_id") or [""])[0]
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            db.delete_session(sid)
            with _lock:
                _agents.pop(sid, None)
                _ctx.pop(sid, None)
                bus = _buses.pop(sid, None)
            if bus is not None:
                # 先发 session_deleted 再关总线：其他标签页的常驻连接收到后
                # 自行收摊（切走/清空界面），close 的哨兵再把连接线程送终
                bus.publish({"type": "session_deleted"})
                bus.close()
            log.info("删除会话 %s", sid)
            self._json({"ok": True})
        else:
            self._json({"error": "未知接口"}, 404)

    # ---------- 登录/注册 ----------

    def _handle_auth(self, action: str):
        """register / login / logout / me。token 存数据库，服务重启不掉线。"""
        if action == "me":  # GET 已单独处理，这里是其他动词的兜底
            user = self._auth_user()
            if user is None:
                return self._json({"error": "未登录"}, 401)
            return self._json({"username": user["username"]})
        body = self._body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if action == "logout":
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                db.delete_token(header[7:].strip())
            return self._json({"ok": True})
        if not re.fullmatch(r"[\w\u4e00-\u9fff.-]{1,24}", username):
            return self._json({"error": "用户名须为 1-24 位字母/数字/下划线/中文/点/横线"}, 400)
        if len(password) < 4 or len(password) > 64:
            return self._json({"error": "密码长度须为 4-64 位"}, 400)
        if action == "register":
            try:
                user = db.create_user(username, password)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            if db.count_users() == 1:  # 第一个注册的用户认领升级前的无主旧任务
                claimed = db.claim_orphan_sessions(user["id"])
                if claimed:
                    log.info("升级前 %d 个旧任务已划归首个用户 %s", claimed, username)
        elif action == "login":
            user = db.get_user_by_name(username)
            if user is None or not db.verify_password(password, user["password_hash"]):
                return self._json({"error": "用户名或密码错误"}, 401)
        else:
            return self._json({"error": "未知接口"}, 404)
        token = db.create_token(user["id"])
        log.info("用户 %s %s成功", username, "注册并登录" if action == "register" else "登录")
        self._json({"token": token, "username": user["username"]})

    # ---------- 问答：命令接口（POST 立即返回）+ 常驻事件流（GET events） ----------

    def _handle_session_submit(self, sid_or_none):
        """提交输入的命令接口：创建/复用任务，立即返回，不在本请求内流式输出。

        回合真正跑在后台线程里（_run_round），按会话锁串行——同一任务并发
        提交自然排队，不同任务并行。POST 返回 {session_id, nonce}：
        session_id 告知前端新任务的 id；nonce 是本客户端生成的回令，回合
        开始事件（turn_start）会原样带回——前端靠它识别"这回合是我发的"，
        不重复渲染自己刚画的用户气泡（其他标签页/刷新后的页面没有这个
        nonce，会按事件里的原文补画）。
        """
        body = self._body()
        plain, user_message = _build_user_message(body)
        if not plain:
            return self._json({"error": "输入不能为空"}, 400)
        # get_session 内部只短持锁（见其注释）；这里不持全局锁——回合已不在
        # 本请求内执行，本接口本身是毫秒级返回的。
        sid, agent = get_session(sid_or_none, self.user["id"])
        title = next((s["title"] for s in db.list_sessions(self.user["id"]) if s["id"] == sid), "")
        if not title:
            db.set_session_title(sid, plain[:24])
        # 附件只带 kind/name 进 turn_start 事件（原文/图片数据太大，不该进
        # 环形缓冲占 500 个格子里的一个——完整内容在消息存储里）
        atts = [{"kind": a.get("kind"), "name": str(a.get("name") or "")[:80]}
                for a in (body.get("attachments") or [])[:6]]
        nonce = str(body.get("nonce") or uuid.uuid4().hex[:12])
        threading.Thread(target=_run_round, daemon=True,
                         args=(sid, agent, plain, user_message, nonce, atts)).start()
        self._json({"session_id": sid, "nonce": nonce})

    def _handle_session_events(self, sid: str):
        """常驻事件流（SSE）。每连接占一个线程（ThreadingHTTPServer 每请求
        一线程，前置检查已确认），客户端断开即线程收尾。

        连接生命周期（顺序是正确性的一部分）：
        1) 先订阅、后快照：两步之间新发布的事件会同时出现在订阅队列和补发
           快照里，用"seq ≤ 已写出的最大 seq 则跳过"闸门去重。反过来（先
           快照后订阅）快照与订阅之间的事件会两头都够不着——漏事件；
        2) 按 replay_plan 补发（events.py，纯逻辑有单测）：缺口补不齐时发
           resync 事件（带当前 seq 作重连锚点）并照常续流——客户端收到
           resync 会主动断开、全量刷新、以该 seq 重连；
        3) caught_up 标记补发段结束（不带 id，不污染 Last-Event-ID）。前端
           靠它把缓冲住的补发事件一次性定性：已在时间线里的完整回合跳过，
           进行中的回合从 turn_start 起渲染；
        4) 实时段：订阅队列 15 秒无事件就写心跳注释行（: ping）。Cloudflare
           等隧道会掐空闲连接，心跳让它们保持存活；心跳不进缓冲不占 seq
           （哪些进缓冲见 events.py 的角色表）。

        since 的两个来路，Last-Event-ID 头优先于 ?since= 参数：浏览器自动
        重连会带头（值 = 最后收到的事件 id，最新鲜）；页面刷新/切换任务走
        参数（来自 localStorage）。保留 HTTP/1.0 + Connection close + 禁
        缓存的既有措施——代理不缓冲、连接语义简单，与心跳配合防超时。
        """
        bus = _event_bus(sid)
        sub = bus.subscribe()  # 先订阅（见上）
        try:
            position = None
            header_id = (self.headers.get("Last-Event-ID") or "").strip()
            if header_id:
                try:
                    position = int(header_id)
                except ValueError:
                    position = None
            if position is None:
                raw = (self._query().get("since") or [""])[0]
                if raw not in ("", None):
                    try:
                        position = int(raw)
                    except ValueError:
                        return self._json({"error": "since 须为整数"}, 400)
            mode, items = bus.replay_plan(position)

            self.protocol_version = "HTTP/1.0"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            if mode == "resync":
                # 带当前 seq 作锚点：客户端刷新后以它重连，重连与刷新之间
                # 新发生的事件仍在缓冲里，可从锚点补回
                self.wfile.write(sse_frame(None, {"type": "resync", "seq": bus.current_seq}))
            # last_written 取实际写出的第一条 seq-1：回合扩展补发会故意从
            # turn_start 起重发客户端已应用过的段落（页面刷新场景需要完整
            # 回合开头），去重闸门由客户端按它自己的渲染记录做，这里只管
            # 网络层不重复写同一条
            last_written = (items[0][0] - 1) if items else (position if position is not None else 0)
            for seq, event in items:
                self.wfile.write(sse_frame(seq, event))
                last_written = seq
            self.wfile.write(sse_frame(None, {"type": "caught_up", "running": bus.running}))
            while True:
                try:
                    item = sub.get(timeout=15)
                except Exception:  # queue.Empty：15 秒无事件
                    self.wfile.write(SSE_HEARTBEAT)  # 心跳只在网络连接上（见 events.py）
                    continue
                if item is None:
                    break  # 会话被删除（bus.close 的哨兵）：结束连接
                seq, event = item
                if seq <= last_written:
                    continue  # 订阅队列与补发快照的重叠段
                self.wfile.write(sse_frame(seq, event))
                last_written = seq
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端断开/刷新：常驻连接的常态（每 15 秒心跳也会在连接死后
            # 抛出），安静收尾。回合不受影响——事件进缓冲，重连可补
            pass
        finally:
            bus.unsubscribe(sub)

    def _handle_chat_stop(self):
        """停止指定任务的生成：给 Agent 的停止开关置位。

        看护线程随即掐断 LLM 连接，agent.run 带着已生成的部分内容收尾，
        事件流上照常收到 done(stopped=true) + turn_end。这里刻意不拿全局锁——
        恰恰是生成卡住时，停止请求必须还能进来。
        """
        sid = str(self._body().get("session_id") or "")
        if not sid or db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        agent = _agents.get(sid)
        if agent is None or agent.cancel_event is None or agent.cancel_event.is_set():
            return self._json({"ok": True, "running": False})
        agent.stop()
        log.info("[会话 %s] 用户请求停止生成", sid)
        self._json({"ok": True, "running": True})

    # ---------- 任务消息（分页回放 + 归档全文） ----------

    def _handle_session_messages(self, sid: str):
        """历史消息分页：默认最近 100 条，before_ord 向上翻页。

        compact = 上下文压缩边界（role="compact"）：前端渲染"以上已压缩"分隔
        卡片用；summary 原文随消息带回，点开可查。外置归档消息（_artifact）
        行内没有正文——只带 head 预览与 path/bytes，前端渲染"内容过大已归档"
        标记，点"查看全文"再走 artifact 接口按需取回。
        """
        qs = self._query()
        try:
            limit = min(500, max(1, int(qs.get("limit", ["100"])[0])))
        except ValueError:
            limit = 100
        raw_before = qs.get("before_ord", [None])[0]
        try:
            before_ord = int(raw_before) if raw_before is not None else None
        except (TypeError, ValueError):
            return self._json({"error": "before_ord 须为整数"}, 400)
        msgs = db.get_messages(sid, before_ord=before_ord, limit=limit)
        items = []
        for m in msgs:
            role = m.get("role")
            if role not in ("user", "assistant", "compact"):
                continue  # 工具消息/带 tool_calls 的中间 assistant 不进时间线
            # mid 一并带回：它是事件流（delta/done/turn_end.user_mid）与时间线
            # 之间的关联键——前端补发去重（"这回合已在时间线里"）靠它比对
            if m.get("_artifact"):
                items.append({"role": role, "content": "", "ord": m["_ord"], "mid": m["_mid"],
                              "artifact": True, "path": m.get("path"),
                              "bytes": m.get("bytes"), "head": m.get("head"),
                              "stats": m.get("_stats")})
            elif m.get("content"):
                # 助手消息带回耗时/token 统计（回放渲染用，来自 message_usage 表）
                items.append({"role": role, "content": m["content"], "ord": m["_ord"],
                              "mid": m["_mid"], "stats": m.get("_stats")})
        # has_more：本页最小 ord 之前还有更早的消息（向上翻页入口的显隐依据）
        has_more = bool(items) and db.has_messages_before(sid, items[0]["ord"])
        self._json({"messages": items, "has_more": has_more})

    def _handle_session_artifact(self, sid: str):
        """读取本任务外置归档的完整消息。路径校验双保险：
        1. db.read_artifact 的 realpath 白名单（防 ../、绝对路径、非 .json）；
        2. 路径首段必须是本任务 id——任务归属虽已在上层校验，但 path 参数本身
           还能指向别家任务的归档，这里一并拦死。"""
        rel = (self._query().get("path") or [""])[0]
        if not rel or not rel.startswith(f"{sid}/"):
            return self._json({"error": "非法的归档路径"}, 400)
        try:
            msg = db.read_artifact(rel)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except (OSError, json.JSONDecodeError):
            return self._json({"error": "归档文件缺失或损坏"}, 404)
        self._json({"message": msg})

    # ---------- 模型供应商 ----------

    def _handle_active_model(self):
        body = self._body()
        pid, model = body.get("provider_id"), body.get("model")
        prov = db.get_provider(str(pid)) if pid else None
        if prov is None:
            return self._json({"error": "供应商不存在"}, 400)
        if model not in [m["name"] for m in prov["models"]]:
            return self._json({"error": f"供应商 {prov['name']} 下没有模型 {model}"}, 400)
        db.set_setting("active_model", {"provider_id": prov["id"], "model": model})
        log.info("激活模型切换为 %s / %s", prov["name"], model)
        self._json({"ok": True, **self._config_view()})

    def _handle_provider_save(self):
        b = self._body()
        name = (b.get("name") or "").strip()
        base_url = (b.get("base_url") or "").strip()
        api_format = (b.get("api_format") or "openai").strip()
        if api_format not in ("openai", "anthropic"):
            return self._json({"error": f"不支持的 API 格式：{api_format}（支持 openai / anthropic）"}, 400)
        if not name or not base_url:
            return self._json({"error": "名称和 Base URL 不能为空"}, 400)
        pid = (b.get("id") or "").strip() or uuid.uuid4().hex[:6]
        exists = db.get_provider(pid) is not None
        api_key = (b.get("api_key") or "").strip() or None  # None/空 = 保持原 key
        # 供应商级默认窗口：不传（None）= 保持原值；窗口解析链"模型自填 → 这里 → .env"
        try:
            prov_window = int(b.get("context_window")) if b.get("context_window") else None
        except (TypeError, ValueError):
            return self._json({"error": "上下文窗口须为整数（token 数）"}, 400)
        db.upsert_provider(pid, name, base_url, api_key, bool(b.get("enabled", True)),
                           api_format, context_window=prov_window)
        for m in b.get("models") or []:  # 可选：创建时一并带模型列表
            if (m.get("name") or "").strip():
                db.upsert_model(pid, m["name"].strip(), int(m.get("context_window") or 262144),
                                bool(m.get("enabled", True)), bool(m.get("vision", False)))
        # "默认"供应商的改动同步回 .env，保证命令行版（读 .env）一致
        if pid == "default" and exists:
            prov = db.get_provider(pid)
            save_env_values({
                "LLM_BASE_URL": prov["base_url"],
                "LLM_API_KEY": prov["api_key"],
            }, str(ENV_FILE))
        db.ensure_active_model()  # 激活模型被改名/删除时自动纠正，避免指向失效模型
        log.info("供应商已保存: %s (%s)", name, pid)
        self._json({"ok": True, "id": pid})

    def _handle_provider_delete(self):
        pid = (self._body().get("id") or "").strip()
        if pid == "default":
            return self._json({"error": "默认供应商不可删除，只能停用"}, 400)
        db.delete_provider(pid)
        db.ensure_active_model()
        log.info("供应商已删除: %s", pid)
        self._json({"ok": True})

    def _handle_model_save(self):
        b = self._body()
        pid, name = (b.get("provider_id") or "").strip(), (b.get("name") or "").strip()
        if db.get_provider(pid) is None:
            return self._json({"error": "供应商不存在"}, 400)
        if not name:
            return self._json({"error": "模型名不能为空"}, 400)
        vision = b.get("vision")
        db.upsert_model(pid, name, int(b.get("context_window") or 262144),
                        bool(b.get("enabled", True)),
                        None if vision is None else bool(vision))
        db.ensure_active_model()
        self._json({"ok": True})

    def _handle_model_delete(self):
        b = self._body()
        db.delete_model((b.get("provider_id") or "").strip(), (b.get("name") or "").strip())
        db.ensure_active_model()
        self._json({"ok": True})

    def _handle_provider_test(self):
        """测试链接：用表单里的协议格式 / Base URL / Key / 模型名发一次真实的 ping 请求。"""
        b = self._body()
        base = (b.get("base_url") or "").strip()
        model = (b.get("model") or "").strip()
        api_format = (b.get("api_format") or "openai").strip()
        api_key = (b.get("api_key") or "").strip()
        if not api_key and b.get("provider_id"):  # Key 留空 = 用已保存的
            prov = db.get_provider(b["provider_id"])
            api_key = prov["api_key"] if prov else ""
        if not (base and model and api_key):
            return self._json({"ok": False, "error": "Base URL、模型名、API Key 缺一不可（Key 留空则用已保存的）"})
        client = create_client(api_format, api_key=api_key, base_url=base, model=model, timeout=20)
        start = time.time()
        try:
            msg = client.chat([{"role": "user", "content": "只回复：ok"}])
            latency = round((time.time() - start) * 1000)
            content = (msg.get("content") or "").strip()[:60]
            log.info("测试链接成功 %s (%dms)", base, latency)
            self._json({"ok": True, "latency_ms": latency, "reply": content})
        except RuntimeError as e:
            self._json({"ok": False, "error": str(e)})

    # ---------- 工作区 ----------

    def _handle_workspace_set(self):
        """切换工作区（按任务隔离，替代曾经的"全局环境变量 + 写回 .env"）：
        - 带 session_id：只改该任务的工作区（须是自己的任务）；
        - 不带：改当前用户的"新任务默认工作区"。
        正在生成的任务拒绝切换——生成中的 Agent 仍持有旧工作区，切了也要等
        下一条消息才生效，用户容易误以为切失败。
        """
        b = self._body()
        path = str(b.get("path") or "").strip()
        sid = str(b.get("session_id") or "").strip()
        target = Path(path).expanduser() if path else None
        if target is None or not target.is_dir():
            return self._json({"error": "目录不存在或不可用"}, 400)
        target = target.resolve()
        if target == Path(target.root):  # Path 与 str 比较为 False，必须同类型比较——
            return self._json({"error": "不能选择文件系统根目录"}, 400)  # 此前这里用 target.root 裸比，从未拦住过
        if sid:
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            with _lock:
                lock = _session_locks.get(sid)
                if lock is not None and lock.locked():
                    return self._json({"error": "该任务正在生成，请等回答结束再切换工作区"}, 409)
                _agents.pop(sid, None)  # 丢弃旧实例：下一条消息用新工作区重建（历史从库里恢复）
            db.set_session_workspace(sid, str(target))
            log.info("[会话 %s] 工作区切换为 %s", sid, target)
        else:
            db.set_setting(f"default_workspace:{self.user['id']}", str(target))
            log.info("用户 %s 的新任务默认工作区: %s", self.user["username"], target)
        self._json({"ok": True, "path": str(target), "scope": "session" if sid else "default"})

    def _handle_fs_dirs(self):
        """列出某个目录下的子目录，供前端"选择工作区"弹窗逐级浏览（起点：用户主目录）。"""
        qs = self._query()
        target = Path(qs.get("path", [str(Path.home())])[0]).expanduser()
        if not target.is_dir():
            return self._json({"error": "目录不存在"}, 400)
        dirs = sorted(
            (e.name for e in target.iterdir() if e.is_dir()),
            key=lambda n: n.startswith("."),  # 普通目录在前，隐藏目录靠后
        )
        parent = target.parent
        self._json({
            "path": str(target),
            "parent": str(parent) if parent != target else None,
            "home": str(Path.home()),
            "dirs": dirs,
        })


def _lan_ips() -> list[str]:
    """本机非回环 IPv4 地址（用于启动时提示可分享的访问地址）。拿不到就返回空。"""
    import socket
    ips = set()
    try:
        # 连一个外部地址（不真发包），让系统选一条路由，从而得到本机出口 IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def main():
    load_env_file(str(ENV_FILE))   # 读 .env（首库播种 / 默认供应商 / 默认工作区用）
    db.init_db()                   # 建表 + 播种（已初始化则跳过）
    log_file = setup_logging(console=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Web 服务启动 0.0.0.0:%d（数据文件 %s）", port, db.DB_PATH)
    print(f"🤖 Agent Demo 已启动:  http://127.0.0.1:{port}   （数据: {db.DB_PATH.name}，日志: {log_file}，Ctrl+C 退出）", flush=True)
    for ip in _lan_ips():
        print(f"   局域网/外网访问:  http://{ip}:{port}   （已开启用户登录，模型 Key 全局共用）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n再见！")


if __name__ == "__main__":
    main()
