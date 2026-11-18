"""analyze_image 读工作区图片 + 浏览器运行时自检分型。

覆盖两条曾经断掉的能力：
  1. agent 自己生成的图片落在工作区，却没有任何工具能读 —— analyze_image 加 path；
  2. 「未安装 playwright」一句话掩盖了 模块缺失/内核缺失/版本不匹配/权限 四类故障。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402
import browser_tools  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


class _Ctx:
    def __init__(self, ws, images=None, vision=None):
        self.workspace = ws
        self.images = images or []
        self.vision_backend = vision or (lambda parts, q: parts[0]["image_url"]["url"])
        self.browser = None


class AnalyzeImagePathTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.ctx = _Ctx(self.ws)

    def _put(self, name, data):
        p = self.ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return name

    def test_reads_png_from_workspace(self):
        name = self._put("shot.png", PNG)
        payload = json.loads(tools.analyze_image(path=name, ctx=self.ctx))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["path"], name)
        self.assertIn("data:image/png;base64,", payload["result"])

    def test_mime_from_magic_not_extension(self):
        """扩展名不可信：jpg 内容存成 .png 也按 jpeg 送模型。"""
        name = self._put("lie.png", JPEG)
        payload = json.loads(tools.analyze_image(path=name, ctx=self.ctx))
        self.assertTrue(payload["ok"])
        self.assertIn("data:image/jpeg;base64,", payload["result"])

    def test_webp_and_nested_dir(self):
        name = self._put("out/charts/a.webp", WEBP)
        payload = json.loads(tools.analyze_image(path=name, ctx=self.ctx))
        self.assertTrue(payload["ok"])
        self.assertIn("data:image/webp;base64,", payload["result"])

    def test_path_not_found_has_code_and_hint(self):
        payload = json.loads(tools.analyze_image(path="nope.png", ctx=self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "PATH_NOT_FOUND")
        self.assertIn("hint", payload)

    def test_non_image_rejected(self):
        name = self._put("fake.png", b"hello, i am text")
        payload = json.loads(tools.analyze_image(path=name, ctx=self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "NOT_AN_IMAGE")

    def test_oversize_rejected(self):
        name = self._put("big.png", PNG)
        with mock.patch.object(tools, "IMAGE_PATH_MAX_BYTES", 10):
            payload = json.loads(tools.analyze_image(path=name, ctx=self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "IMAGE_TOO_LARGE")

    def test_escape_workspace_denied(self):
        payload = json.loads(tools.analyze_image(path="../../etc/passwd", ctx=self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "PATH_DENIED")

    def test_path_wins_over_message_images(self):
        """显式 path 时不回退消息里的图——避免"想看的文件不存在"却悄悄分析了另一张。"""
        name = self._put("real.png", PNG)
        self.ctx.images = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]
        payload = json.loads(tools.analyze_image(path=name, ctx=self.ctx))
        self.assertTrue(payload["ok"])
        self.assertIn("path", payload)
        self.assertNotIn("image_id", payload)


class BrowserRuntimeDiagnosisTest(unittest.TestCase):
    def test_kernel_missing_classified(self):
        e = browser_tools._classify_launch_error(
            Exception("Executable doesn't exist at /Users/x/Library/Caches/ms-playwright/chromium-1200/chrome"))
        self.assertEqual(e.code, "BROWSER_KERNEL_MISSING")
        self.assertIn("BROWSER_CHROME_PATH", e.hint)

    def test_permission_classified(self):
        e = browser_tools._classify_launch_error(PermissionError("Operation not permitted"))
        self.assertEqual(e.code, "BROWSER_PERMISSION")

    def test_unknown_falls_back(self):
        e = browser_tools._classify_launch_error(Exception("weird boom"))
        self.assertEqual(e.code, "BROWSER_LAUNCH_FAILED")

    def test_chrome_path_config_used_and_validated(self):
        real = Path(sys.executable)
        with mock.patch.dict(os.environ, {"BROWSER_CHROME_PATH": str(real)}, clear=False):
            self.assertEqual(browser_tools._browser_launch_options(), {"executable_path": str(real)})
        with mock.patch.dict(os.environ, {"BROWSER_CHROME_PATH": "/definitely/not/here"}, clear=False):
            with self.assertRaises(browser_tools.BrowserUnavailable) as cm:
                browser_tools._browser_launch_options()
            self.assertEqual(cm.exception.code, "BROWSER_CHROME_PATH_INVALID")

    def test_channel_config_removed(self):
        """BROWSER_CHANNEL 已移除：不得再驱动用户本机 Chrome（会打断用户工作）。"""
        env = {k: v for k, v in os.environ.items() if k != "BROWSER_CHROME_PATH"}
        env["BROWSER_CHANNEL"] = "chrome"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(browser_tools._browser_launch_options(), {})

    def test_default_headless(self):
        """默认必须无头：agent 开浏览器不得弹出可见窗口抢占焦点。"""
        self.assertTrue(browser_tools.BrowserManager("t-headless").headless)

    def test_unavailable_envelope_marks_service_level(self):
        """服务级不可用：带 unavailable=true，模型据此区分"修工具"与"换选择器"。"""
        out = json.loads(browser_tools._unavailable_err(
            browser_tools.BrowserUnavailable("PLAYWRIGHT_MISSING", "没装", "去装")))
        self.assertFalse(out["ok"])
        self.assertTrue(out["unavailable"])
        self.assertEqual(out["error_code"], "PLAYWRIGHT_MISSING")
        self.assertIn("去装", out["hint"])

    def test_screenshot_to_path_writes_into_workspace(self):
        ws = Path(tempfile.mkdtemp())
        ctx = _Ctx(ws)
        mgr = browser_tools.BrowserManager("t-sid")
        saved, fail = mgr._shot_to_path(PNG, "shots/login.png", ctx)
        self.assertIsNone(fail)
        self.assertEqual(saved, "shots/login.png")
        self.assertEqual((ws / "shots/login.png").read_bytes(), PNG)

    def test_screenshot_to_path_outside_denied(self):
        ws = Path(tempfile.mkdtemp())
        saved, fail = browser_tools.BrowserManager("t-sid")._shot_to_path(PNG, "../evil.png", _Ctx(ws))
        self.assertIsNone(saved)
        payload = json.loads(fail)
        self.assertEqual(payload["error_code"], "PATH_DENIED")


class LoginPageErgonomicsTest(unittest.TestCase):
    """登录页人机工程：摘要不准/定位不对是「怎么都登不进去」的两大根因。

    需要真实浏览器内核（playwright install chromium）。没装时跳过而非失败——
    否则克隆项目的人跑测试会因缺 190MB 内核而红一片。

    默认**整类跳过**：跑一次要真启动 Chromium，会吃内存/CPU，放在日常
    `unittest discover` 里没有必要。需要时显式开：
        RUN_BROWSER_TESTS=1 python -m unittest tests.test_image_path_and_browser_runtime
    （用例本身是无头的，不会弹窗，但仍建议按需运行。）
    """

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_BROWSER_TESTS") != "1":
            raise unittest.SkipTest("浏览器用例默认跳过（设 RUN_BROWSER_TESTS=1 启用）")
        try:
            import playwright  # noqa: F401
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                if not Path(p.chromium.executable_path).exists():
                    raise RuntimeError("chromium not installed")
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"浏览器内核不可用：{e}")

    def _run_in_browser(self, body):
        """在**一个**独立线程里完成「启动→执行→关闭」。

        playwright sync API 绑定创建它的线程，且不能在 asyncio loop 里启动
        （discover 下别的测试会留下 loop）。所以不能 setUp 起线程、方法里用——
        线程一退出连接就断（实测 TargetClosedError）。body(page, mgr) 在该线程内跑。
        """
        box = {}

        def runner():
            mgr = None
            try:
                # 用独立临时 profile：固定 sid 的 profile 可能被上次残留实例占着，
                # 导致 launch_persistent_context 直接 TargetClosed。
                tmp = tempfile.mkdtemp()
                mgr = browser_tools.BrowserManager("t-login-ergonomics")
                mgr._profile_dir = lambda: Path(tmp)
                page = mgr._ensure()
                page.set_content(f"<html><body>{body[0]}<script>{body[1]}</script></body></html>")
                box["ok"] = body[2](page, mgr)
            except unittest.SkipTest:
                raise
            except Exception as e:  # noqa: BLE001
                box["err"] = e
            finally:
                if mgr is not None:
                    try:
                        mgr.close()
                    except Exception:
                        pass

        t = threading.Thread(target=runner)
        t.start()
        t.join(120)
        if "err" in box:
            raise box["err"]
        if "ok" not in box:
            raise AssertionError("浏览器用例未产生结果（可能线程超时）")
        return box["ok"]

    def test_login_button_survives_nav_links(self):
        """20 个导航链接在前的登录页：登录按钮必须仍出现在摘要里。

        旧实现把 button/a/[role=button] 混排取前 15 个，导航链接把「登录」
        挤出去，模型看不到它只能猜 selector，然后 ELEMENT_NOT_FOUND。
        """
        nav = "".join(f'<a href="#{i}">导航{i}</a>' for i in range(20))
        html = (f'<nav>{nav}</nav><form><input name="u" type="text">'
                f'<button type="submit">登录</button></form>')

        def body(page, mgr):
            desc = mgr._describe_page()
            return desc

        desc = self._run_in_browser((html, "", body))
        self.assertIn("登录", desc["buttons"]["buttons"])
        self.assertNotIn("导航0", desc["buttons"]["buttons"])      # 导航不该混进按钮列表
        self.assertEqual(len(desc["forms"]), 1)
        self.assertEqual(desc["forms"][0]["submit"], ["登录"])

    def test_password_value_never_echoed(self):
        """inputs.filled 只报是否有值，绝不把密码内容吐给模型。"""
        def body(page, mgr):
            page.fill('input[name="p"]', "s3cret")
            desc = mgr._describe_page()
            return desc["inputs"][0]["filled"], json.dumps(desc, ensure_ascii=False)

        filled, dumped = self._run_in_browser(('<input name="p" type="password">', "", body))
        self.assertTrue(filled)
        self.assertNotIn("s3cret", dumped)

    def test_bare_css_selector_is_resolved(self):
        """裸 CSS（input[name=x]）必须能定位到元素，而不是被当成可见文字去匹配。"""
        def body(page, mgr):
            return mgr._locator(page, "input[name=username]").count()

        self.assertEqual(self._run_in_browser(('<input name="username" type="text">', "", body)), 1)

    def test_controlled_component_accepts_typed_input(self):
        """只监听 keyup 的受控组件：逐字输入能更新状态，fill() 不能。"""
        script = ("document.addEventListener('keyup',e=>{if(e.target.name)"
                  "document.getElementById('m').textContent=e.target.value;});")
        html = '<input name="u" type="text"><div id="m"></div>'

        def body(page, mgr):
            # 直接调管理器方法：它已在本线程内（与 tool_call 的分发线程相同，
            # playwright 的线程绑定要求满足）；若走 tool_call 会切到 actor 线程
            # 排队，而那个线程随本用例结束就关了。
            loc = mgr._locator(page, "input[name=u]")
            loc.click()
            loc.fill("")
            loc.type("alice", delay=30)
            after_type = page.inner_text("#m")
            page.fill("input[name=u]", "")
            page.fill("input[name=u]", "bob")
            after_fill = page.inner_text("#m")
            return after_type, after_fill

        after_type, after_fill = self._run_in_browser((html, script, body))
        self.assertEqual(after_type, "alice")
        self.assertNotEqual(after_fill, "bob")   # fill 不触发 keyup：状态没跟上


if __name__ == "__main__":
    unittest.main()
