"""analyze_image 读工作区图片 + 浏览器运行时自检分型。

覆盖两条曾经断掉的能力：
  1. agent 自己生成的图片落在工作区，却没有任何工具能读 —— analyze_image 加 path；
  2. 「未安装 playwright」一句话掩盖了 模块缺失/内核缺失/版本不匹配/权限 四类故障。
"""

import json
import os
import sys
import tempfile
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

    def test_channel_config(self):
        env = {k: v for k, v in os.environ.items() if k != "BROWSER_CHROME_PATH"}
        env["BROWSER_CHANNEL"] = "chrome"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(browser_tools._browser_launch_options(), {"channel": "chrome"})

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


if __name__ == "__main__":
    unittest.main()
