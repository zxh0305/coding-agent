#!/usr/bin/env python3
"""
贴底行为冒烟：流式期间页面必须跟着走，且用户上滚后绝不能被拽回
================================================================================

用法：python3 backend/manual/smoke_stick_bottom.py

为什么要这个脚本：贴底是本项目 bug 最密集的区域（git log 里"贴顶/不贴底/
被拽回"反复修复），但它的逻辑全在浏览器里——纯单测覆盖不到，只能靠手点页面
发现，于是每次改动都可能悄悄回归。这里用 Playwright + 一个 mock 后端把真实
前端跑起来，用【像素级】断言替代肉眼：

  ✅ 流式期间页面贴底（离底 < 4px）
  ✅ 用户上滚后不被拽回（这比贴底更重要：打扰看历史是更严重的体验问题）
  ✅ 长回复（内容真正溢出）也贴底

覆盖过的真实 bug（都会被本脚本抓到）：
  * note-box 触到 max-height:260px 后不再长高，dy 恒为 0，页面永远停在离底
    32px 处，直到 done 才跳一下——"流式期间不自动贴底"；
  * 统计行 appendChild 进正文区那一帧没人对齐，内容卡在半空约 40ms。

需要 playwright（backend 的 browser_tools 已依赖它）：
    pip install playwright && playwright install chromium
装不上时本脚本以退出码 0 跳过，不阻塞其它验证。
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
SID = "smoke-sess"
DELTA_COUNT = 80          # 足够把页面撑到可滚
WITH_THINKING = True      # 混合 reasoning_delta：覆盖思考窗路径


class MockHandler(BaseHTTPRequestHandler):
    """只喂事件、不碰数据库的最小后端。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        path = self.path.split("?")[0]
        if path.startswith("/api/sessions") and path.endswith("/messages"):
            return self._json({"session_id": SID, "nonce": "n1"})
        if path == "/api/sessions":
            return self._json({"session_id": SID, "nonce": "n1"})
        return self._json({"ok": True})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/auth/me":
            return self._json({"username": "smoke"})
        if path == "/api/sessions":
            return self._json([{"id": SID, "title": "冒烟", "state": "none",
                                "preview": "", "archived": 0}])
        if path == "/api/config":
            return self._json({"provider": "p", "model": "m", "context_window": 128000})
        if path in ("/api/models", "/api/providers"):
            return self._json([])
        if path == "/api/workspace":
            return self._json({"path": "/tmp"})
        if path == "/api/context":
            return self._json({})
        if path.endswith("/messages"):
            return self._json({"messages": [], "has_more": False})
        if path.endswith(("/docs", "/attachments", "/todos")):
            return self._json([])
        if path.endswith("/events"):
            return self._stream()
        fp = FRONTEND / (path.lstrip("/") or "index.html")
        if path == "/":
            fp = FRONTEND / "index.html"
        if fp.is_file():
            b = fp.read_bytes()
            ct = ("text/html" if fp.suffix == ".html"
                  else "text/css" if fp.suffix == ".css" else "application/javascript")
            self.send_response(200)
            self.send_header("Content-Type", ct + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def w(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            w({"type": "caught_up", "running": False})
            time.sleep(0.3)
            w({"type": "turn_start", "nonce": "n1", "input": "你好", "atts": [],
               "started_at": time.time(), "user_mid": "um1"})
            w({"type": "round", "round": 1, "mid": "m1"})
            for i in range(DELTA_COUNT):
                if WITH_THINKING and i % 2 == 0:
                    w({"type": "reasoning_delta", "delta": f"思考第 {i} 段文本内容。"})
                else:
                    w({"type": "answer_delta", "mid": "m1",
                       "delta": f"回答第 {i} 段文本内容，用来把页面撑长。"})
                time.sleep(0.02)
            w({"type": "done", "answer": "完成", "mid": "m1", "elapsed_s": 2.0, "usage": {}})
            w({"type": "turn_end", "user_mid": "um1"})
            time.sleep(30)
        except Exception:
            pass


def measure(pg, duration_ms=3500):
    """连续逐帧采「离底距离」，覆盖整个流式期间。

    按【时间】而不是【帧数】采样：mock 在 turn_start 前有 0.3s 延迟，固定
    80 帧只覆盖开头一小段，真正的漂移（统计行插入那一帧）会被漏掉——冒烟
    脚本必须覆盖到事件流结束，否则禁用修复也测不出来（踩过）。
    双 rAF 保证读到的是用户实际看到的稳定画面。
    """
    return pg.evaluate("""(durationMs) => new Promise(resolve => {
      const c = document.getElementById('chat');
      const out = []; const t0 = performance.now();
      function tick(){
        requestAnimationFrame(() => requestAnimationFrame(() => {
          out.push(c.scrollHeight - c.scrollTop - c.clientHeight);
          if (performance.now() - t0 < durationMs) tick(); else resolve(out);
        }));
      }
      tick();
    })""", duration_ms)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("跳过：未安装 playwright（pip install playwright && playwright install chromium）")
        return 0

    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    failures = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            # ── 用例 1：流式期间贴底 ────────────────────────────────
            pg = browser.new_page(viewport={"width": 900, "height": 520})
            pg.add_init_script("localStorage.setItem('auth_token','t');"
                               "localStorage.setItem('auth_username','smoke');")
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(url, wait_until="networkidle")
            pg.wait_for_timeout(600)
            pg.query_selector("#input").fill("你好")
            pg.query_selector("#input").press("Enter")
            samples = measure(pg)
            worst = max(samples)
            print(f"用例1 流式贴底：最大离底 {worst:.1f}px（{len(samples)} 帧）")
            if worst >= 4:
                failures.append(f"流式期间离底 {worst:.1f}px（应 <4px）")
            if errs:
                failures.append(f"页面报错：{errs[:3]}")
            pg.close()

            # ── 用例 2：用户上滚后不被拽回（比贴底更重要）────────────
            pg = browser.new_page(viewport={"width": 900, "height": 520})
            pg.add_init_script("localStorage.setItem('auth_token','t');"
                               "localStorage.setItem('auth_username','smoke');")
            pg.goto(url, wait_until="networkidle")
            pg.wait_for_timeout(600)
            pg.query_selector("#input").fill("你好")
            pg.query_selector("#input").press("Enter")
            pg.wait_for_timeout(1500)          # 等页面被撑到可滚
            pg.mouse.move(450, 300)
            pg.mouse.wheel(0, -400)            # 上滚看历史
            pg.wait_for_timeout(150)
            top_after = pg.evaluate("document.getElementById('chat').scrollTop")
            moved = False
            for _ in range(15):
                pg.wait_for_timeout(100)
                if pg.evaluate("document.getElementById('chat').scrollTop") != top_after:
                    moved = True
            print(f"用例2 上滚保护：scrollTop 是否被程序改动 = {moved}")
            if moved:
                failures.append("用户上滚后被自动贴底拽回（打扰看历史）")
            pg.close()

            browser.close()
    finally:
        srv.shutdown()

    print()
    if failures:
        print("❌ 冒烟失败：")
        for f in failures:
            print("   -", f)
        return 1
    print("✅ 贴底冒烟全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
