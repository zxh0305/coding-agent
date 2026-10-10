"""会话路由：提交、事件流、历史分页、归档全文、文档/附件、截图、收摊
=================================================================

从原 app.py 的 Handler 里逐字搬移以下方法：
  _handle_session_submit / _handle_session_events / _handle_session_messages
  _handle_session_artifact / _handle_session_docs / _handle_browser_shot
  _handle_session_attachments / _delete_session_cleanup / _handle_session_rename
  _handle_session_seen / _handle_truncate / _handle_compact
  _delete_session_cleanup 另在 app.install_service_hooks 时回填到 busref，
  供 do_DELETE 的批量删除复用（与原先"方法挂在同一个 Handler 上"等价）。

装配层能力（log / 会话锁 / 会话总线 / 运行状态 / 停止目标 / 记忆提取）统一经
routes.busref 访问（属性取值，注入发生在 import 之后也能生效）。
"""

import base64
import json
import threading
import uuid
from pathlib import Path

import db
from events import SSE_HEARTBEAT, sse_frame
from services.messages import _build_user_message
from services.session_boot import get_session

from routes import busref


class SessionRoutes:
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
        # 【先验校验，再建会话】空输入必须在调用 get_session 之前拦掉：
        # get_session 会在解析模型客户端后【落库创建会话行】；若等到
        # _build_user_message 之后再判空，每个被拒绝的空请求都会在库里留下
        # 一个"空标题、零消息"的孤儿会话（实测：发 5 次空输入 = 5 个垃圾会话，
        # 既脏了任务列表又持续涨库）。这里用与 _build_user_message 同口径的
        # 廉价预检：有正文、或有任一有效附件（图片带 data、文本可解码非空）
        # 才算有输入；预检通过后再走 get_session。
        _text = (body.get("message") or "").strip()
        _has_att = False
        for _a in (body.get("attachments") or [])[:6]:
            _d = str(_a.get("data") or "")
            if not _d:
                # 大附件走分块上传后已落盘，发送时只带 name（无 data）——
                # 有 name 即视为有效附件，不能因"没有内联 data"判成空输入。
                if str(_a.get("name") or "").strip():
                    _has_att = True
                    break
                continue
            if _a.get("kind") == "image":
                _has_att = True
                break
            try:
                if base64.b64decode(_d):
                    _has_att = True
                    break
            except Exception:
                continue
        if not _text and not _has_att:
            return self._json({"error": "输入不能为空"}, 400)
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
            with busref._lock:
                busref._agents.pop(sid, None)  # 丢弃无目录时构建的占位实例，下一回合按绑定目录重建
            agent = get_session(sid, self.user["id"])[1]
        title = next((s["title"] for s in db.list_sessions(self.user["id"]) if s["id"] == sid), "")
        if not title:
            db.set_session_title(sid, plain[:24])
        # 附件只带 kind/name 进 turn_start 事件（原文/图片数据太大，不该进
        # 环形缓冲占 500 个格子里的一个——完整内容在消息存储里）
        atts = [{"kind": a.get("kind"), "name": str(a.get("name") or "")[:80]}
                for a in (body.get("attachments") or [])[:6]]
        nonce = str(body.get("nonce") or uuid.uuid4().hex[:12])
        threading.Thread(target=busref._run_round, daemon=True,
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
        bus = busref._event_bus(sid)
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
        # 进行中回合的快照：切走再切回的页面靠它补画执行过程（含已累积的思考
        # 内容）。回合结束后快照行已删、正式轨迹已按 answer_mid 落库，这里自然
        # 取不到——running=false 时这笔查询直接跳过，正常路径零额外开销。
        if busref._session_state(sid) == "running":
            user_mid = next((it["mid"] for it in reversed(items)
                             if it["role"] == "user"), None)
            snap = db.get_trace(sid, user_mid) if user_mid else None
            if snap:
                _bus = busref._buses.get(sid)
                return self._json({"messages": items, "has_more": has_more,
                                   "user_index": db.user_message_index(sid),
                                   "running_trace": snap,
                                   "running_started_at":
                                       (_bus.round_started_at if _bus else None)})
        # user_index：本会话【全部】用户提问的轻量索引（mid+ord，不含正文）。
        # 左侧导航条用它一次性画出整个会话的提问分布，不受「只加载最近 N 条」
        # 的窗口限制；点击某条时若尚未加载，再用 before_ord 分页把那一页取回来。
        self._json({"messages": items, "has_more": has_more,
                    "user_index": db.user_message_index(sid)})

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

    # ---------- 大附件分块上传（begin / chunk / commit / abort） ----------
    # 参照 ZCode 的三段式上传：每块都是独立的小 POST（远小于 MAX_BODY_BYTES），
    # 于是"附件多大"与"请求体多大"解耦，单附件上限得以上到 100MB。详见 db.staging_*。

    def _handle_attach_upload_begin(self, sid: str):
        """开启一次分块上传：{name, size, total_chunks, sha256?} → {upload_id}。"""
        body = self._body()
        try:
            info = db.staging_begin(
                sid, body.get("name"), body.get("size"),
                body.get("total_chunks"), body.get("sha256") or "")
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        self._json(info)

    def _handle_attach_upload_chunk(self, sid: str):
        """写入一个分块：{upload_id, chunk_index, data(base64)} → {next_chunk_index}。"""
        body = self._body()
        upload_id = str(body.get("upload_id") or "")
        raw = str(body.get("data") or "")
        if not raw:
            return self._json({"error": "分块内容为空"}, 400)
        try:
            blob = base64.b64decode(raw)
        except Exception:
            return self._json({"error": "分块不是合法 base64"}, 400)
        try:
            info = db.staging_put_chunk(
                sid, upload_id, body.get("chunk_index"), blob)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except FileNotFoundError:
            return self._json({"error": "上传会话不存在或已过期，请重新上传"}, 404)
        self._json(info)

    def _handle_attach_upload_commit(self, sid: str):
        """拼装 → 校验 → 落成正式附件：{upload_id} → {name, bytes}。"""
        body = self._body()
        try:
            info = db.staging_commit(sid, str(body.get("upload_id") or ""))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except FileNotFoundError:
            return self._json({"error": "上传会话不存在或已过期"}, 404)
        self._json({"name": info["name"], "bytes": info["bytes"]})

    def _handle_attach_upload_abort(self, sid: str):
        """放弃一次上传（幂等）。"""
        body = self._body()
        db.staging_abort(sid, str(body.get("upload_id") or ""))
        self._json({"ok": True})

    # ---------- 会话维护：重命名 / 已读 / 回退 / 压缩 / 删除收摊 ----------

    def _handle_session_rename(self, sid: str):
        """重命名任务标题。自动标题（首轮提交时取输入前 24 字）之外，用户
        可以在左侧任务列表里改任意名字——存 title 字段，与其他展示共用。
        这里同时置 title_manual=1：用户亲手动过的名字，首轮结束的 AI 标题
        总结绝不能再覆盖（见 db.set_session_title 的 manual 参数）。"""
        title = str(self._body().get("title") or "").strip()
        if not title or len(title) > 80:
            return self._json({"error": "标题须为 1-80 字符"}, 400)
        db.set_session_title(sid, title, manual=True)
        self._json({"ok": True, "title": title})

    def _handle_session_seen(self, sid: str):
        """把某会话的绿/红点标记为"已读"（清掉未读徽标）。

        未读标记语义：回合在"用户不在这个会话里"时跑完才亮绿/红点，用户切进去
        看过就清掉。前端在 switchSession 与前台跑完的 turn_end 处调用本接口。
        只改内存态：进程重启后标记本就该归零。"""
        with busref._lock:
            st = busref._turn_status.get(sid)
            if st is not None:
                st["seen"] = True
        self._json({"ok": True})

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
        with busref._session_lock(sid):
            if busref._running_agents.get(sid) is not None:
                return self._json({"error": "任务正在运行，请先停止再回退"}, 409)
            try:
                out = db.truncate_from(sid, mid)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            agent = busref._agents.get(sid)
            if agent is not None:
                # 内存与库同一口径：丢弃切点及之后的消息（含 compact 标记——
                # db 层已保证切点在边界之后，这里只可能筛掉活区消息）与指纹
                # 账本条目。没有 _ord 的消息（理论不存在）保守保留。
                agent.history = [m for m in agent.history
                                 if m.get("_ord") is None or m.get("_ord") < out["ord"]]
                alive = {m.get("_mid") for m in agent.history}
                agent.saved = {k: v for k, v in agent.saved.items() if k in alive}
            busref._event_bus(sid).publish({"type": "history_truncated"})
            busref.log.info("[会话 %s] 回退编辑：删除 %d 条消息（切点 ord=%d）",
                            sid, out["removed"], out["ord"])
            self._json({"ok": True, "removed": out["removed"]})

    def _handle_compact(self, sid: str):
        """/compact 斜杠命令：手动触发上下文压缩（agent.compact_now 的 force 路径）。

        会话锁内执行（与回合 worker 串行）、锁内确认无运行中回合——压缩要改
        agent.history，与正在流式写历史的回合并发会互相踩。成功后做三件收尾
        （与回合收尾的自动压缩同口径）：增量落盘、推 compacted 事件（前端复用
        自动压缩的处理：插分隔卡 + 刷容量徽章）、更新容量缓存。
        """
        with busref._session_lock(sid):
            if busref._running_agents.get(sid) is not None:
                return self._json({"error": "任务正在运行，请先停止再压缩"}, 409)
            agent = busref._agents.get(sid)
            if agent is None:
                return self._json({"error": "会话未加载（服务重启后发一条消息即可恢复）"}, 409)
            result = agent.compact_now()
            if result.get("compacted"):
                written = db.save_messages(sid, agent.history, agent.saved)
                db.touch_session(sid)
                busref.log.info("[会话 %s] /compact 压缩完成，落盘 %d 行", sid, written)
                payload = {"summary": result.get("summary"),
                           "prompt_tokens": result.get("prompt_tokens"),
                           "context": result.get("context")}
                busref._event_bus(sid).publish({"type": "compacted", **payload})
                busref._ctx[sid] = payload
            else:
                busref.log.info("[会话 %s] /compact 未触发：%s", sid, result.get("reason"))
            self._json(result)

    def _delete_session_cleanup(self, sid: str):
        """删除一个会话的全部收摊动作（原 do_DELETE 内联逻辑提为方法，供单删
        与批量删共用）：落库删除、清内存态、广播 session_deleted 后关总线。"""
        db.delete_session(sid)
        with busref._lock:
            busref._agents.pop(sid, None)
            busref._ctx.pop(sid, None)
            busref._turn_status.pop(sid, None)
            bus = busref._buses.pop(sid, None)
        if bus is not None:
            # 先发 session_deleted 再关总线：其他标签页的常驻连接收到后
            # 自行收摊（切走/清空界面），close 的哨兵再把连接线程送终
            bus.publish({"type": "session_deleted"})
            bus.close()
