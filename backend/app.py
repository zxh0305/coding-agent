"""
Web 服务（装配层）
=================

把 Agent 包装成本地 HTTP API，并托管 frontend/ 目录下的静态页面。
纯标准库实现（http.server + sqlite3），无任何第三方依赖。

本文件是**装配层**：只负责把各层拼装起来（路径常量、共享状态、Handler 多继承
组装、启动流程），业务逻辑已按域拆分到：
  * services/  业务服务（模型解析 / 标题 / 轨迹 / 回合执行 / 会话装配 / …）
  * routes/    HTTP 路由（鉴权 / 会话 / 对话 / 模型 / Git / 工作区）
  * config.py  路径常量    * state.py  进程内共享状态

对外契约不变：启动方式仍是 `python3 backend/app.py [端口]`（默认 8000），
URL、事件协议、鉴权规则与拆分前逐字节一致。为兼容既有测试，本模块把被测试
引用的内部符号（_agents / _running_agents / _resolve_active / Handler / …）
重新导出到自身命名空间。

接口一览：
  GET  /                          前端聊天页面（静态文件托管）
  POST /api/auth/register         注册 {"username", "password"} → {token, username}
  POST /api/auth/login            登录 {"username", "password"} → {token, username}
  POST /api/auth/logout           退出登录（作废当前 token）
  GET  /api/auth/me               当前登录用户（校验 token 是否有效）
  GET  /api/config?session_id=    激活模型信息（供应商/模型/Key 打码/上下文窗口）。
                                  带 id = 该任务用的模型；不带 = 用户默认（新任务将用的）
  GET  /api/models?session_id=    可用模型列表（各供应商已启用的模型，供工具栏选择）
  POST /api/active-model          切换激活模型 {"provider_id", "model", "session_id"?}
                                  （带 session_id 只改该任务；不带改默认 = 新任务初始模型）
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
  POST /api/sessions/<id>/permission/<pid>  回答权限确认卡片
                                    {"decision": "allow"|"allow_session"|"deny"}
                                    （ask 暂停的回合在此恢复；超时按拒绝处理）
  GET  /api/sessions/<id>/artifact?path=  读取外置归档消息的完整原文（路径白名单校验）
  DELETE /api/sessions?session_id=  删除任务（须是自己的任务）
  GET  /api/context?session_id=   该任务当前上下文容量
  POST /api/chat/stop             停止指定任务的生成 {"session_id"}
  POST /api/sessions/<sid>/truncate  回退编辑：删除某条用户消息及其后的全部
                                  消息 {"mid"}（被压缩进摘要的旧消息拒绝）
  POST /api/sessions/<sid>/compact  /compact 斜杠命令：手动触发上下文压缩
                                  （无视阈值强制走分级压缩；运行中 409）

命令与事件解耦：POST 只入队（HTTP/1.0 时代的"每轮一个流"被替换掉），回合
由后台线程按会话锁串行执行，全部过程事件经统一发布口（events.SessionEvents
的 publish）进每会话一条的环形缓冲——常驻 SSE 连接、断线补发、多标签页
共用同一个真相。seq 是事件流水号（sessions.last_seq 持久化，重启不归零），
与消息的 mid 是两套身份。

登录与鉴权：除 /api/auth/* 外的所有接口要求 Authorization: Bearer <token>。
登录只做身份区分与会话隔离（各用户只看到自己的任务列表）；供应商/模型是
全局共享的——所有登录用户共用服务端配置的 LLM Key，也都能进"管理模型"面板。
工作区按任务隔离：每个任务可有自己的工作区（解析链：任务自选 → 用户默认
→ 项目 workspace/），切换互不影响，并发任务不互踩。

数据存储：任务、消息、用户、供应商配置全部落盘在 data/agent_data.db
（SQLite，见 db.py；日志与外置正文也在 data/ 下）；重启不丢。Agent 进程内只缓存
实例，历史按需从库里恢复。

运行：python3 backend/app.py [端口]     默认端口 8000
安全提示：服务监听 0.0.0.0（供局域网/外网访问），只有一层简单登录，
模型供应商/工作区等管理功能对所有登录用户开放，请只分享给信任的人；
若要暴露公网，建议再加反向代理与 HTTPS。
"""

import logging
import os
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import db
from agent import Agent
from code_tools import prepare_workspace
from config import BACKEND_DIR, ENV_FILE, FRONTEND_DIR, PROJECT_DIR
from events import SessionEvents
from llm_client import (create_client, load_env_file, save_env_values,
                        ApiHTTPError, ApiConnectionError, _RETRYABLE_STATUS)  # noqa: F401
from logger import setup_logging
from memory import memory_dir, recent_user_texts, run_extraction_async
from permissions import PermissionGate
from state import (_agents, _buses, _ctx, _lock, _running_agents, _session_locks,
                   _sigs, _turn_status)

# 业务服务层
from services import browser as _svc_browser
from services import session_boot as _svc_session_boot
from services import title as _svc_title
from services import turn as _svc_turn
from services.browser import _push_browser_screenshot
from services.messages import MAX_BODY_BYTES, MAX_IMAGE_B64  # noqa: F401  （re-export：被 routes 间接使用）
from services.model_resolve import (_active_window, _context_window_fallback, _mask,
                                    _match_active, _model_vision, _resolve_active,
                                    _resolve_client, _resolve_vision_model,
                                    _vision_backend, _vision_backend_for)
from services.session_boot import get_session
from services.title import _generate_session_title, _spawn_title_generation
from services.trace import (_persist_trace, _snapshot_trace, SNAPSHOT_INTERVAL,
                            SNAPSHOT_MAX_ENTRIES, SNAPSHOT_MAX_TEXT)
from services.turn import _run_round
from services.workspace import _resolve_workspace

# 路由层
from routes import busref
from routes.auth import AuthRoutes
from routes.base import HandlerBase
from routes.chat import ChatRoutes
from routes.dispatch import DispatchMixin
from routes.git import GitRoutes
from routes.models import ModelRoutes
from routes.sessions import SessionRoutes
from routes.workspace import WorkspaceRoutes

log = logging.getLogger("app")

# 供测试与装配层使用的常量再导出（原先都定义在本文件）
BACKEND_DIR = BACKEND_DIR
PROJECT_DIR = PROJECT_DIR
FRONTEND_DIR = FRONTEND_DIR
ENV_FILE = ENV_FILE


# ---------------------------------------------------------------------------
# 装配层能力：会话锁、事件总线、会话状态、权限闸门、记忆提取启动点
# 这些依赖共享状态（state）与日志，定义在本层，再注入给 services / routes，
# 从而让下层无需（也不能）反向 import app。
# ---------------------------------------------------------------------------

def _session_lock(sid: str) -> threading.Lock:
    """按任务粒度加锁：同一任务同时只允许一个生成过程（防止并发写乱消息历史），
    不同任务互不阻塞。锁对象随首个并发请求创建并常驻（数量级 = 任务数，可接受）。
    刻意不在停止接口用这把锁——生成卡住时，停止请求必须还能进来。"""
    with _lock:
        return _session_locks.setdefault(sid, threading.Lock())


def _event_bus(sid: str) -> SessionEvents:
    with _lock:
        bus = _buses.get(sid)
        if bus is None:
            bus = SessionEvents(last_seq=db.get_last_seq(sid),
                                persist=lambda seq, _sid=sid: db.set_last_seq(_sid, seq))
            _buses[sid] = bus
        return bus


def _session_state(sid: str) -> str:
    """任务的运行状态（供列表展示），动态算、不落库——它是内存态事实，
    进程重启后所有回合都已不存在，本就该归零。

      running  回合进行中（turn_start 已发、turn_end 未到）
      waiting  回合进行中且卡在权限闸门等用户确认（比 running 更该提醒）
      done     跑过且最近一轮正常结束、"你还没看过这次结果"（列表显示绿点）
      error    最近一轮出错、"你还没看过这次结果"（列表显示红点）
      none     从没跑过，或最近一轮的结果已被查看过（不显示状态）

    没有 bus 的会话必然没在跑：bus 惰性创建，此处只读不建——为列表展示
    凭空造 bus 会白占内存、还会把 last_seq 从库里读出来。
    """
    with _lock:
        bus = _buses.get(sid)
        agent = _agents.get(sid)
        last = _turn_status.get(sid)
    if bus is not None and bus.running:
        if agent is not None and agent.permissions.pending_count > 0:
            return "waiting"
        return "running"
    # 不在跑：绿/红点只在"结果未被查看过"时亮（未读标记语义）；看过或从没
    # 跑过都退化成无状态，不画点。
    if last and not last.get("seen"):
        return last.get("outcome") or "none"
    return "none"


# ask 等待用户决定的上限（秒）。超时不是安全边界——超时按拒绝处理，本来就
# 站在安全侧；这里只是防挂死：别让 worker 线程为一张再没人看的卡片等一辈子。
PERMISSION_ASK_TIMEOUT = 300


def _build_permission_gate(workspace: Path) -> PermissionGate:
    """构造一个会话的权限闸门（permissions.py）。用户规则按工作区隔离：存
    settings 表，key = "permissions:<workspace_path>"，值是 JSON 数组、可直接
    手编（后续可挂管理面板），例如：
      [{"tool": "run_bash", "pattern": "git push*", "decision": "allow"}]
    权限模式（readonly/confirm/yolo，前端输入框下拉）同样按工作区记忆，
    key = "perm_mode:<workspace_path>"，加载器每次判定现读——前端切换模式
    下一轮工具调用立即生效，无需重建会话。
    加载器以闭包注入而非让闸门直接 import db：闸门保持存储无关（CLI/单测
    不引库也能跑），每次判定现读——手编规则下一轮工具调用立即生效。"""
    return PermissionGate(
        workspace=workspace,
        user_rules_loader=lambda w=str(workspace): db.get_setting(f"permissions:{w}", []),
        mode_loader=lambda: db.get_setting(f"perm_mode:{workspace}", "confirm"),
        ask_timeout=PERMISSION_ASK_TIMEOUT,
    )


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


def _stop_target(sid: str) -> Agent | None:
    """停止请求的真正目标：正在跑回合的实例，其次才是当前缓存的实例。

    切模型/切工作区会重建 _agents[sid]，而旧回合仍持【旧】实例在跑——只查
    _agents 会把停止开关置到没人听的新实例上（实测：00:23:53 发起请求，
    00:24:15 切模型，00:24:16 点停止，旧请求直到 00:24:53 超时才收场，
    用户眼里的"停止"慢了近 40 秒）。"""
    return _running_agents.get(sid) or _agents.get(sid)


def install_service_hooks() -> None:
    """把装配层能力注入 services / routes（模块属性注入，import 后调用）。

    这样下层模块通过模块属性访问 log / 会话锁 / 事件总线 等，既拿到的是同一
    对象，又不会形成 services → app / routes → app 的反向 import（那会循环）。
    """
    # services/*
    for mod in (_svc_title, _svc_browser, _svc_turn, _svc_session_boot):
        mod.log = log
    # create_client 的调用点已下沉到 services.model_resolve；历史上测试用
    # `app.create_client = fake` 打桩，这里让 model_resolve 经代理始终取 app 的
    # 同名属性，保住"打 app.create_client 即生效"的既有测试契约。
    import services.model_resolve as _svc_model_resolve
    _svc_model_resolve.create_client = lambda *a, **k: create_client(*a, **k)
    _svc_turn._session_lock = _session_lock
    _svc_turn._event_bus = _event_bus
    _svc_turn._lock = _lock
    _svc_turn._running_agents = _running_agents
    _svc_turn._turn_status = _turn_status
    _svc_turn._ctx = _ctx
    _svc_turn._spawn_memory_extraction = _spawn_memory_extraction
    _svc_turn._spawn_title_generation = _spawn_title_generation
    _svc_browser._event_bus = _event_bus
    _svc_title._event_bus = _event_bus
    _svc_session_boot._lock = _lock
    _svc_session_boot._agents = _agents
    _svc_session_boot._sigs = _sigs
    _svc_session_boot._event_bus = _event_bus
    _svc_session_boot._build_permission_gate = _build_permission_gate
    _svc_session_boot._vision_backend_for = _vision_backend_for
    # routes/*
    busref.install(
        log=log,
        _session_lock=_session_lock,
        _event_bus=_event_bus,
        _session_state=_session_state,
        _build_permission_gate=_build_permission_gate,
        _stop_target=_stop_target,
        _spawn_memory_extraction=_spawn_memory_extraction,
        _run_round=_run_round,
    )


install_service_hooks()  # 模块导入即装配（只需一次，重复调用幂等）


class Handler(AuthRoutes, SessionRoutes, ChatRoutes, ModelRoutes,
              GitRoutes, WorkspaceRoutes, DispatchMixin, HandlerBase):
    """API 路由 + 静态文件托管（frontend/ 目录）。

    HandlerBase 必须排在最后（提供 SimpleHTTPRequestHandler 基类与 __init__），
    各路由 mixin 在前，方法解析顺序与拆分前一致。
    """


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
    db.cleanup_orphan_attachments()  # 兜底：清理无主会话的附件目录（防 kill -9 残留）
    db.cleanup_staging()             # 兜底：清理超时未提交的分块上传暂存区
    orphan = db.cleanup_orphans()    # 兜底：清理无主行（历史遗留 / 旧版漏删）
    if any(orphan.values()):
        print(f"🧹 已清理孤儿行: {orphan}", flush=True)
    db.optimize_db()                 # 回收删会话留下的空洞，缩小库文件
    from browser_tools import cleanup_stale_profiles  # noqa: E402
    n = cleanup_stale_profiles(int(os.environ.get("BROWSER_PROFILE_KEEP_DAYS", "7")))
    if n:
        print(f"🧹 已清理 {n} 个陈旧浏览器 profile 目录", flush=True)
    # 日志兜底回收：TimedRotatingFileHandler 只在跨零点删「条数超限」的备份，
    # 单日暴涨的大日志（曾达 367MB）与长期不重启的目录都不会被清，这里补一刀。
    from logger import prune_old_logs  # noqa: E402
    log_dir = os.environ.get("LOG_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
    m = prune_old_logs(log_dir, int(os.environ.get("LOG_KEEP_DAYS", "14")))
    if m:
        print(f"🧹 已回收 {m} 个过期日志文件", flush=True)
    log_file = setup_logging(console=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    # 长连接并发调优（SSE 每连接一个线程、常驻不释放）：
    # 1) daemon_threads：主线程退出时不被常驻 SSE 线程卡住；同时让线程池
    #    在客户端断开后立即回收，不 join 等待；
    # 2) request_queue_size：listen 的 accept  backlog。默认 5 太小——多个
    #    标签页/手机端同时连接时，超出 backlog 的握手会排在内核队列里迟迟
    #    不被 accept，表现为"页面转圈几秒才连上"（压测 60 条 SSE 建连
    #    P50 曾达 3.1s，正是 backlog 打满 + 逐条 accept 的排队现象）；
    # 3) block_on_close=False：连接关闭不在 shutdown 时阻塞 join。
    server.daemon_threads = True
    server.request_queue_size = 256
    server.block_on_close = False
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
