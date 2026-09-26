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

数据存储：任务、消息、用户、供应商配置全部落盘在 data/agent_data.db
（SQLite，见 db.py；日志与外置正文也在 data/ 下）；重启不丢。Agent 进程内只缓存
实例，历史按需从库里恢复。

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
from git_tools import GitError, repo_summary
from git_tools import branches as git_branches
from git_tools import checkout as git_checkout
from git_tools import identity as git_identity
from git_tools import log as git_log
from git_tools import show as git_show
from llm_client import (create_client, load_env_file, save_env_values,
                        ApiHTTPError, ApiConnectionError, _RETRYABLE_STATUS)
from logger import setup_logging
from memory import memory_dir, recent_user_texts, run_extraction_async
import permissions
from permissions import PermissionGate
from tools import TOOL_SCHEMAS

log = logging.getLogger("app")

# 提交 hash 白名单：只允许 7-40 位十六进制（短 hash / 完整 SHA-1）。
# 这是 /api/git/show 的第一道防线——hash 会作为 argv 传给 git，虽然
# shell=False 已经免疫命令注入，但限制字符集能挡掉"传个分支名/选项
# 进来"（如 --output=… 这类被误当参数的形态）。
_GIT_HASH_RE = re.compile(r"[0-9a-fA-F]{7,40}")

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
ENV_FILE = PROJECT_DIR / ".env"

# Agent 实例缓存：sid -> (Agent, sig)。sig 是"激活供应商+模型+密钥"的指纹，
# 指纹变了（用户切换模型/改配置）就重建实例，但历史从数据库恢复，不丢对话。
_agents: dict[str, Agent] = {}
_sigs: dict[str, str] = {}
# 正在跑回合的 Agent 实例（sid → agent）。与 _agents 分开记：切模型/切工作区
# 会重建 _agents[sid]，但旧回合仍持【旧】实例在跑——停止请求若只查 _agents，
# 会把开关置到没人听的新实例上（实测：停止后干等 60s 直到旧请求超时才收场）。
_running_agents: dict[str, Agent] = {}
_ctx: dict[str, dict] = {}  # sid -> 最近一次上下文统计（非关键数据，只存内存）
# 最近一轮的结局：sid -> {"outcome": "done"|"error", "seen": bool}（只存内存）。
# 语义是"未读标记"：回合跑完时若用户不在这个会话里，就置 seen=False，列表亮
# 绿点（done）/红点（error）提示"有新结果"；用户切进去看过即置 seen=True，
# 徽标消失。用户正看着的会话前台跑完，会被前端立即标记已读，因此不亮。
# 不落库：进程重启后没有"上一轮"可言，退回无状态是正确的。
_turn_status: dict[str, dict] = {}
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


def _push_browser_screenshot(sid: str, png: bytes, url: str, note: str) -> None:
    """浏览器工具的截图推送（browser_tools.screenshot_pusher 注入点）：
    PNG 落盘到会话浏览器目录 + 发 SSE 事件（前端右侧「浏览器」弹窗实时显示）。
    调用发生在工具线程里，publish 有锁，安全；落盘失败不影响工具调用。"""
    try:
        out = Path("data/browser-shots") / sid
        out.mkdir(parents=True, exist_ok=True)
        n = len(list(out.glob("shot-*.png"))) + 1
        rel = out / f"shot-{n:04d}.png"
        rel.write_bytes(png)
        _event_bus(sid).publish({"type": "browser_shot",
                                 "url": url, "note": note,
                                 "shot": f"/api/sessions/{sid}/browser/shot?n={n}"})
    except Exception:
        log.exception("[会话 %s] 浏览器截图推送失败（忽略）", sid)


def _persist_trace(sid: str, mid: str, trace: list) -> None:
    """把一轮提问的轨迹裁剪后落库。轨迹里的工具结果原样来自执行器（可能几 MB），
    回放场景用不到全文（那在归档/历史消息里），逐条截断控制体积；
    总条数也设上限——失控回合的轨迹不该撑爆库。"""
    entries = []
    for e in trace[:150]:
        if not isinstance(e, dict):
            continue
        e = dict(e)
        r = e.get("result")
        if isinstance(r, str) and len(r) > 1200:
            e["result"] = r[:1200] + f"…[截断，完整输出见执行记录，共 {len(r)} 字符]"
        a = e.get("arguments")
        if isinstance(a, str) and len(a) > 2000:
            e["arguments"] = a[:2000] + "…[截断]"
        t = e.get("text")  # reasoning 条目的思考文本：可很长，回放不必全文
        if isinstance(t, str) and len(t) > 4000:
            e["text"] = t[:4000] + f"…[思考截断，共 {len(t)} 字符]"
        entries.append(e)
    db.set_trace(sid, mid, json.dumps(entries, ensure_ascii=False))


def _run_round(sid: str, agent: Agent, plain: str, user_message: dict,
               nonce: str, atts: list) -> None:
    """回合执行体（POST 只入队，真正的生成在这里跑）。

    会话锁串行同一任务的回合（多个 POST 排队时各自线程等锁，等价于旧
    "请求内执行"的排队语义）；所有过程事件走统一发布口 bus.publish——
    禁止第二条写入路径，旁路写入不会进缓冲，重连客户端永远看不到。

    事件协议：在原有回合事件（round/reasoning_delta/answer_delta/tool_call/
    tool_result/permission_request/usage/done/compacted）外增加两个回合边界事件：
      turn_start {nonce, input, atts}  回合开始：多标签页/刷新后的页面靠它
                                       补画用户气泡并进入"生成中"状态；nonce
                                       让发起方识别自己（不重复画）
      turn_end   {user_mid}            回合结束（落盘已完成）：前端驱动排队队
                                       列推进的唯一信号；user_mid = 本回合输入
                                       消息的 mid，前端补发去重靠它识别
                                       "这回合已在时间线里"
    回答类事件额外带 mid（本轮回答段落的身份）：前端把同一 mid 的 delta
    归并进同一个气泡——与历史消息的 mid 同一体系，补发与时间线才能对上。
    reasoning_delta 是例外：它【不带 mid】。推理不是回答，带 mid 会让前端把
    思考过程归并进回答气泡（同一 mid = 同一气泡），正文区就出现了思考过程；
    它在「执行过程」面板里有自己的块，靠 round 事件切换归属。
    """
    bus = _event_bus(sid)
    with _session_lock(sid):
        # 排队期间任务可能已被删除：直接放弃（会话行没了，落盘也会跳过）
        if db.session_owner(sid) is None:
            return
        # 登记"正在跑回合的实例"：停止请求的真正目标（见 _running_agents 注释）。
        # finally 里只有仍是本实例时才摘除——若回合中途实例被重建，新实例的
        # 登记不能被旧回合的收尾误删。
        _running_agents[sid] = agent
        try:
            # 新回合开跑：清掉上一轮的绿/红点（此刻列表应显示"运行中"，不是
            # 上次的结局）。回合真结局在下面收尾处按 error 重新置位。
            with _lock:
                _turn_status.pop(sid, None)
            # 用户消息【回合开始即落库】，不等回合收尾。否则回合进行中切走再
            # 切回来时，时间线分页接口查不到它；若 turn_start 恰好被环形缓冲
            # 挤掉（超长回合），补发也画不回来——用户输入就"消失"了。提前落库
            # 后，切回的页面靠分页接口必然能看到它。save_messages 增量幂等，
            # 收尾处的整段落盘按内容指纹跳过它，不产生重复行。
            # mid 必须这里预分配并写进 user_message：agent.run 的 _run 把同一个
            # 对象追加进 history，收尾整段落盘、turn_end 提取的 user_mid 都与
            # 此处一致（原逻辑等收尾时才由 save_messages 分配，那时 turn_start
            # 早已发完，带不了 mid）。
            user_mid = uuid.uuid4().hex
            user_message["_mid"] = user_mid
            db.save_messages(sid, [user_message], {})
            # 截图推送注入（browser_tools → SSE）：回合线程里安全，publish 自带锁
            import browser_tools
            browser_tools.screenshot_pusher = _push_browser_screenshot
            bus.publish({"type": "turn_start", "nonce": nonce, "input": plain, "atts": atts,
                         # 回合真起点（秒）。补发/多标签页/刷新后的页面没有本地
                         # 计时起点，靠它把「已工作 N 秒」接上，而不是从 0 重数。
                         "started_at": time.time(),
                         # 本回合输入消息的 mid：前端据此对"已在时间线"的输入
                         # 去重补画（落库提前后，历史接口与补发都会带它）。
                         "user_mid": user_mid})
            log.info("[会话 %s] 用户提问: %s", sid, plain)
            seg_mid = None  # 当前回答段落的 SSE 气泡 mid（每个 round 事件换一段，仅事件流用）
            error = None
            retryable = False  # 出错时是否值得引导用户重试（见下面的归因逻辑）
            try:
                for kind, payload in agent.run(plain, user_message):
                    if kind == "round":
                        seg_mid = uuid.uuid4().hex[:12]
                        bus.publish({"type": "round", "mid": seg_mid, **payload})
                    elif kind in ("answer_delta", "done"):
                        bus.publish({"type": kind, "mid": seg_mid, **payload})
                    elif kind == "reasoning_delta":
                        # 思考过程不是回答：绝不带 seg_mid。带上的话前端会把推理
                        # 归并进回答气泡（同一 mid = 同一气泡），推理文字就冒充了
                        # 正文——思考流在「执行过程」面板里有自己的块，不需要 mid。
                        bus.publish({"type": kind, **payload})
                    else:
                        bus.publish({"type": kind, **payload})
                    if kind in ("usage", "compacted"):  # 最新上下文容量，供 /api/context
                        _ctx[sid] = payload
                    if kind == "done":
                        log.info("[会话 %s] 最终回答: %s", sid, str(payload.get("answer"))[:200])
            except RuntimeError as e:
                log.exception("LLM 请求失败")
                error = str(e)
                # 结构化错误归因（参照 ZCode errorAttribution.retryable）：
                # 前端据此渲染"重试"按钮而不是一坨报错。值得点重试的 = 连接层
                # 失败（重试窗口耗尽后仍失败）与 402 余额不足 / 429 限流 / 5xx
                # ——充了值、过了限流窗口就有机会成功；400/401/证书错误等
                # 注定失败的请求不引导用户重试。
                retryable = (isinstance(e, ApiConnectionError)
                             or (isinstance(e, ApiHTTPError)
                                 and (e.status in _RETRYABLE_STATUS or e.status == 402)))
            except Exception:
                # 未预期异常也必须转成 error 事件：前端把 turn_end 当回合结束的
                # 唯一信号，线程无声死掉会让所有订阅页永远挂在"生成中"
                log.exception("回合执行出现未预期异常")
                error = "服务器内部错误，详情见 backend 日志"

            # 出错时把错误本身作为一条 assistant 消息追加进历史（方案 A）：
            # 不落库的话，错误卡只活在当前页面的 DOM 里，切会话/刷新后即消失，
            # 时间线上只剩用户气泡、助手那边"无声无息"。带 error 标记落库后：
            # 1) 历史回放能渲染出同样的错误卡（前端 blocks.js 识别 m.error）；
            # 2) 下一轮发给模型的上下文里能看到"上轮失败了、败在哪"——这本身
            #    就是有价值的对话历史（模型可据此道歉/换思路），无需特意剔除。
            if error is not None:
                # 前缀只拼一次：实时路径（前端 error 事件）与回放路径（这条落库
                # 消息）都由各自渲染层加 "❌ "，此处 content 存纯文本。
                agent.history.append({"role": "assistant", "content": str(error),
                                      "error": True, "retryable": bool(retryable)})
            # 收尾（正常/停止/出错共用）：增量落盘 → 重编号信号 → turn_end。
            # 落盘在前、turn_end 在后——turn_end 里的 user_mid 是"这回合已可
            # 从时间线读到"的承诺，顺序反了前端去重会误判。
            written = db.save_messages(sid, agent.history, agent.saved)
            db.touch_session(sid)
            log.info("[会话 %s] 本轮落盘 %d 行", sid, written)
            # 轨迹的落库键必须是【最终回答消息落库后的真实 mid】——不能用上面
            # 事件流里的 seg_mid。seg_mid 是 _run_round 现生成的 12 位短 id，
            # 只服务于前端把同一段回答的 delta 归并进一个气泡，它从不写进
            # agent.history；而消息的真实 mid 是 save_messages 分配的 32 位
            # uuid（见 db.save_messages）。用 seg_mid 落轨迹，get_traces 按
            # 消息 mid 永远查不到——回放时「执行过程」折叠条永不出现。
            # save_messages 就地给每条消息写回了 _mid，所以这里能从历史里取到：
            # 取最后一条落库的 assistant 消息（收尾答案），跳过 _synthetic。
            if error is None:
                answer_mid = next((m.get("_mid") for m in reversed(agent.history)
                                   if m.get("role") == "assistant" and not m.get("_synthetic")), None)
                if answer_mid:
                    # 执行过程轨迹随最终回答落库：切换会话/刷新后历史回放仍能
                    # 展开看"当时每一步做了什么、改了哪些文件"。失败不阻塞收尾。
                    try:
                        _persist_trace(sid, answer_mid, agent.trace)
                    except Exception:
                        log.exception("[会话 %s] 轨迹落库失败（忽略）", sid)
            # 记下本轮结局，供任务列表徽标用（"跑过且正常结束"= done 绿点、
            # 出错 = error 红点）。seen=False = 未读：徽标只在"用户不在这个会话
            # 里跑完"时亮，用户切进去看过即置 seen=True 清掉（前端在切会话 /
            # 前台跑完的 turn_end 处调用 POST /api/sessions/<id>/seen）。
            # 用户主动停止不算错——stopped 走的是正常收尾路径（error 为 None），
            # 与旧行为一致地显示为 done。
            with _lock:
                _turn_status[sid] = {"outcome": "error" if error is not None else "done",
                                     "seen": False}
            if error is not None:
                db.renumbered_sessions.discard(sid)  # 错误路径不带重编号信号（与旧行为一致）
                bus.publish({"type": "error", "message": error,
                             "retryable": bool(retryable)})
            elif sid in db.renumbered_sessions:
                # 间隔耗尽兜底触发过整会话重编号：分页游标（before_ord 指向旧
                # 序号空间）全部失效，推事件让前端重拉时间线
                db.renumbered_sessions.discard(sid)
                bus.publish({"type": "history_renumbered"})
            # user_mid = 本回合输入消息的 mid。必须跳过 _synthetic 合成消息
            # （收尾指令/循环提醒也是 role=user 且排在最后，但永不落库、没有
            # _mid）——否则收尾轮结束的回合会把去重键带成 None，前端的补发
            # 去重会把这回合误判成"不在时间线里"而重复渲染。
            user_mid = next((m.get("_mid") for m in reversed(agent.history)
                             if m.get("role") == "user" and not m.get("_synthetic")), None)
            bus.publish({"type": "turn_end", "user_mid": user_mid})
            # 回合结束关掉本会话的浏览器：Chromium 进程不常驻；profile 目录
            # 保留在磁盘，下次 browser_* 工具再用时登录态还在。
            try:
                import browser_tools
                browser_tools.close_session(sid)
            except Exception:
                pass
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
        finally:
            if _running_agents.get(sid) is agent:
                _running_agents.pop(sid, None)


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


def _resolve_client(sid: str | None = None) -> tuple[object, str, str, bool]:
    """按当前激活模型（含其供应商的 API 格式与视觉标记）构建客户端，
    返回 (client, model, 指纹, 是否支持视觉)。sid = 按该会话的模型解析。"""
    prov, model = _resolve_active(sid)
    if not model:
        raise SystemExit("没有已启用的模型，请在网页「管理模型」里添加并启用")
    client = create_client(prov.get("api_format", "openai"),
                           api_key=prov["api_key"], base_url=prov["base_url"], model=model)
    vision = _model_vision(prov, model)
    sig = json.dumps([prov["id"], model, prov["base_url"], prov["api_key"], prov.get("api_format"), vision],
                     ensure_ascii=False)
    return client, model, sig, vision


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


MAX_IMAGE_B64 = 6_000_000   # 单张图片 base64 长度上限（约 4.5MB 原图）


def _build_user_message(body: dict, sid: str) -> tuple[str, dict | None]:
    """把 {message, attachments} 组装成 OpenAI 格式的用户消息。

    图片 → image_url 视觉输入（需要所用模型支持视觉）；
    文本文件 → 落盘到会话附件区（data/attachments/<sid>/），消息里只注入
    文件名与大小，模型用 read_attachment 工具按需分页读取。
    返回 (纯文本预览, 完整消息)；预览用于任务标题。

    为什么文本附件不再注入正文：早期做法把全文解码后塞进消息，300KB 中文
    就约 10 万 token，且随会话历史每轮重复携带，上限只能卡死在 300KB。改为
    引用式后，上下文成本从"全文"降到"几十 token"，单文件上限放宽到 5MB，
    模型还能通过分页覆盖全文。sid 用于确定附件归属，不可为空。
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
                blob = base64.b64decode(data)
            except Exception:
                continue
            if not blob:
                continue
            info = db.save_attachment(sid, name, blob)  # 落盘；超限抛 ValueError
            file_notes.append(
                f"### 附件文件：{info['name']}（已存入附件区，共 {info['bytes']} 字节）\n"
                f"请用 read_attachment 工具读取内容（支持 offset/limit 分页），"
                f"不要假设已经看到全文。")
            names.append(info["name"])
    if file_notes:
        parts.append({"type": "text", "text": "用户附带了以下文件：\n\n" + "\n\n".join(file_notes)})
    if not parts:
        return "", None
    plain = text or ("[附件] " + "、".join(names))
    if len(parts) == 1 and parts[0]["type"] == "text":
        return plain, {"role": "user", "content": parts[0]["text"]}
    return plain, {"role": "user", "content": parts}


def _resolve_workspace(user_id: int, sid: str) -> Path | None:
    """解析一个任务的工作区，优先级：任务自选 → 用户默认（仅存量兼容）。

    返回 None = 任务未绑定项目（新任务未选目录时）：工具调用会被闸门拦下，
    前端引导先选目录。sid 为空时返回 None（新任务的绑定发生在选目录那一刻，
    不再预绑用户默认——此前默认目录会让"新任务"悄悄带上项目归属）。
    """
    ws = db.get_session_workspace(sid) if sid else None
    if not ws:
        # 存量兼容：老会话（升级前就有 workspace 行为空）落回用户默认，
        # 不至于让历史任务突然失去项目归属；新任务不走这条路。
        legacy = db.get_setting(f"default_workspace:{user_id}") if sid else None
        return prepare_workspace(legacy) if legacy else None
    return prepare_workspace(ws)


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

    client, model, sig, vision = _resolve_client(sid)
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
                          permission_gate=_build_permission_gate(workspace),
                          session_id=sid)  # 文档工具据此确定文档归属
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


def _stop_target(sid: str) -> Agent | None:
    """停止请求的真正目标：正在跑回合的实例，其次才是当前缓存的实例。

    切模型/切工作区会重建 _agents[sid]，而旧回合仍持【旧】实例在跑——只查
    _agents 会把停止开关置到没人听的新实例上（实测：00:23:53 发起请求，
    00:24:15 切模型，00:24:16 点停止，旧请求直到 00:24:53 超时才收场，
    用户眼里的"停止"慢了近 40 秒）。"""
    return _running_agents.get(sid) or _agents.get(sid)


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
        例外：events 与 browser/shot 端点额外接受 ?token= 查询参数鉴权——
        浏览器原生的 EventSource / <img> 不支持自定义请求头，
        Authorization 带不进去。只对这两个端点开这个口子：token 出现在
        URL 里存在被代理日志记录的暴露面，能窄则窄。
        通过后把用户挂在 self.user 上，后续接口直接用。
        """
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            self.user = None
            return True
        user = self._auth_user()
        token_in_url = path.endswith("/events") or path.endswith("/browser/shot")
        if user is None and token_in_url:
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

    def _query_sid(self) -> str | None:
        """查询串里的 session_id：缺失、或不属于当前用户 → None（按全局默认处理）。
        只用于"读"接口：拿别人的 id 最多看到默认模型，不会泄露或改动任何会话。"""
        sid = (self._query().get("session_id") or [""])[0]
        if sid and db.session_owner(sid) != self.user["id"]:
            return None
        return sid or None

    def _config_view(self, sid: str | None = None) -> dict:
        """当前激活模型视图。sid = 按该会话的模型返回（None = 该用户的全局默认）。"""
        prov, model = _resolve_active(sid)
        return {
            "provider_id": prov["id"],
            "provider_name": prov["name"],
            "model": model,
            "api_key_masked": _mask(prov["api_key"]),
            "context_window": _active_window(sid),
            "vision": _model_vision(prov, model),  # 激活模型是否支持看图（前端附件提示用）
            "session_id": sid or "",   # 回显：前端据此丢弃切会话途中的过期响应
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
        elif path == "/api/config":
            # 带 session_id = 该任务用的模型；不带 = 全局默认（新任务将用的）
            self._json(self._config_view(self._query_sid()))
        elif path == "/api/models":
            sid = self._query_sid()
            models = []
            for p in db.list_providers():
                if not p["enabled"]:
                    continue
                for m in p["models"]:
                    if m["enabled"]:
                        models.append({"provider_id": p["id"], "provider_name": p["name"],
                                       "model": m["name"], "context_window": m["context_window"]})
            prov, active_model = _resolve_active(sid)
            self._json({"models": models, "active": active_model,
                        "active_provider": prov["id"]})
        elif self.path == "/api/providers":
            provs = []
            for p in db.list_providers():
                provs.append({**p, "api_key": None, "api_key_masked": _mask(p["api_key"])})
            self._json(provs)
        elif self.path.startswith("/api/workspace"):
            # 带当前任务 id 时返回该任务的绑定目录；不带 = 用户默认（仅展示用）。
            # custom = 该会话是否已绑定项目（新任务未选目录时为 false：前端
            # 显示"选择项目"，发送前强制先选）。
            sid = (self._query().get("session_id") or [""])[0]
            if sid and db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            if sid:
                ws, custom = db.get_session_workspace(sid), bool(db.get_session_workspace(sid))
            else:
                ws = db.get_setting(f"default_workspace:{self.user['id']}")
                custom = bool(ws)
            self._json({"path": str(ws) if ws else "", "custom": custom})
        elif self.path.startswith("/api/fs/dirs"):
            self._handle_fs_dirs()
        elif self.path.startswith("/api/git/"):
            # Git 浮窗的三个只读接口：log（列表）/ show（单条 diff）/ summary
            self._handle_git(path)
        elif self.path == "/api/tools":
            self._json({"tools": [
                {"name": t["function"]["name"],
                 "description": t["function"]["description"],
                 "parameters": t["function"]["parameters"]}
                for t in TOOL_SCHEMAS
            ]})
        elif self.path == "/api/sessions":
            rows = db.list_sessions(self.user["id"])
            for r in rows:  # state 是内存态，db 层不掺和，在这里现算后随列表带回
                r["state"] = _session_state(r["id"])
            self._json(rows)
        elif re.fullmatch(r"/api/sessions/[^/]+/perm_mode", path):
            # 会话的权限模式（前端输入框下拉）：闸门 mode_loader 每次判定现读
            sid = path.split("/")[3]
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            self._handle_perm_mode(sid)
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
            if sub == "docs":
                return self._handle_session_docs(sid)
            if sub == "attachments":
                return self._handle_session_attachments(sid)
            if sub == "browser" and len(parts) > 4 and parts[4] == "shot":
                return self._handle_browser_shot(sid)
            return self._handle_session_messages(sid)
        elif self.path.startswith("/api/context"):
            sid = (self._query().get("session_id") or [""])[0]
            if sid and db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            stat = _ctx.get(sid, {})
            # tokens = 最近一次真实请求的 prompt_tokens（=模型当前上下文大小）。
            # 旧字段 prompt_tokens 是本回合多轮请求的累加值，会把重发的历史
            # 重复计数，只留给兼容；徽章口径用新的 context_tokens。
            # 内存无值（服务重启后）时回查 message_usage 最新记录兜底——
            # 落库的是每条 assistant 消息当时的 stats_json。
            tokens = stat.get("context_tokens")
            if tokens is None:
                tokens = db.latest_context_tokens(sid)
            self._json({
                "tokens": tokens or 0,
                "window": _active_window(sid),  # 分母按该任务的模型算（各会话窗口可以不同）
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
            elif re.fullmatch(r"/api/sessions/[^/]+/permission/[^/]+", path):
                # 权限确认的决定回令：/api/sessions/<sid>/permission/<pid>
                parts = path.split("/")
                sid, pid = parts[3], parts[5]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_permission(sid, pid)
            elif re.fullmatch(r"/api/sessions/[^/]+/truncate", path):
                # 回退编辑：删除某条用户消息及其后的全部消息
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_truncate(sid)
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
            elif re.fullmatch(r"/api/sessions/[^/]+/rename", path):
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_session_rename(sid)
            elif re.fullmatch(r"/api/sessions/[^/]+/perm_mode", path):
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_perm_mode(sid)
            elif re.fullmatch(r"/api/sessions/[^/]+/seen", path):
                # 标记该会话的绿/红点已读（清掉未读徽标）
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_session_seen(sid)
            elif self.path == "/api/workspace":
                self._handle_workspace_set()
            elif self.path == "/api/git/checkout":
                self._handle_git_checkout()
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
        # /api/sessions/<sid>/attachments?name=xxx / docs?name=xxx：单文件删除
        # （附件浮窗与文档面板的 🗑 按钮）。归属校验与 GET 同一套。
        m = re.fullmatch(r"/api/sessions/([^/]+)/(attachments|docs)",
                         urllib.parse.urlparse(self.path).path)
        if m:
            sid, kind = m.group(1), m.group(2)
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            name = (self._query().get("name") or [""])[0]
            if not name:
                return self._json({"error": "缺少 name 参数"}, 400)
            try:
                if kind == "attachments":
                    db.delete_attachment(sid, name)
                else:
                    db.delete_doc(sid, name)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except FileNotFoundError:
                pass  # 幂等：已不存在视为删除成功
            return self._json({"ok": True})
        if urllib.parse.urlparse(self.path).path == "/api/sessions":  # self.path 带 ?query，须剥掉再比较
            sid = (self._query().get("session_id") or [""])[0]
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            db.delete_session(sid)
            with _lock:
                _agents.pop(sid, None)
                _ctx.pop(sid, None)
                _turn_status.pop(sid, None)
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
        # 会话 id 要先拿到：文本附件需要按会话落盘（data/attachments/<sid>/）。
        # get_session 内部只短持锁（见其注释）；本接口本身毫秒级返回，回合不在
        # 本请求内执行，这里提前取不会拉长锁窗口。
        sid, agent = get_session(sid_or_none, self.user["id"])
        plain, user_message = _build_user_message(body, sid)
        if not plain:
            return self._json({"error": "输入不能为空"}, 400)
        # 新任务可随请求携带 workspace：创建即绑定项目。先校验目录再建会话，
        # 绑定（set_session_workspace）发生在 get_session 构建回合 Agent 之前
        # ——原子性由"绑定落库 → 构建 Agent → 启动回合线程"的顺序保证。
        ws_req = str(body.get("workspace") or "").strip()
        ws_target = None
        if ws_req:
            ws_target = Path(ws_req).expanduser().resolve()
            if not ws_target.is_dir() or ws_target == Path(ws_target.root):
                return self._json({"error": "工作目录不存在或不可用"}, 400)
            if sid_or_none:
                return self._json({"error": "已存在的任务不支持随消息改绑目录，请用 /api/workspace"}, 400)
        if ws_target is not None:
            db.set_session_workspace(sid, str(ws_target))
            # 附件此前只能落 data/attachments（上传发生在绑定工作区之前），
            # 现在有了工作区，把它们搬过去——之后 agent 的文件工具才够得到。
            db.migrate_attachments_to_workspace(sid)
            with _lock:
                _agents.pop(sid, None)  # 丢弃无目录时构建的占位实例，下一回合按绑定目录重建
            agent = get_session(sid, self.user["id"])[1]
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
            self.wfile.write(sse_frame(None, {"type": "caught_up", "running": bus.running,
                                              "started_at": bus.round_started_at}))
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
        agent = _stop_target(sid)
        if agent is None or agent.cancel_event is None or agent.cancel_event.is_set():
            return self._json({"ok": True, "running": False})
        agent.stop()
        log.info("[会话 %s] 用户请求停止生成", sid)
        self._json({"ok": True, "running": True})

    def _handle_truncate(self, sid: str):
        """回退编辑（ZCode editUserQuery 的 rewind 语义，V1 不带文件回卷）：
        删除某条用户消息【及其后】的全部消息，前端随后重拉时间线并把编辑后的
        内容作为新的一轮发出。被压缩进摘要的旧消息拒绝回退（摘要引用会悬空）。

        会话锁内执行（与回合 worker 串行）；锁内确认无运行中回合——防御
        前端的 streaming 判断失灵（别的标签页正在跑时这里会拦住）。
        """
        mid = str(self._body().get("mid") or "")
        if not mid:
            return self._json({"error": "缺少 mid"}, 400)
        with _session_lock(sid):
            if _running_agents.get(sid) is not None:
                return self._json({"error": "任务正在运行，请先停止再回退"}, 409)
            try:
                out = db.truncate_from(sid, mid)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            agent = _agents.get(sid)
            if agent is not None:
                # 内存与库同一口径：丢弃切点及之后的消息（含 compact 标记——
                # db 层已保证切点在边界之后，这里只可能筛掉活区消息）与指纹
                # 账本条目。没有 _ord 的消息（理论不存在）保守保留。
                agent.history = [m for m in agent.history
                                 if m.get("_ord") is None or m.get("_ord") < out["ord"]]
                alive = {m.get("_mid") for m in agent.history}
                agent.saved = {k: v for k, v in agent.saved.items() if k in alive}
            _event_bus(sid).publish({"type": "history_truncated"})
            log.info("[会话 %s] 回退编辑：删除 %d 条消息（切点 ord=%d）",
                     sid, out["removed"], out["ord"])
            self._json({"ok": True, "removed": out["removed"]})

    def _handle_permission(self, sid: str, pid: str):
        """权限确认的决定回令：{"decision": "allow"|"allow_session"|"deny"}。

        时刻注意：回合 worker 此刻正阻塞在闸门的等待上（permission_request
        已发出），本 handler 跑在 HTTP 线程、只调 agent.resolve_permission 在
        闸门上 set 一个 Event 唤醒它——不持会话锁、不碰消息历史。等待超过
        PERMISSION_ASK_TIMEOUT 无人应答会按拒绝收场，此后迟到的点击在这里
        得到 ok=false（前端提示"确认已失效"）。会话实例被重建（切模型/工作区）
        后 pending 随旧实例丢弃，同样落到 ok=false——等待方超时兜底，无悬挂。"""
        decision = str(self._body().get("decision") or "")
        agent = _agents.get(sid)
        if agent is None:
            return self._json({"ok": False, "error": "任务不存在或已重建，确认已失效"}, 404)
        if decision not in ("allow", "allow_session", "deny"):
            return self._json({"error": "decision 须为 allow / allow_session / deny"}, 400)
        ok = agent.resolve_permission(pid, decision)
        if not ok:
            return self._json({"ok": False, "error": "确认请求不存在或已被处理"}, 404)
        self._json({"ok": True})

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
        # around_ord：导航条跳到「窗口之外」的消息时用——以该 ord 为中心取一页，
        # 前端据此把它所在的窗口加载进时间线（返回的是窗口内 mid/ord/role 索引，
        # 正文仍走常规分页，避免在这里重复实现一套渲染数据组装）。
        raw_around = qs.get("around_ord", [None])[0]
        try:
            around_ord = int(raw_around) if raw_around is not None else None
        except (TypeError, ValueError):
            return self._json({"error": "around_ord 须为整数"}, 400)
        if around_ord is not None:
            win = db.window_around_ord(sid, around_ord, before=limit, after=limit)
            return self._json({"window": win, "user_index": db.user_message_index(sid)})
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
                item = {"role": role, "content": m["content"], "ord": m["_ord"],
                        "mid": m["_mid"], "stats": m.get("_stats")}
                # 带 tool_calls 的中间 assistant 消息必须把这个字段带出去：它的
                # 正文只是"过程说明"（如"现在开始改"），已随最终回答的 trace 落库
                # （type=process_text），前端 historyNode 靠 tool_calls 判断"这条
                # 不进时间线"，否则回放时每段过程说明都变回一张正文卡片——与实时
                # 视图（降级进执行过程面板）割裂，时间线被大量碎句刷屏。
                # 曾经漏带：前端拿不到 tool_calls，判空恒成立，过滤形同虚设。
                if m.get("tool_calls"):
                    item["tool_calls"] = m["tool_calls"]
                items.append(item)
        # 执行过程轨迹按本页的 mid 批量取（只有最终回答消息才有）：前端历史
        # 回放渲染折叠条，点开可看当时每一步做了什么
        answer_mids = [it["mid"] for it in items if it["role"] == "assistant"]
        traces = db.get_traces(sid, answer_mids)
        for it in items:
            if it["mid"] in traces:
                it["trace"] = traces[it["mid"]]
        # has_more：本页最小 ord 之前还有更早的消息（向上翻页入口的显隐依据）
        has_more = bool(items) and db.has_messages_before(sid, items[0]["ord"])
        # user_index：本会话【全部】用户提问的轻量索引（mid+ord，不含正文）。
        # 左侧导航条用它一次性画出整个会话的提问分布，不受「只加载最近 N 条」
        # 的窗口限制；点击某条时若尚未加载，再用 before_ord 分页把那一页取回来。
        self._json({"messages": items, "has_more": has_more,
                    "user_index": db.user_message_index(sid)})

    def _handle_git(self, path: str):
        """Git 浮窗数据源：/api/git/log、/api/git/show、/api/git/summary。

        全部只读（git log/show/config），因此不过权限闸门。工作区按 session_id
        解析（与 Agent 工具用的是同一套 _resolve_workspace）——浮窗看到的就是
        当前任务真正在操作的目录，不是服务进程的 cwd。
        """
        sid = (self._query().get("session_id") or [""])[0]
        if sid and db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        # workspace 直传（新任务态）：会话还没创建时前端预选了项目，徽章/浮窗
        # 也要能立刻显示该项目的分支名——传目录路径直接查询。目录必须真实存在，
        # 且只允许绝对路径（与提交消息绑工作区同一套校验思路）；不传则走会话解析。
        ws_req = (self._query().get("workspace") or [""])[0]
        if ws_req and not sid:
            ws_target = Path(ws_req).expanduser().resolve()
            if not ws_target.is_dir() or ws_target == Path(ws_target.root):
                return self._json({"ok": False, "reason": "no_workspace",
                                   "error": "目录不存在或不可用"}, 200)
            ws = ws_target
        else:
            ws = _resolve_workspace(self.user["id"], sid)
        if ws is None:
            return self._json({"ok": False, "reason": "no_workspace",
                               "error": "该任务还没有绑定项目文件夹"}, 200)
        try:
            if path == "/api/git/summary":
                return self._json({"ok": True, **repo_summary(ws),
                                   "identity": git_identity(ws)})
            if path == "/api/git/log":
                qs = self._query()
                try:
                    limit = min(100, max(1, int((qs.get("limit") or ["30"])[0])))
                    offset = max(0, int((qs.get("offset") or ["0"])[0]))
                except ValueError:
                    return self._json({"error": "limit/offset 须为整数"}, 400)
                # author=me 时用本机 git email 过滤；author=other 时前端拿到
                # 全量后自行剔除自己（git --author 不支持"非"语义）
                who = (qs.get("author") or ["all"])[0]
                me = git_identity(ws).get("email", "")
                author = me if who == "me" and me else ""
                data = git_log(ws, limit=limit, offset=offset, author=author)
                if who == "other" and me:
                    data["commits"] = [c for c in data["commits"]
                                       if c["email"].lower() != me.lower()]
                return self._json({"ok": True, **repo_summary(ws), **data})
            if path == "/api/git/show":
                commit_hash = (self._query().get("hash") or [""])[0]
                if not _GIT_HASH_RE.fullmatch(commit_hash):
                    return self._json({"error": "非法的提交 hash"}, 400)
                return self._json({"ok": True, **git_show(ws, commit_hash)})
            if path == "/api/git/branches":
                return self._json({"ok": True, **git_branches(ws)})
            return self._json({"error": "未知的 git 接口"}, 404)
        except GitError as e:
            # 不是仓库 / 空仓库（无提交）等都从这里出去：前端显示提示文案，
            # 不是错误弹窗——"这个文件夹不是 git 仓库"是正常状态而非故障
            return self._json({"ok": False, "reason": "not_repo",
                               "error": str(e)}, 200)

    def _handle_git_checkout(self):
        """切换工作区所在仓库的分支（本模块唯一的 git 写操作）。

        分支名必须已在本地分支白名单里（git_tools.checkout 里校验），因此
        不接受任意字符串；工作树有未提交改动时 git 会拒绝并原样报错——
        绝不 --force 丢弃用户的改动。
        """
        body = self._body()
        sid = str(body.get("session_id") or "")
        if sid and db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        ws = _resolve_workspace(self.user["id"], sid)
        if ws is None:
            return self._json({"ok": False, "reason": "no_workspace",
                               "error": "该任务还没有绑定项目文件夹"}, 200)
        branch = str(body.get("branch") or "").strip()
        if not branch:
            return self._json({"error": "branch 不能为空"}, 400)
        try:
            git_checkout(ws, branch)
            return self._json({"ok": True, **repo_summary(ws),
                               **git_branches(ws)})
        except GitError as e:
            # 未提交改动冲突 / 分支不存在等：把 git 的原话给用户看
            return self._json({"ok": False, "error": str(e)}, 200)

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

    def _handle_session_docs(self, sid: str):
        """本会话的文档：无 name 参数 = 列出全部；带 name = 读单个 md 原文。

        归属已在上层校验（session_owner）。name 是不可信输入，db 层做 realpath
        白名单校验（防 ../ 逃逸、跨会话、非 .md），越界即 400。"""
        name = (self._query().get("name") or [""])[0]
        if not name:
            return self._json({"docs": db.list_docs(sid)})
        try:
            content = db.read_doc(sid, name)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except (OSError, FileNotFoundError):
            return self._json({"error": "文档不存在"}, 404)
        self._json({"name": name, "content": content})

    def _handle_browser_shot(self, sid: str):
        """浏览器工具推送的截图（PNG 字节流）。?n=<序号> 指定第几张，
        缺省取最新一张。归属已在上层校验；n 只允许数字，杜绝路径逃逸。"""
        qs = self._query()
        n = (qs.get("n") or [""])[0]
        root = Path("data/browser-shots") / sid
        if n.isdigit():
            target = root / f"shot-{int(n):04d}.png"
        else:
            shots = sorted(root.glob("shot-*.png")) if root.is_dir() else []
            target = shots[-1] if shots else None
        if not target or not target.is_file():
            return self._json({"error": "截图不存在"}, 404)
        try:
            data = target.read_bytes()
        except OSError:
            return self._json({"error": "截图读取失败"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_session_attachments(self, sid: str):
        """本会话的附件（用户上传、已落盘的文件类附件）：列表 / 读单个。

        无 name 参数 = 列出全部；带 name = 读该附件内容。与 docs 接口同构，
        但只读——浮窗是「查看器」，不提供上传/删除/编辑（上传仍走 📎 与
        /api/sessions/<sid>/messages 的 attachments 字段）。

        只回文本：二进制与压缩包不给字节流（base64 会把响应撑大好几倍，
        而且任意字节不该直接进 DOM），改为回结构描述——压缩包列成员清单，
        二进制只报类型，让用户知道"这是什么、能不能在这儿看"。

        归属已在上层校验（session_owner）。name 是不可信输入，db._attach_path
        做 realpath 白名单校验（防 ../ 逃逸、跨会话），越界即 400。
        """
        name = (self._query().get("name") or [""])[0]
        if not name:
            items = db.list_attachments(sid)
            return self._json({"attachments": items,
                               "total_bytes": sum(it["bytes"] for it in items)})
        qs = self._query()
        try:
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
            limit = min(5000, max(1, int((qs.get("limit") or ["2000"])[0])))
        except ValueError:
            return self._json({"error": "offset/limit 须为整数"}, 400)
        try:
            info = db.read_attachment_text(sid, name, offset=offset, limit=limit)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except FileNotFoundError:
            return self._json({"error": "附件不存在"}, 404)
        except OSError as e:
            return self._json({"error": f"附件读取失败：{e}"}, 500)
        # 归档 / 二进制：只回描述，不回内容
        if info.get("kind") in ("archive", "binary"):
            return self._json(info)
        self._json(info)

    # ---------- 模型供应商 ----------

    def _handle_active_model(self):
        """切换激活模型。带 session_id = 只改该任务用哪个模型；不带 = 改全局默认，
        即"新任务的初始模型"（新任务还没有会话行，无法按会话存）。
        校验的是"该供应商下存在这个模型"而非"已启用"：与旧行为一致，已停用的
        模型仍可被显式选中（解析时会回落，但用户的显式选择不被静默吞掉）。"""
        body = self._body()
        pid, model = body.get("provider_id"), body.get("model")
        sid = str(body.get("session_id") or "").strip()
        if sid and db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        prov = db.get_provider(str(pid)) if pid else None
        if prov is None:
            return self._json({"error": "供应商不存在"}, 400)
        if model not in [m["name"] for m in prov["models"]]:
            return self._json({"error": f"供应商 {prov['name']} 下没有模型 {model}"}, 400)
        if sid:
            db.set_session_model(sid, prov["id"], model)
            log.info("会话 %s 的模型切换为 %s / %s", sid, prov["name"], model)
        else:
            db.set_setting("active_model", {"provider_id": prov["id"], "model": model})
            log.info("默认模型（新任务初始模型）切换为 %s / %s", prov["name"], model)
        self._json({"ok": True, **self._config_view(sid or None)})

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

    def _handle_session_rename(self, sid: str):
        """重命名任务标题。自动标题（首轮提交时取输入前 24 字）之外，用户
        可以在左侧任务列表里改任意名字——存 title 字段，与其他展示共用。"""
        title = str(self._body().get("title") or "").strip()
        if not title or len(title) > 80:
            return self._json({"error": "标题须为 1-80 字符"}, 400)
        db.set_session_title(sid, title)
        self._json({"ok": True, "title": title})

    def _handle_perm_mode(self, sid: str):
        """读/写会话的权限模式（readonly|confirm|yolo）。按工作区记忆：
        同一项目文件夹的新任务沿用上次选择的模式（与用户规则的隔离粒度一致）；
        无工作区的会话落在空 key 下（等于全局默认）。闸门每次判定经 mode_loader
        现读，切换后下一轮工具调用立即生效，无需重建会话。"""
        ws = db.get_session_workspace(sid) or ""
        if self.command == "POST":
            mode = str(self._body().get("mode") or "").strip()
            if mode not in permissions.MODES:
                return self._json({"error": f"mode 须为 {'/'.join(permissions.MODES)}"}, 400)
            db.set_setting(f"perm_mode:{ws}", mode)
            log.info("[会话 %s] 权限模式切换为 %s（工作区 %s）", sid, mode, ws or "无")
        self._json({"mode": db.get_setting(f"perm_mode:{ws}", "confirm")})

    def _handle_session_seen(self, sid: str):
        """把某会话的绿/红点标记为"已读"（清掉未读徽标）。

        未读标记语义：回合在"用户不在这个会话里"时跑完才亮绿/红点，用户切进去
        看过就清掉。前端在 switchSession 与前台跑完的 turn_end 处调用本接口。
        只改内存态：进程重启后标记本就该归零。"""
        with _lock:
            st = _turn_status.get(sid)
            if st is not None:
                st["seen"] = True
        self._json({"ok": True})

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
    db.cleanup_orphan_attachments()  # 兜底：清理无主会话的附件目录（防 kill -9 残留）
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
