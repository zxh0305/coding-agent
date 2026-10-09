"""取回（或创建）任务会话，装配好 Agent
====================================

原 app.py 顶层的 get_session，逐字搬移。它把"解析模型客户端 → 建会话行 →
建 Agent → 从库恢复历史窗口"串成一条装配链。

依赖装配层能力（统一日志 log、全局锁 _lock、Agent 实例缓存与配置指纹 _agents/
_sigs、会话总线 _event_bus、权限闸门构造、视觉后端绑定），由
app.install_service_hooks() 注入——这样本模块无需（也不能）import app，避免循环。
"""

import uuid

from agent import Agent

import db
from code_tools import prepare_workspace
from services.model_resolve import _active_window, _resolve_client
from services.workspace import _resolve_workspace

# 由 app.install_service_hooks() 注入的装配层能力。
log = None
_lock = None
_agents = None
_sigs = None
_event_bus = None
_build_permission_gate = None
_vision_backend_for = None


def get_session(session_id, user_id: int) -> tuple[str, Agent]:
    """取回（或创建）一个任务会话，返回 (id, Agent)。历史缺失时从数据库恢复。

    任务归属校验：传入的 session_id 必须存在且属于该用户，否则一律开新任务
    （防止拿着别人的任务 id 读/写别人的对话）。

    注意顺序：先确认会话归属与模型可用，再创建会话行——否则模型配置有问题时
    会在数据库里留下"零消息、空标题"的孤儿任务。模型按会话解析（老会话用它
    自己选的、新会话用全局默认），所以必须先把 sid 定下来再解析客户端。
    """
    sid = None
    if isinstance(session_id, str) and session_id:
        # 任务是否存在、是否归当前用户，都以数据库为准
        if db.session_owner(session_id) == user_id:
            sid = session_id
    if sid is None:
        # 新任务的 id 提前定下来（会话行仍在解析客户端成功后才建，保持
        # "模型配置有问题时不留孤儿会话行"的顺序）：api_retry 事件回闭包
        # 需要 sid 才能把重试进度推给这个会话的事件流。
        sid = uuid.uuid4().hex[:8]

    prov, client, model, sig, vision = _resolve_client(sid)
    # 重试观测接线：LLM 请求瞬态失败退避重试时，把进度推给会话事件流
    # （前端显示"正在重试(2/3)"，等待不再像卡死）。闭包捕获 sid——客户端
    # 实例随 Agent 缓存复用，但同一会话的 sid 恒定，无需重建闭包。
    client.on_retry = (lambda info, _sid=sid:
                       _event_bus(_sid).publish({"type": "api_retry", **info}))
    if not db.session_owner(sid):
        db.create_session(sid, user_id)
    workspace = _resolve_workspace(user_id, sid)
    if workspace is None:
        # 未绑定项目的会话（用户选了"不绑定项目"）：工具沙箱用系统默认目录，
        # 但【不落库为项目归属】——列表里进"其他"组。落库的话它就成了项目。
        workspace = prepare_workspace(None)
    # 工作区纳入配置指纹：任务的工作区被切换后，下次构建会重建 Agent（历史照旧从库里恢复）
    sig += "|" + str(workspace)

    # 锁只保护实例缓存的读写这一瞬间。生成过程可能持续几分钟，绝不能全程
    # 持锁——否则一条慢请求会把所有提问/删除/停止请求全部卡死（真实踩过的坑）。
    with _lock:
        if sid not in _agents or _sigs.get(sid) != sig:
            agent = Agent(llm=client, verbose=False, vision_supported=vision,
                          workspace=workspace, vision_backend=_vision_backend_for(sid),
                          context_window=_active_window(sid),  # 压缩触发线的基准（切换模型后重建实例即更新）
                          artifact_reader=db.read_artifact,  # 外置大消息的还原器（模型视图用）
                          # 工具结果落盘 sink（结果落盘轻引用）：全文进 data/tool_results/<sid>/
                          result_sink=lambda content, _sid=sid: db.write_tool_result(_sid, content),
                          permission_gate=_build_permission_gate(workspace),
                          session_id=sid,  # 文档工具据此确定文档归属
                          model_tag=(prov["id"], model))  # 随 _stats 落库，用量页按它聚合
            # 落盘工具结果的读侧（read_tool_result 工具用，与 result_sink 同一套目录）
            agent.ctx.tool_result_reader = db.read_tool_result
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
