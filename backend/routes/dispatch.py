"""三条路由分发链（do_GET / do_POST / do_DELETE）
=============================================

从原 app.py 的 Handler 类逐字搬移：分发顺序、正则、404 语义一字未改——URL 契约
与测试都依赖它。

通过模块属性（见 app.install_service_hooks）引用装配层与业务服务，避免
routes → app 的反向 import：

  GET  /api/sessions,...        → sessions mixin 的 _handle_session_events 等
  POST /api/sessions,...        → sessions / chat / models / workspace mixin
  DELETE /api/sessions,...      → sessions mixin 的 _delete_session_cleanup
"""

import re
import urllib.parse

import db
from services.model_resolve import _active_window, _mask, _resolve_active

from routes import busref

# log 是 busref 的模块常量（不经注入），import 期取值安全；
# 注入点（_session_state 等）一律在调用点写 busref.<名>，不可在此处取别名。
log = busref.log


class DispatchMixin:
    """do_GET / do_POST / do_DELETE 三条分发链。"""

    def do_GET(self):
        # 与 do_POST 同款兜底：GET 链路任何接口异常原先会一路抛到 socketserver
        # ——socket 被直接关掉、不回任何 HTTP 响应，前端 fetch 只能报"无法连接
        # 后端服务"，把代码 bug 伪装成服务没启动。这里统一转成 500 JSON。
        # 95a0572 曾经在此真实翻车：历史接口的 property 误加括号调用，前端只见
        # "连不上后端"，真实堆栈只在进程日志里。
        try:
            self._do_get()
        except Exception:
            log.exception("接口处理出错")  # 完整堆栈进 agent.log
            try:
                self._json({"error": "服务器内部错误，详情见 backend 日志"}, 500)
            except Exception:
                pass  # SSE 等已开始写响应的接口报不了 500，只能断开（与原行为一致）

    def _do_get(self):
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
        elif path == "/api/usage/summary":
            # 模型用量汇总（管理模型弹窗的"用量"页）。days 缺省 7。
            days = self._query().get("days", ["7"])[0]
            days = int(days) if str(days).isdigit() and int(days) > 0 else None
            self._json(db.usage_summary(days))
        elif path == "/api/usage/sessions":
            # 用量下钻：某供应商/模型分别花在哪些会话上（用量页模型行展开）。
            q = self._query()
            days = q.get("days", ["7"])[0]
            days = int(days) if str(days).isdigit() and int(days) > 0 else None
            self._json(db.usage_session_rows(days, q.get("provider_id", [""])[0],
                                             q.get("model", [""])[0]))
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
            from tools import TOOL_SCHEMAS
            self._json({"tools": [
                {"name": t["function"]["name"],
                 "description": t["function"]["description"],
                 "parameters": t["function"]["parameters"]}
                for t in TOOL_SCHEMAS
            ]})
        elif self.path.split("?")[0] == "/api/sessions":
            # 注意 self.path 含查询串，必须先剥掉再比较，否则 ?archived=1 会 404
            archived = (self._query().get("archived") or ["0"])[0]
            rows = db.list_sessions(self.user["id"], archived=int(archived == "1"))
            for r in rows:  # state 是内存态，db 层不掺和，在这里现算后随列表带回
                r["state"] = busref._session_state(r["id"])
            # 预览行：每会话最后一条消息的一句话摘要（一条 SQL 批量取，不逐会话查）
            previews = db.last_message_previews([r["id"] for r in rows])
            for r in rows:
                r["preview"] = previews.get(r["id"], "")
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
            if sub == "todos":
                # 任务清单：todo_write 落库后的最新一份（空数组 = 已清空）
                return self._json({"todos": db.get_todos(sid)})
            return self._handle_session_messages(sid)
        elif self.path.startswith("/api/context"):
            sid = (self._query().get("session_id") or [""])[0]
            if sid and db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            stat = busref._ctx.get(sid, {})
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
            elif re.fullmatch(r"/api/sessions/[^/]+/compact", path):
                # /compact 斜杠命令：手动触发上下文压缩
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                self._handle_compact(sid)
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
            elif re.fullmatch(r"/api/sessions/[^/]+/archive", path):
                # 归档：从任务栏消失，进归档区。运行中的任务也允许归档（回合在
                # 后台继续跑完，事件流照常推送，只是列表里看不见了）。
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                db.archive_session(sid, 1)
                log.info("归档会话 %s", sid)
                self._json({"ok": True, "sid": sid, "archived": 1})
            elif re.fullmatch(r"/api/sessions/[^/]+/unarchive", path):
                sid = path.split("/")[3]
                if db.session_owner(sid) != self.user["id"]:
                    return self._json({"error": "任务不存在或不属于当前用户"}, 404)
                db.archive_session(sid, 0)
                log.info("取消归档会话 %s", sid)
                self._json({"ok": True, "sid": sid, "archived": 0})
            elif path == "/api/sessions/unarchive":
                # 批量恢复（归档区勾选多行后一键恢复）。逐个校验归属：不属于
                # 当前用户的 id 跳过而不是整体 404——批量操作里混进一个坏 id
                # 不该让其余正常项全部失败。归档区里能被勾选的自然都是归档态，
                # archive_session(0) 本身幂等，不必再逐个查 archived。
                ids = self._body().get("ids")
                if not isinstance(ids, list) or not ids \
                        or not all(isinstance(i, str) and i for i in ids) or len(ids) > 100:
                    return self._json({"error": "ids 须为非空字符串数组（≤100）"}, 400)
                restored, skipped = 0, 0
                for sid in ids:
                    if db.session_owner(sid) != self.user["id"]:
                        skipped += 1
                        continue
                    db.archive_session(sid, 0)
                    restored += 1
                log.info("批量取消归档 %d 个会话（跳过 %d）", restored, skipped)
                self._json({"ok": True, "restored": restored, "skipped": skipped})
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
            # 两种形态：?session_id=xxx 单删（原有入口，归档区行内 🗑）；
            # body {"ids": [...]} 批量删（归档区勾选后的批量删除按钮）。
            # 批量走同一套收摊逻辑，逐个校验归属与归档态——单个坏 id 跳过
            # 计数，不让其余正常项整体失败。
            ids = self._body().get("ids") if self.headers.get("Content-Length") else None
            if isinstance(ids, list):
                if not ids or not all(isinstance(i, str) and i for i in ids) or len(ids) > 100:
                    return self._json({"error": "ids 须为非空字符串数组（≤100）"}, 400)
                deleted, skipped = 0, 0
                for sid in ids:
                    if db.session_owner(sid) == self.user["id"] and db.session_archived(sid):
                        self._delete_session_cleanup(sid)
                        deleted += 1
                    else:
                        skipped += 1
                log.info("批量删除 %d 个会话（跳过 %d）", deleted, skipped)
                return self._json({"ok": True, "deleted": deleted, "skipped": skipped})

            sid = (self._query().get("session_id") or [""])[0]
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            # 硬约束：只有归档区里的任务才允许删除。前端"删除"入口也只在归档区
            # 出现，这里再拦一道，防止直接调接口绕过"会话栏不可删"的设计。
            if not db.session_archived(sid):
                return self._json({"error": "任务未归档，请先归档再删除"}, 409)
            self._delete_session_cleanup(sid)
            log.info("删除会话 %s", sid)
            self._json({"ok": True})
        else:
            self._json({"error": "未知接口"}, 404)
