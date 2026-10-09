"""回合执行体（POST 只入队，真正的生成在这里跑）
==============================================

从原 app.py 顶层原样搬移 _run_round。会话锁串行同一任务的回合；所有过程事件
走统一发布口 bus.publish——禁止第二条写入路径。

依赖装配层能力（统一日志 log、会话锁、会话总线、记忆提取启动点），由
app.install_service_hooks() 注入（模块属性，调用时动态取）。

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

import json
import time
import uuid

from agent import Agent

import db
from llm_client import ApiConnectionError, ApiHTTPError, _RETRYABLE_STATUS
from services.browser import _push_browser_screenshot
from services.trace import SNAPSHOT_INTERVAL, _persist_trace, _snapshot_trace

# 由 app.install_service_hooks() 注入的装配层能力。
log = None
_session_lock = None
_event_bus = None
_lock = None
_running_agents = None
_turn_status = None
_ctx = None
_spawn_memory_extraction = None
_spawn_title_generation = None


def _run_round(sid: str, agent: Agent, plain: str, user_message: dict,
               nonce: str, atts: list) -> None:
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
            # 进行中快照（方案A核心）：回合事件实时推给在线订阅者，但切走再切回
            # 的页面靠的是「历史分页 + 环形缓冲补发」。环形缓冲只有 500 条，长回合
            # 的事件量轻松把它挤穿，补发退化成"尽力补尾巴"——切回来的页面就只剩
            # "已工作 N 秒"的空壳，思考内容全部丢失，看起来像卡死。这里把 agent
            # 现场的 trace 快照按 user_mid 键节流落库（session_traces 同表），历史
            # 接口发现回合仍在跑时带回 running_trace，前端据此把执行过程折叠条
            # 连同已累积的思考内容一次性补画。收尾时正式轨迹按 answer_mid 落库、
            # 临时快照行删除，同表共存互不干扰。
            snap_mid = user_mid
            last_snap = 0.0

            def _maybe_snapshot(force=False):
                nonlocal last_snap
                now = time.time()
                if not force and now - last_snap < SNAPSHOT_INTERVAL:
                    return
                try:
                    db.set_trace(sid, snap_mid, json.dumps(_snapshot_trace(agent), ensure_ascii=False))
                    last_snap = now
                except Exception:
                    log.exception("[会话 %s] 进行中快照落库失败（忽略）", sid)

            # 子代理事件外发（嵌套 trace）与隐藏消耗归账（用量页），每回合开始
            # 注入一次。事件路径：bus.publish 进环形缓冲/订阅分发（唯一写入路径）；
            # subagent 事件顺带触发进行中快照——长侦察期间父回合没有任何事件，
            # 不触发的话刷新/切回页面的快照会缺整个子代理过程段。
            def _sub_event_sink(evt):
                bus.publish(evt)
                if evt.get("type") == "subagent":
                    _maybe_snapshot()

            agent.event_sink = _sub_event_sink
            agent.usage_sink = (lambda kind, stats, _sid=sid, _agent=agent:
                                db.record_usage(_sid, kind, stats,
                                                _agent.model_tag[0], _agent.model_tag[1]))
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
                        _maybe_snapshot()  # 思考流是"切回来像没反应"的重灾区：跟着 delta 节流落快照
                    else:
                        bus.publish({"type": kind, **payload})
                        if kind in ("tool_call", "tool_result"):
                            _maybe_snapshot()  # 工具步骤同样进快照（非思考模型的回合只有工具流）
                        if kind == "todo_update":
                            # 清单随会话持久化（迁移 19）：右上角清单浮窗在刷新/
                            # 切会话后仍能回放当前清单与完成状态，不再只存内存
                            try:
                                db.save_todos(sid, payload.get("todos") or [])
                            except Exception:
                                log.exception("保存任务清单失败")
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
            # 正式轨迹已按 answer_mid 落库（answer_mid 为 None 也会走到这）：删掉
            # 进行中临时快照行，session_traces 里不留孤儿——否则 get_traces 按
            # 消息 mid 批查虽不会命中它，但行会随时间积累。失败不阻塞收尾。
            try:
                db.delete_trace(sid, user_mid)
            except Exception:
                log.exception("[会话 %s] 临时快照清理失败（忽略）", sid)
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
            # 首轮结束的标题总结：只在【本会话第一次提问】跑一次，用回答文本
            # 把首轮提交时的占位标题（输入前 24 字）换成真正概括任务的短标题。
            # 判据是历史里只有一条真实用户消息（收尾指令/循环提醒是 _synthetic
            # 合成消息，必须排除）；出错的回合不走这里（内容不完整，总结出来的
            # 名字容易跑偏）。线程内自行复查手动改名标记，用户中途改名不会被
            # AI 覆盖。
            try:
                if sum(1 for m in agent.history
                       if m.get("role") == "user" and not m.get("_synthetic")) <= 1:
                    answer = next((m.get("content") for m in reversed(agent.history)
                                   if m.get("role") == "assistant" and m.get("content")), "")
                    _spawn_title_generation(sid, agent.llm.chat, plain, answer)
            except Exception:
                log.exception("[会话 %s] 标题总结启动失败（忽略）", sid)
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
