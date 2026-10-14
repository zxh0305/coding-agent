"""Handler 基类：鉴权、响应工具、路由分发壳
========================================

这是原 app.py 里 `class Handler(SimpleHTTPRequestHandler)` 的**方法级拆分**产
物：类属性与基础方法（鉴权、响应工具、do_GET/do_POST/do_DELETE 三条分发链、
以及不属于某个业务域的少量方法）留在这里；各业务域的 _handle_* 方法以 mixin
形式混入（见 routes/__init__.py）。

两条硬约束（本次重排必须守住）：
1. do_GET/do_POST/do_DELETE 的分发链**逐字保留**（含分支顺序、正则、404 语义），
   因为 URL 契约与测试都依赖它；
2. app.py 里的 `class Handler(...)` 通过多继承组合本基类与各 mixin——类对象、
   方法解析顺序与搬移前一致，`app.Handler._handle_truncate(...)` 照旧可用。
"""

import json
import re
import urllib.parse
from http.server import SimpleHTTPRequestHandler

import logging

import db
from config import FRONTEND_DIR
from services.messages import MAX_BODY_BYTES
from services.model_resolve import _active_window, _mask, _model_vision, _resolve_active
from state import _agents, _buses, _ctx, _lock, _running_agents, _session_locks, _turn_status

# 本模块用的日志器与 app 同名同源（logging 里同名即同一对象），因此无需从 app
# import——避免 routes → app 的反向依赖（那会形成循环 import）。app.py 里
# `log = logging.getLogger("app")`，这里 getLogger("app") 拿到的是同一个 logger。
log = logging.getLogger("app")


class _BodyTooLarge(Exception):
    """请求体超过 MAX_BODY_BYTES。由 _body 抛出、do_POST/do_DELETE 捕获成 413。

    为什么用异常而不是返回空字典：空字典会让 handler 按"字段缺失"回一个语义
    错误的 400（前端显示"用户名不能为空"，而用户其实传了个 13MB 的日志包）。
    异常能把"这是体积问题"这一事实带到统一错误出口，文案才说得清。
    """

    def __init__(self, length: int):
        super().__init__(f"请求体 {length} 字节超过上限 {MAX_BODY_BYTES}")
        self.length = length


class HandlerBase(SimpleHTTPRequestHandler):
    """API 路由 + 静态文件托管的公共骨架（鉴权 / 响应 / 分发）。"""

    # HTTP/1.1：启用 keep-alive，一个 TCP 连接复用多条请求。
    # 默认（BaseHTTPRequestHandler.protocol_version = "HTTP/1.0"）下每个请求
    # 都要新建 TCP 连接——前端一次页面加载会并发打十几个 /api/*（config、
    # sessions、context、内存、git…），每次 3 次握手不是小开销；在公网/隧道
    # （用户会从手机或外网访问）下尤其明显。实测本机响应头此前是 HTTP/1.0。
    #
    # 1.1 要求响应必须带 Content-Length 或分块编码，否则客户端会一直等后续
    # 内容而挂住。本项目的 JSON/静态响应都由 _json 与 SimpleHTTPRequestHandler
    # 带 Content-Length；SSE 是唯一的例外，它【显式】把自己降回 1.0 +
    # Connection: close（见 sessions._handle_session_events）——那是刻意的：
    # 长连接不能被 keep-alive 复用，且该处依赖"连接结束即帧边界"的语义。
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        # 每次响应前重置（同一连接可能复用；BaseHTTPRequestHandler 每个请求
        # 都新建实例，但显式初始化更稳，也让 end_headers 的判断无歧义）。
        self._cache_control_sent = False
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def send_header(self, keyword, value):
        """记录 Cache-Control 是否已发，供 end_headers 去重。

        不走 self._headers_buffer：那是 [(keyword, value)] 的列表，但需要
        逐条解析字符串、且不同 Python 版本实现有差异，直接在这里打标最可靠。
        """
        if keyword.lower() == "cache-control":
            self._cache_control_sent = True
        super().send_header(keyword, value)

    def log_message(self, fmt, *args):  # 访问日志降级为 DEBUG，不刷屏
        log.debug("HTTP %s", fmt % args)

    def end_headers(self):
        # 禁止缓存：浏览器对静态文件有启发式缓存，曾出现"新 index.html 配旧 app.js"
        # 的版本错配——旧脚本在新页面上找不到元素，执行中断，所有按钮集体失灵。
        # 本地开发页面每次都重新验证（文件没变时服务端回 304，开销极小）。
        #
        # 只在【别名未发过】时补一条：SSE 端点自己发过 Cache-Control: no-cache，
        # 此前这里会再发一次，响应里于是有两个同名头（实测 SSE 响应确实如此：
        # `Cache-Control: no-cache` 紧跟 `Cache-Control: no-cache, must-revalidate`）。
        # 重复头不出错，但要靠"后一条覆盖前一条"的隐式规则，中间层（代理/网关）
        # 解释顺序未必一致——统一成一条更省心。
        if not self._cache_control_sent:
            self._cache_control_sent = True
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
        # 请求体上限：单用户本地服务，但没上限的话一个超大 body 会被原样
        # read 进内存再 json.loads——白白占用内存与一个线程。图片附件走
        # base64 内联（单图上限 MAX_IMAGE_B64≈6MB），12MB 足够覆盖"一条消息
        # + 若干附件"。
        #
        # 超限时抛 _BodyTooLarge 而不是返回 {}：返回空字典会让各 handler 按
        # "字段缺失"回一个 400，而文案是"用户名不能为空"之类——用户传了个
        # 大附件却被告知输入为空，完全误导（真实踩过：13MB 的日志包上传被
        # 报成"输入不能为空"）。由 do_POST/do_DELETE 统一捕获成 413，
        # 文案直说"请求体过大"并带上限值，才是可操作的反馈。
        if length > MAX_BODY_BYTES:
            log.warning("请求体超限已拒绝: %d 字节 > %d", length, MAX_BODY_BYTES)
            raise _BodyTooLarge(length)
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

    # ---------- 路由分发 ----------
    # 说明：do_GET/do_POST/do_DELETE 的实现见 routes/dispatch.py 的 DispatchMixin，
    # 之所以拆到单独文件，是因为三条链都很长（合计 ~250 行），与"基础骨架"职责
    # 不同；这里保留同名的继承入口，方法解析顺序与搬移前一致。
