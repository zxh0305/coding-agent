"""
内置浏览器工具（Browser tools）
===============================

让 agent 能驱动一个真实的 Chromium 完成「验证改动 / 自动登录 / 网页调研」：

  browser_open(url)          打开页面（带着复制的本地浏览器 profile，免登录）
  browser_click(selector)    点击元素
  browser_type(selector, t)  输入文字（密码等敏感值可用 env:VAR 引用环境变量）
  browser_screenshot()       截图给视觉模型"看"，同步推右侧「浏览器」弹窗

设计要点（与项目其它工具模块一致的约定）：
  * 工具层不持有全局状态：浏览器实例挂在 ToolContext.browser 上，
    由 app.py 按【会话】创建/复用/销毁（回合结束自动清理）；
  * 失败走统一信封 {ok:false, error, hint?}，让模型读原因改道；
  * selector 统一用 Playwright 的 text=/css=/xpath= 等引擎前缀语法，
    裸字符串默认按 text= 处理（对模型最友好：直接写按钮文字）。

登录态复用：首次使用时把用户的 Chrome 默认 profile【复制】一份到
data/browser-profiles/<sid>/，之后的 cookies/localStorage 一直留在这份
副本里。为什么复制而不直接用原目录：Chrome 运行中会锁 profile（SingletonLock），
且自动化直接用原目录有污染/风险；副本与真实浏览器互不干扰。

依赖：playwright（pip install playwright && playwright install chromium）。
未安装时工具返回可读错误并给出安装指引——不炸服务，其它功能照常。
"""

import json
import shutil
import threading
from pathlib import Path

import tools as _tools_mod  # error_result（tools.py 先于本模块导入）

# playwright 是可选依赖：导入失败不炸模块，工具调用时给出可读指引
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PW_AVAILABLE = True
except ImportError:
    sync_playwright = None
    PWTimeout = Exception
    _PW_AVAILABLE = False

PW_INSTALL_HINT = "后端未安装 playwright：pip install playwright && playwright install chromium（约 300MB，一次性）"

# 每个 agent 实例（会话）一个浏览器管理器；同一会话内的多次工具调用复用
_MANAGERS: dict = {}
_MANAGERS_LOCK = threading.Lock()

# 截图推送回调：由 app.py 注入 fn(sid, png_bytes, url) —— 截图落库/进 SSE。
# 工具层保持传输无关（与 vision_backend 同一个注入模式）。
screenshot_pusher = None

VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20000
ACTION_TIMEOUT_MS = 8000


def manager_for(sid: str) -> "BrowserManager":
    """取（或创建）一个会话的浏览器管理器。app.py 在回合收尾时调 close_session。"""
    with _MANAGERS_LOCK:
        if sid not in _MANAGERS:
            _MANAGERS[sid] = BrowserManager(sid)
        return _MANAGERS[sid]


def close_session(sid: str) -> None:
    """回合收尾：关掉该会话的浏览器（playwright 进程一并退出），丢掉 profile 引用。
    profile 目录保留在磁盘上——下次同会话再用时登录态还在。"""
    with _MANAGERS_LOCK:
        mgr = _MANAGERS.pop(sid, None)
    if mgr:
        mgr.close()


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str, hint: str = "") -> str:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


class BrowserManager:
    """一个会话的 Chromium 生命周期。惰性启动（第一次工具调用才拉起浏览器）。"""

    def __init__(self, sid: str):
        self.sid = sid
        self._pw = None          # sync_playwright() 返回的 Playwright 对象
        self._ctx = None         # 持久化上下文（带 profile）
        self._page = None
        self.last_shot_b64 = None  # 最新截图的 base64（analyze_image 的浏览器回退路径用）
        self._lock = threading.Lock()  # Playwright sync API 不允许跨线程并发使用

    # ---------- 生命周期 ----------

    def _profile_dir(self) -> Path:
        """本会话的浏览器 profile 副本目录。首次调用时从系统 Chrome 复制。"""
        base = Path("data/browser-profiles") / self.sid
        if base.exists():
            return base
        base.mkdir(parents=True, exist_ok=True)
        src = _chrome_profile_src()
        if src:
            try:
                # 只复制 HTTP Storage（cookies/localStorage/密码自动填充的元数据）
                # ——Cache 等几个 GB 的目录没有复用价值，纯浪费时间与磁盘。
                shutil.copytree(src, base, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(
                                    "Cache*", "Code Cache", "GPUCache",
                                    "Service Worker", "*.tmp"))
            except OSError:
                pass  # 复制失败（权限/磁盘）：退化为全新 profile，登录态需重登
        return base

    def _ensure(self):
        """惰性启动：返回 (page, ok_msg)。失败时抛 RuntimeError（带可读原因）。"""
        if self._page is not None:
            return self._page
        if not _PW_AVAILABLE:
            raise RuntimeError(PW_INSTALL_HINT)
        # 启动/复用在锁内：Playwright 的 sync API 绑定创建线程，跨线程调用
        # 直接报错——所有操作串行化在同一把锁上（本项目工具本来就是串行调度）。
        with self._lock:
            if self._page is not None:
                return self._page
            self._pw = sync_playwright().start()
            try:
                # chromium 而非 chrome channel：playwright install 装的版本，
                # 不依赖用户机器上有没有装 Chrome
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self._profile_dir()),
                    headless=True,
                    viewport=VIEWPORT,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
                self._page.set_default_timeout(ACTION_TIMEOUT_MS)
            except Exception as e:
                self.close()
                raise RuntimeError(f"浏览器启动失败：{e}") from e
        return self._page

    def close(self):
        with self._lock:
            for closer in (
                lambda: self._ctx.close(),
                lambda: self._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass
            self._page = None
            self._ctx = None
            self._pw = None

    # ---------- 内部动作 ----------

    def _push_screenshot(self, note: str = ""):
        """把当前页面截图推给前端（右侧浏览器弹窗）。失败静默：截图推送
        只是可视化，不该让工具调用本身失败。"""
        push = screenshot_pusher
        if push is None or self._page is None:
            return
        try:
            png = self._page.screenshot(type="png")
            push(self.sid, png, self._page.url, note)
        except Exception:
            pass

    @staticmethod
    def _selector(sel: str) -> str:
        """归一化 selector：裸字符串默认按页面文本匹配（text=引擎）。
        带 Playwright 引擎前缀（css= / xpath= / text=）的原样透传。"""
        sel = (sel or "").strip()
        if not sel:
            return sel
        for prefix in ("css=", "xpath=", "text=", "id="):
            if sel.startswith(prefix):
                return sel
        return f"text={sel}"

    def _describe_page(self) -> dict:
        """当前页面的结构化摘要（给模型当"眼睛"用），带超时保护。"""
        p = self._page
        return {
            "title": p.title(),
            "url": p.url,
            "headings": p.eval_on_selector_all(
                "h1, h2, h3", "els => els.slice(0, 10).map(e => e.textContent.trim().slice(0, 120))"),
            "buttons": p.eval_on_selector_all(
                "button, [role=button], a",
                "els => els.slice(0, 15).map(e => (e.textContent || '').trim().slice(0, 60)).filter(Boolean)"),
            "inputs": p.eval_on_selector_all(
                "input, textarea, select",
                "els => els.slice(0, 10).map(e => ({tag: e.tagName.toLowerCase(), "
                "type: e.type || '', name: e.name || '', placeholder: e.placeholder || '', "
                "visible: !!(e.offsetParent || e.type === 'hidden' ? e.offsetParent : e.offsetParent)}))"),
        }

    # ---------- 工具入口（被 browser_tools_* 包装后进 TOOL_REGISTRY） ----------

    def open(self, url: str) -> str:
        try:
            page = self._ensure()
            page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            info = self._describe_page()
            self._push_screenshot("打开页面")
            return _ok({"result": f"已打开 {url}", **info})
        except PWTimeout:
            return _err(f"打开 {url} 超时（{NAV_TIMEOUT_MS // 1000}s）",
                        "检查 URL 是否可达；内网页面确认是否需要先连 VPN")
        except RuntimeError as e:
            return _err(str(e), "在部署机上安装 playwright 后重试")
        except Exception as e:
            return _err(f"打开页面失败：{e}", "检查 URL 格式（须带 http:// 或 https://）")

    def click(self, selector: str) -> str:
        try:
            page = self._ensure()
            page.click(self._selector(selector), timeout=ACTION_TIMEOUT_MS)
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            info = self._describe_page()
            self._push_screenshot(f"点击 {selector}")
            return _ok({"result": f"已点击「{selector}」", **info})
        except PWTimeout:
            return _err(f"找不到可点击的元素「{selector}」或页面加载超时",
                        "先 browser_screenshot 看当前页面，按页面上的真实文字重写 selector")
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"点击失败：{e}", "selector 用页面元素的可见文字（自动按 text= 匹配）")

    def type_text(self, selector: str, text: str) -> str:
        # 敏感值支持 env:VAR 引用：密码不进对话历史、不进模型上下文
        if text.startswith("env:"):
            import os
            var = text[4:].strip()
            text = os.environ.get(var, "")
            if not text:
                return _err(f"环境变量 {var} 未设置或为空",
                            "让用户在部署环境设置该变量后重试（密码不经过对话明文传输）")
        try:
            page = self._ensure()
            page.fill(self._selector(selector), text, timeout=ACTION_TIMEOUT_MS)
            self._push_screenshot(f"输入 {selector}")
            return _ok({"result": f"已在「{selector}」输入 {len(text)} 个字符",
                        "url": page.url})
        except PWTimeout:
            return _err(f"找不到输入框「{selector}」",
                        "先 browser_screenshot 看当前页面；输入框定位用它的 placeholder 或 name")
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"输入失败：{e}")

    def screenshot(self) -> str:
        try:
            page = self._ensure()
            png = page.screenshot(type="png")
            self._push_screenshot("截图")
            import base64
            # 挂在管理器上（ctx.browser.last_shot_b64）：analyze_image 的浏览器
            # 回退路径靠它把这张截图交给视觉模型——图片本体不进对话文本。
            self.last_shot_b64 = base64.b64encode(png).decode()
            return _ok({"result": "已截图并展示给用户（右侧浏览器面板）。"
                                  "请立刻调用 analyze_image 查看页面内容，再决定下一步。",
                        "url": page.url,
                        "size": len(png)})
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"截图失败：{e}")


def _chrome_profile_src() -> Path | None:
    """探测系统 Chrome 的默认 profile 目录（跨平台）。找不到返回 None。"""
    home = Path.home()
    candidates = (
        home / "Library/Application Support/Google/Chrome/Default",   # macOS
        home / ".config/google-chrome/Default",                        # Linux
        home / "AppData/Local/Google/Chrome/User Data/Default",        # Windows
    )
    for c in candidates:
        if c.is_dir():
            return c
    return None


# ---------------------------------------------------------------------------
# 工具包装：给 TOOL_REGISTRY 的入口（ctx.browser 由 agent.py 注入 BrowserManager）
# ---------------------------------------------------------------------------

def _ctx_browser(ctx):
    mgr = getattr(ctx, "browser", None) if ctx is not None else None
    if mgr is None:
        return None, _err("浏览器会话不可用（系统内部问题）", "重试一次；持续失败请联系部署者")
    return mgr, None


def browser_open(url: str, ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.open(url)


def browser_click(selector: str, ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.click(selector)


def browser_type(selector: str, text: str, ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.type_text(selector, text)


def browser_screenshot(ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.screenshot()


BROWSER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "打开一个网页（内置 Chromium，自动复用本地浏览器登录态，已登录的网站无需再登录）。"
                           "什么时候用：验证前端/接口改动、自动登录或操作网页、上网调研（需要看真实渲染效果的站点）。"
                           "返回页面标题、URL、标题层级、按钮与输入框摘要，帮你决定下一步点哪/填哪。"
                           "示例：{\"url\": \"http://localhost:8000\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL，须带 http:// 或 https://"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "点击当前页面上的元素（按钮/链接）。selector 优先直接写元素可见文字（如 '登录'，"
                           "自动按文本匹配）；也支持 css=/xpath=/text= 前缀语法。点击后返回新的页面摘要。"
                           "示例：{\"selector\": \"登录\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "元素的可见文字，或 css=/xpath=/text= 前缀的 selector"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "向输入框填入文字（登录表单、搜索框等）。selector 用输入框的 placeholder/name/可见文字。"
                           "密码等敏感值不要写明文——用环境变量引用：text 传 \"env:LOGIN_PASSWORD\"，"
                           "部署环境设置该变量后自动取值，密码不进入对话记录。"
                           "示例：{\"selector\": \"用户名\", \"text\": \"alice\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "输入框的 placeholder/name/可见文字，或引擎前缀 selector"},
                    "text": {"type": "string", "description": "要输入的内容；敏感值用 env:变量名 引用环境变量"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "对当前页面截图并在右侧「浏览器」面板展示给用户。你自己看不到像素——"
                           "截完图【必须紧接着调用 analyze_image】读取页面内容再决定下一步。"
                           "什么时候用：browser_open/click/type 返回的摘要不足以判断页面状态时、"
                           "验证渲染效果时、需要读验证码/弹窗内容时。无参数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

BROWSER_TOOL_REGISTRY = {
    "browser_open": browser_open,
    "browser_click": browser_click,
    "browser_type": browser_type,
    "browser_screenshot": browser_screenshot,
}

# 只读性：open 与 screenshot 不改网页状态（但出网/拉起浏览器，不并行）；
# click/type 会改变页面状态，非只读。
BROWSER_TOOL_READ_ONLY = {
    "browser_open": False,
    "browser_click": False,
    "browser_type": False,
    "browser_screenshot": False,
}
