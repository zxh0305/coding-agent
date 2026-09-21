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
  GET  /api/sessions/<id>/messages  某任务的历史消息（须是自己的任务）
  DELETE /api/sessions?session_id=  删除任务（须是自己的任务）
  GET  /api/context?session_id=   该任务当前上下文容量
  POST /api/chat/stream           流式问答（SSE）
  POST /api/chat/stop             停止指定任务的生成 {"session_id"}

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
from llm_client import create_client, load_env_file, save_env_values
from logger import setup_logging
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
    """激活模型的上下文窗口（供应商配置里每个模型可单独设）。"""
    prov, model = _resolve_active()
    for m in prov["models"]:
        if m["name"] == model and m["context_window"]:
            return m["context_window"]
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
                          workspace=workspace, vision_backend=_vision_backend)
            agent.history = db.get_messages(sid)  # 重启/换模型后从库里恢复对话
            _agents[sid] = agent
            _sigs[sid] = sig
            log.info("会话 %s Agent 就绪 model=%s 工作区=%s（历史 %d 条，视觉=%s）",
                     sid, model, workspace, len(agent.history), vision)
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
        通过后把用户挂在 self.user 上，后续接口直接用。
        """
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            self.user = None
            return True
        user = self._auth_user()
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
        if self.path == "/api/auth/me":
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
        elif self.path.startswith("/api/sessions/"):  # /api/sessions/<id>/messages
            sid = self.path.split("/")[3]
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            msgs = [{"role": m["role"], "content": m.get("content") or "",
                     "stats": m.get("_stats")}  # 助手消息带回耗时/token 统计（回放渲染用）
                    for m in db.get_messages(sid)
                    if m.get("role") in ("user", "assistant") and m.get("content")]
            self._json(msgs)
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
            if self.path == "/api/chat":
                self._handle_chat()
            elif self.path == "/api/chat/stream":
                self._handle_chat_stream()
            elif self.path == "/api/chat/stop":
                self._handle_chat_stop()
            elif self.path == "/api/active-model":
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

    # ---------- 问答 ----------

    def _handle_chat(self):
        body = self._body()
        plain, user_message = _build_user_message(body)
        if not plain:
            return self._json({"error": "输入不能为空"}, 400)
        # get_session 内部已用锁保护实例缓存；这里不再持全局锁——
        # 一次问答可能跑几分钟，全程持锁会卡死其他请求（含停止）。
        # 只拿【本任务】的锁：同一任务并发提问串行化，不同任务并行。
        sid, agent = get_session(body.get("session_id"), self.user["id"])
        with _session_lock(sid):
            try:
                if not next((s["title"] for s in db.list_sessions(self.user["id"]) if s["id"] == sid), ""):
                    db.set_session_title(sid, plain[:24])
                log.info("[会话 %s] 用户提问: %s", sid, plain)
                answer = agent.chat(plain, user_message)
                log.info("[会话 %s] 最终回答: %s", sid, answer)
            except RuntimeError as e:
                log.exception("LLM 请求失败")
                db.replace_messages(sid, agent.history)  # 部分历史也落盘
                return self._json({"error": str(e), "session_id": sid}, 502)
            self._finish_round(sid, agent)
            self._json({"session_id": sid, "answer": answer, "trace": agent.trace})

    def _handle_chat_stream(self):
        """流式问答：SSE。第一个事件是 session（告知前端任务 id），之后是过程事件。"""
        body = self._body()
        plain, user_message = _build_user_message(body)
        if not plain:
            return self._json({"error": "输入不能为空"}, 400)
        # get_session 内部已用锁保护实例缓存；这里不再持全局锁——流式生成
        # 可能持续几分钟，全程持锁曾把删除任务/停止请求全部卡死。
        # 只拿【本任务】的锁：同一任务并发提问串行化，不同任务并行。
        sid, agent = get_session(body.get("session_id"), self.user["id"])
        session_lock = _session_lock(sid)
        with session_lock:
            title = next((s["title"] for s in db.list_sessions(self.user["id"]) if s["id"] == sid), "")
            if not title:
                title = plain[:24]
                db.set_session_title(sid, title)

            self.protocol_version = "HTTP/1.0"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def send_event(obj: dict) -> None:
                self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))

            send_event({"type": "session", "id": sid, "title": title})
            log.info("[会话 %s] 用户提问: %s", sid, plain)
            try:
                for kind, payload in agent.run(plain, user_message):
                    send_event({"type": kind, **payload})
                    if kind == "usage":  # 记录最新上下文容量，供 /api/context 查询
                        _ctx[sid] = payload
                    if kind == "done":
                        log.info("[会话 %s] 最终回答: %s", sid, payload["answer"])
            except RuntimeError as e:
                log.exception("LLM 请求失败")
                send_event({"type": "error", "message": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                # 用户关页面/刷新导致连接断开：推不出事件了，安静收尾（finally 仍会落盘）
                log.info("[会话 %s] 客户端提前断开", sid)
            finally:
                db.replace_messages(sid, agent.history)  # 完整历史落盘（重启后可恢复）
                db.touch_session(sid)

    def _handle_chat_stop(self):
        """停止指定任务的生成：给 Agent 的停止开关置位。

        看护线程随即掐断 LLM 连接，agent.run 带着已生成的部分内容收尾，
        SSE 上照常收到 done(stopped=true)。这里刻意不拿全局锁——
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

    def _finish_round(self, sid: str, agent: Agent) -> None:
        db.replace_messages(sid, agent.history)
        db.touch_session(sid)

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
        db.upsert_provider(pid, name, base_url, api_key, bool(b.get("enabled", True)), api_format)
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
