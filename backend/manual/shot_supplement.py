"""截图验证：补充条折叠单行 / 展开全文 / 状态胶囊配色 / 与用户气泡等宽对齐。"""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
from routes import busref
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

gate = threading.Event()
entered = threading.Event()


class Model:
    def chat(self, messages, **kwargs):
        return {"role": "assistant", "content": "ok"}

    def chat_stream(self, messages, tools=None, cancel=None):
        users = [m for m in messages if m.get("role") == "user"]
        text = users[-1].get("content", "")
        if isinstance(text, list):
            text = "\n".join(p.get("text", "") for p in text if p.get("type") == "text")
        if text == "hold":
            entered.set()
            deadline = time.monotonic() + 40
            while not gate.is_set() and time.monotonic() < deadline:
                if cancel.wait(0.04):
                    break
        yield "delta", "好的，我先看下你补充的内容。"
        yield "message", {"role": "assistant", "content": "好的，我先看下你补充的内容。"}


case = ApiTestBase()
case.setUp()
case.set_global("p1", "a-small")
db.create_user("shot-user", "shot-pass")
uid = db.get_user_by_name("shot-user")["id"]
ws = Path(case.tmp) / "workspace"
ws.mkdir()
(ws / "README.txt").write_text("test")
db.set_setting(f"default_workspace:{uid}", str(ws))
patches = [
    patch("app.create_client", side_effect=lambda *a, **k: Model()),
    patch("services.turn._spawn_memory_extraction"),
    patch("services.turn._spawn_title_generation"),
]
for item in patches:
    item.start()
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1365, "height": 960})
        page.goto(case.base)
        page.locator("#login-user").fill("shot-user")
        page.locator("#login-pass").fill("shot-pass")
        page.locator("#login-submit").click()
        expect(page.locator("#layout")).to_be_visible()
        page.locator("#new-task").click()
        page.locator("#input").fill("hold")
        page.locator("#input").press("Enter")
        assert entered.wait(6)
        page.wait_for_function("!!activeTurnMid")
        # 一条短补充 + 一条超长补充（验证折叠不换行）
        page.locator("#input").fill("刚才插入信息你参考了吗")
        page.locator("#input").press("Enter")
        expect(page.locator(".turn-input-list details")).to_have_count(1)
        page.locator("#input").fill(
            "在这个项目下面安装playwright，然后有什么需要的依赖一起撞上，"
            "而且要保证其他用户可以用，另外再把截图功能也补上，最后跑一遍全部回归测试确认没有退化"
        )
        page.locator("#input").press("Enter")
        expect(page.locator(".turn-input-list details")).to_have_count(2)
        page.wait_for_timeout(500)

        # 折叠态尺寸断言：每条 summary 单行、高度 < 40
        heights = page.locator(".turn-input-list summary").evaluate_all(
            "els=>els.map(el=>el.getBoundingClientRect().height)")
        widths_bubble = page.locator("#chat .bubble.user").first.evaluate(
            "el=>el.getBoundingClientRect().width")
        widths_list = page.locator(".turn-input-list").evaluate(
            "el=>el.getBoundingClientRect().width")
        print("SUMMARY_HEIGHTS", heights)
        print("BUBBLE_W", round(widths_bubble), "LIST_W", round(widths_list))
        # 右对齐与等宽核对：用户气泡外框、补充条外框的左右边界
        geo = page.evaluate("""()=>{
          const u=document.querySelector('#chat .bubble.user')?.closest('.msg-group')||document.querySelector('#chat .bubble.user');
          const l=document.querySelector('.turn-input-list');
          const r=a=>{const b=a.getBoundingClientRect();return [Math.round(b.left),Math.round(b.right),Math.round(b.width)];};
          const d=document.querySelectorAll('.turn-input-list details');
          return {user:r(u),list:r(l),item:r(d[0]),ava:getComputedStyle(l).textAlign,
                  itemBg:getComputedStyle(d[0]).backgroundColor,
                  badge:getComputedStyle(d[0].querySelector('.supp-state')).color};
        }""")
        print("GEO", geo)
        assert all(h < 40 for h in heights), heights
        page.screenshot(path="/tmp/supp_collapsed.png", clip={
            "x": 400, "y": 60, "width": 900, "height": 420})

        # 展开长的那条
        page.locator(".turn-input-list details").nth(1).locator("summary").click()
        page.wait_for_timeout(400)
        full = page.locator(".turn-input-list details").nth(1).inner_text()
        print("EXPANDED_HAS_TAIL", "确认没有退化" in full)
        assert "确认没有退化" in full
        page.screenshot(path="/tmp/supp_expanded.png", clip={
            "x": 400, "y": 60, "width": 900, "height": 520})
        gate.set()
        page.wait_for_function("!streaming", timeout=20000)
        page.wait_for_timeout(600)
        print("STATE_TEXT", page.locator(".turn-input-list").inner_text()[:60])
        page.screenshot(path="/tmp/supp_applied.png", clip={
            "x": 400, "y": 60, "width": 900, "height": 520})
        # 移动端
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function('document.documentElement.dataset.mode==="mobile"')
        page.wait_for_timeout(300)
        assert page.locator(".composer").evaluate("el=>el.scrollWidth<=el.clientWidth")
        page.screenshot(path="/tmp/supp_mobile.png", full_page=False)
        browser.close()
    print("SHOTS OK")
finally:
    gate.set()
    for agent in list(busref._running_agents.values()):
        agent.stop()
    case.tearDown()
    case.doCleanups()
    for item in reversed(patches):
        item.stop()
