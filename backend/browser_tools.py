"""Shared, interactive Chromium window with per-session persistent storage.

A dedicated actor thread owns every Playwright object. The UI can take control,
then return it to the agent. Screenshots are only collected for explicit visual
analysis, never used as the interactive surface. New profiles are isolated;
system Chrome data is not copied. Windows stay open across turns and can be
closed explicitly; unattended actor threads reclaim their browser after idle.
"""

import json
import queue
from concurrent.futures import Future
from functools import wraps
import shutil
import threading
import time
from pathlib import Path

# playwright 是可选依赖
# 注意：本模块绝不 import tools（tools.py 反向导入本模块，循环导入会炸）。
# 失败信封用本文件自带的 _err/_ok，与 doc_tools 同一自足模式。：导入失败不炸模块，工具调用时给出可读指引
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

VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20000
ACTION_TIMEOUT_MS = 8000


def manager_for(sid: str) -> "BrowserManager":
    """取（或创建）一个会话的浏览器管理器。显式关闭或空闲回收 Chromium；管理器可跨回合复用。"""
    with _MANAGERS_LOCK:
        if sid not in _MANAGERS:
            _MANAGERS[sid] = BrowserManager(sid)
        return _MANAGERS[sid]


PROFILES_BASE = Path("data/browser-profiles")


def cleanup_stale_profiles(keep_days: int) -> int:
    """启动时清理陈旧的浏览器 profile 副本：目录 mtime 超过 keep_days 天的整棵删除。

    背景：每个用过浏览器工具的会话都会复制一份系统 Chrome profile（~90MB），
    回合结束只关进程、目录永不清理，长期使用会无限线性增长。登录态价值
    （cookies/localStorage）只对近期活跃会话有意义，陈旧副本直接回收。
    keep_days<=0 表示关闭清理。返回删除的目录数。
    """
    if keep_days <= 0 or not PROFILES_BASE.is_dir():
        return 0
    deadline = time.time() - keep_days * 86400
    removed = 0
    for d in PROFILES_BASE.iterdir():
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime >= deadline:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except OSError:
            continue  # 单个目录失败不阻断其余清理
    return removed


def close_session(sid: str) -> None:
    """Close Chromium on its actor thread while keeping the manager reusable."""
    with _MANAGERS_LOCK:
        mgr = _MANAGERS.get(sid)
    if mgr:
        mgr.close()


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str, hint: str = "") -> str:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


def _serialized(method):
    @wraps(method)
    def wrapper(self,*args,**kwargs):
        def invoke():
            if self._manual:
                return _err("用户正在手动操作浏览器，请等待交回 Agent")
            self._last_action=time.monotonic()
            return method(self,*args,**kwargs)
        return invoke() if threading.current_thread() is self._worker else self._call(invoke)
    return wrapper


class BrowserManager:
    """一个会话的 Chromium 生命周期。惰性启动（第一次工具调用才拉起浏览器）。"""

    def __init__(self, sid: str, *, headless=False):
        self.sid = sid
        self.headless = headless
        self._last_action = time.monotonic()
        self._epoch = 0
        self._jobs = queue.Queue()
        self._dispatch_lock = threading.Lock()
        self._worker = None
        self._manual = False
        self._released = threading.Event()
        self._released.set()
        self._pw = None          # sync_playwright() 返回的 Playwright 对象
        self._ctx = None         # 持久化上下文（带 profile）
        self._page = None
        self.last_shot_b64 = None  # 最新截图的 base64（analyze_image 的浏览器回退路径用）
        self._lock = threading.RLock()  # Playwright sync API 不允许跨线程并发使用

    def _call(self, fn, *args):
        with self._dispatch_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work,daemon=True,name="browser-"+self.sid[:12])
                self._worker.start()
            future = Future()
            self._jobs.put((future,fn,args))
        return future.result()

    def _work(self):
        # All Playwright objects are created, used and destroyed on this thread.
        while True:
            try:
                future,fn,args = self._jobs.get(timeout=60)
            except queue.Empty:
                with self._dispatch_lock:
                    if self._jobs.empty() and not self._manual and time.monotonic()-self._last_action>1800:
                        self._close()
                        self._worker = None
                        return
                continue
            try:
                if not self._manual and time.monotonic()-self._last_action>1800:self._close()
                future.set_result(fn(*args))
            except Exception as error: future.set_exception(error)

    def state(self):
        with self._dispatch_lock:
            if self._worker is None:
                return {"open":False,"url":"","manual":False,"mode":"window","title":""}
        return self._call(self._state)

    def _state(self):
        page=self._page
        live=page is not None and not page.is_closed()
        if not live:
            self._manual=False;self._released.set()
        return {"open":live,"url":page.url if live else "", "manual":self._manual,
                "mode":"window","title":page.title() if live else ""}

    def control(self,action,url=None):
        return self._call(self._control,action,url)

    def _control(self,action,url):
        self._last_action=time.monotonic()
        if action=="release":
            self._manual=False;self._released.set()
        elif action=="close":
            self._close()
        elif action in ("takeover","open","back","forward","reload"):
            if action=="open" and (not isinstance(url,str) or not url.startswith(("http://","https://")) or len(url)>4096):
                raise ValueError("请输入完整的 http:// 或 https:// 地址")
            page=self._ensure()
            self._manual=True;self._released.clear()
            if action=="open":
                page.goto(url,timeout=NAV_TIMEOUT_MS,wait_until="domcontentloaded")
            elif action=="back":page.go_back(timeout=NAV_TIMEOUT_MS,wait_until="domcontentloaded")
            elif action=="forward":page.go_forward(timeout=NAV_TIMEOUT_MS,wait_until="domcontentloaded")
            elif action=="reload":page.reload(timeout=NAV_TIMEOUT_MS,wait_until="domcontentloaded")
            page.bring_to_front()
        else:raise ValueError("未知浏览器操作")
        self._publish_state("用户操作")
        return self._state()

    def tool_call(self,name,ctx,*args):
        epoch=self._epoch
        cancel=getattr(ctx,"cancel_event",None)
        while not self._released.wait(.2):
            if cancel is not None and cancel.is_set():return _err("任务已停止，未操作浏览器")
        if cancel is not None and cancel.is_set():return _err("任务已停止，未操作浏览器")
        def invoke():
            if epoch!=self._epoch:return _err("浏览器已关闭，未继续等待中的操作")
            return getattr(self,name)(*args)
        return self._call(invoke)

    # ---------- 生命周期 ----------

    def _profile_dir(self) -> Path:
        """本会话独立 profile，登录一次后在本会话中复用。"""
        base = Path("data/browser-profiles") / self.sid
        if base.exists():
            return base
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _ensure(self):
        """惰性启动：返回 (page, ok_msg)。失败时抛 RuntimeError（带可读原因）。"""
        if self._page is not None:
            if not self._page.is_closed():
                pages=[p for p in self._ctx.pages if not p.is_closed()]
                if pages:self._page=pages[-1]
                return self._page
            self._close()
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
                    headless=self.headless,
                    viewport=VIEWPORT,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
                self._page.set_default_timeout(ACTION_TIMEOUT_MS)
            except Exception as e:
                self._close()
                raise RuntimeError(f"浏览器启动失败：{e}") from e
        return self._page

    def close(self):
        with self._dispatch_lock:
            worker=self._worker
        if worker is not None:self._call(self._close)

    def _close(self):
        self._epoch+=1
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
            self._manual = False
            self._released.set()

    # ---------- 内部动作 ----------

    def _publish_state(self, note: str = ""):
        """Publish lightweight state; never stream image snapshots to the panel."""
        try:
            from services import browser as service
            if service._event_bus is not None:
                service._event_bus(self.sid).publish({"type":"browser_state",**self._state(),"note":note})
        except Exception:
            pass

    def _locator(self,page,selector):
        if selector.startswith(("css=","xpath=","text=","id=")):
            return page.locator(selector)
        label=page.get_by_label(selector,exact=True)
        if label.count():return label
        placeholder=page.get_by_placeholder(selector,exact=True)
        if placeholder.count():return placeholder
        return page.get_by_text(selector,exact=True)

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

    @_serialized
    def open(self, url: str) -> str:
        if not isinstance(url,str) or not url.startswith(("http://","https://")) or len(url)>4096:
            return _err("请输入完整的 http:// 或 https:// 地址")
        try:
            page = self._ensure()
            page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            info = self._describe_page()
            self._publish_state("打开页面")
            return _ok({"result": f"已打开 {url}", **info})
        except PWTimeout:
            return _err(f"打开 {url} 超时（{NAV_TIMEOUT_MS // 1000}s）",
                        "检查 URL 是否可达；内网页面确认是否需要先连 VPN")
        except RuntimeError as e:
            return _err(str(e), "在部署机上安装 playwright 后重试")
        except Exception as e:
            return _err(f"打开页面失败：{e}", "检查 URL 格式（须带 http:// 或 https://）")

    @_serialized
    def click(self, selector: str) -> str:
        try:
            page = self._ensure()
            self._locator(page,selector).click(timeout=ACTION_TIMEOUT_MS)
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            info = self._describe_page()
            self._publish_state(f"点击 {selector}")
            return _ok({"result": f"已点击「{selector}」", **info})
        except PWTimeout:
            return _err(f"找不到可点击的元素「{selector}」或页面加载超时",
                        "先 browser_snapshot 读取当前页面，按页面上的真实文字重写 selector")
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"点击失败：{e}", "selector 用页面元素的可见文字（自动按 text= 匹配）")

    @_serialized
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
            self._locator(page,selector).fill(text, timeout=ACTION_TIMEOUT_MS)
            self._publish_state(f"输入 {selector}")
            return _ok({"result": f"已在「{selector}」输入 {len(text)} 个字符",
                        "url": page.url})
        except PWTimeout:
            return _err(f"找不到输入框「{selector}」",
                        "先 browser_snapshot 读取当前页面；输入框定位用它的 placeholder 或 name")
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"输入失败：{e}")

    @_serialized
    def snapshot(self):
        try:
            self._ensure()
            return _ok(self._describe_page())
        except Exception as error:return _err(f"读取页面失败：{error}")

    @_serialized
    def press(self,key):
        try:
            if key not in ("Enter","Tab","Escape","ArrowDown","ArrowUp","PageDown","PageUp","Space"):
                return _err("不支持此按键")
            self._ensure().keyboard.press(key)
            self._publish_state("按键")
            return _ok(self._describe_page())
        except Exception as error:return _err(f"按键失败：{error}")

    @_serialized
    def screenshot(self) -> str:
        try:
            page = self._ensure()
            png = page.screenshot(type="png")
            self._publish_state("截图")
            import base64
            # 挂在管理器上（ctx.browser.last_shot_b64）：analyze_image 的浏览器
            # 回退路径靠它把这张截图交给视觉模型——图片本体不进对话文本。
            self.last_shot_b64 = base64.b64encode(png).decode()
            return _ok({"result": "已采集页面供视觉分析；用户可在独立浏览器窗口直接操作。"
                                  "请立刻调用 analyze_image 查看页面内容，再决定下一步。",
                        "url": page.url,
                        "size": len(png)})
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"截图失败：{e}")


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
    return err if err else mgr.tool_call("open",ctx,url)


def browser_click(selector: str, ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.tool_call("click",ctx,selector)


def browser_type(selector: str, text: str, ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.tool_call("type_text",ctx,selector,text)


def browser_screenshot(ctx=None) -> str:
    mgr, err = _ctx_browser(ctx)
    return err if err else mgr.tool_call("screenshot",ctx)


BROWSER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "打开真实可操作的 Chromium 窗口（会话独立登录态；用户可手动接管）。"
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
            "description": "仅为视觉分析采集当前页面，不向右侧面板追加截图。你自己看不到像素——"
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


def browser_snapshot(ctx=None):
    mgr,err=_ctx_browser(ctx)
    return err if err else mgr.tool_call("snapshot",ctx)


def browser_press(key,ctx=None):
    mgr,err=_ctx_browser(ctx)
    return err if err else mgr.tool_call("press",ctx,key)


BROWSER_TOOL_REGISTRY.update(browser_snapshot=browser_snapshot,browser_press=browser_press)
BROWSER_TOOL_READ_ONLY.update(browser_snapshot=False,browser_press=False)
BROWSER_TOOL_SCHEMAS.extend([
    {"type":"function","function":{"name":"browser_snapshot","description":"读取当前浏览器页面标题、按钮和输入框摘要，无需截图。优先用此工具观察页面变化。","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"browser_press","description":"在当前页面按键，如 Enter 提交、Tab 切换、PageDown 滚动。","parameters":{"type":"object","properties":{"key":{"type":"string","enum":["Enter","Tab","Escape","ArrowDown","ArrowUp","PageDown","PageUp","Space"]}},"required":["key"]}}}
])
