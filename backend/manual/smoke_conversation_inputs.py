"""Real login/Agent/SSE for in-turn steering and persisted quote snapshots."""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from routes import busref
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect

gate=threading.Event();entered=threading.Event();stop_entered=threading.Event();seen=[]
class Model:
    def chat(self,messages,**kwargs):return {"role":"assistant","content":"补充引用回归"}
    def chat_stream(self,messages,tools=None,cancel=None):
        users=[m for m in messages if m.get("role")=="user"]
        text=users[-1].get("content","")
        if isinstance(text,list):text="\n".join(p.get("text","") for p in text if p.get("type")=="text")
        seen.append(text)
        yield "reasoning_delta","正在核对要求。"
        if text in ("hold-current","hold-stop"):
            (entered if text=="hold-current" else stop_entered).set()
            deadline=time.monotonic()+60
            while not gate.is_set() and time.monotonic()<deadline:
                if cancel.wait(.04):yield "message",{"role":"assistant","content":"停止"};return
            answer="旧候选，不应作为最终回复"
        elif any("保留兼容" in str(m.get("content")) for m in users) and "quoted_context" not in text and "引用" not in text and text!="queued-after":
            answer="已保留兼容\n\n```python\nanswer = 42\n```"
        elif "用户引用的对话片段" in text:
            answer="引用接收完成"
        else:answer="完成："+text
        yield "delta",answer
        yield "message",{"role":"assistant","content":answer}

case=ApiTestBase();case.setUp();case.set_global("p1","a-small")
db.create_user("input-tester","browser-test-pass");uid=db.get_user_by_name("input-tester")["id"]
workspace=Path(case.tmp)/"workspace";workspace.mkdir();(workspace/"README.txt").write_text("test")
db.set_setting(f"default_workspace:{uid}",str(workspace))
patches=[patch("app.create_client",side_effect=lambda *args,**kwargs:Model()),patch("services.turn._spawn_memory_extraction"),patch("services.turn._spawn_title_generation")]
for item in patches:item.start()
try:
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={"width":1365,"height":960})
        errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(case.base);page.locator("#login-user").fill("input-tester");page.locator("#login-pass").fill("browser-test-pass");page.locator("#login-submit").click()
        expect(page.locator("#layout")).to_be_visible()
        page.locator("#new-task").click();page.locator("#input").fill("hold-current");page.locator("#input").press("Enter")
        assert entered.wait(5);page.wait_for_function("!!activeTurnMid")
        sid=page.evaluate("currentSession");root=page.evaluate("activeTurnMid")
        page.get_by_label("发送方式").select_option("steer")
        page.locator("#input").fill("追加：保留兼容");page.locator("#input").press("Enter")
        expect(page.locator(".turn-input-list")).to_contain_text("等待纳入")
        assert page.locator("#chat .bubble.user").count()==1
        page.locator("#input").fill("queued-after");page.locator("#input").press("Enter")
        expect(page.locator("#server-queue-bar")).to_contain_text("queued-after")
        page.reload();expect(page.locator("#layout")).to_be_visible();page.evaluate("sid=>switchSession(sid)",sid)
        page.wait_for_function("bootBuffer===null && !workspacePending")
        expect(page.locator(".turn-input-list")).to_contain_text("等待纳入")
        gate.set()
        expect(page.locator("#chat .bubble.assistant")).to_have_count(2,timeout=15000)
        page.wait_for_function("!streaming")
        expect(page.locator("#chat .bubble.assistant").first).to_contain_text("已保留兼容")
        expect(page.locator(".turn-input-list")).to_contain_text("已纳入")
        expect(page.locator("#chat .bubble.user")).to_have_count(2)
        page.wait_for_function("document.querySelector('#chat .bubble.assistant')?.dataset.persisted==='true'")
        source_mid=page.locator("#chat .bubble.assistant").first.get_attribute("data-mid")
        page.locator("#chat .bubble.assistant code").first.evaluate("el=>{const range=document.createRange();range.selectNodeContents(el);const s=getSelection();s.removeAllRanges();s.addRange(range);document.dispatchEvent(new Event('selectionchange'));}")
        page.get_by_role("button",name="引用提问",exact=True).click()
        expect(page.locator("#reference-tray")).to_contain_text("answer = 42")
        page.locator("#input").fill("请解释引用代码")
        page.evaluate("saveDraft(currentSession)")
        page.reload();expect(page.locator("#layout")).to_be_visible();page.evaluate("sid=>switchSession(sid)",sid)
        page.wait_for_function("bootBuffer===null && !workspacePending")
        expect(page.locator("#reference-tray")).to_contain_text("answer = 42")
        page.locator("#input").press("Enter")
        expect(page.locator("#chat .bubble.assistant")).to_have_count(3,timeout=10000)
        page.wait_for_function("!streaming")
        expect(page.locator("#chat .bubble.assistant").last).to_contain_text("引用接收完成")
        page.locator("#chat .bubble.user .ub-reference").click()
        expect(page.locator(".reference-view-dialog")).to_contain_text("answer = 42")
        page.get_by_role("button",name="定位原文",exact=True).click()
        expect(page.locator(".reference-view-dialog")).not_to_be_visible()
        assert page.locator(f'[data-mid="{source_mid}"]').count()==1
        gate.clear();page.locator("#input").fill("hold-stop");page.locator("#input").press("Enter")
        assert stop_entered.wait(5)
        page.get_by_label("发送方式").select_option("steer")
        page.locator("#input").fill("结束后不能偷偷转成下一项")
        page.wait_for_timeout(400);page.locator("#send").click()
        page.wait_for_function("!streaming")
        before=page.locator("#chat .bubble.user").count()
        page.locator("#input").press("Enter")
        expect(page.locator("#input")).to_have_value("结束后不能偷偷转成下一项")
        assert page.locator("#chat .bubble.user").count()==before
        page.get_by_label("发送方式").select_option("queue")
        page.locator("#input").fill("引用与补充验证结束");page.locator("#input").press("Enter")
        expect(page.locator("#chat .bubble.assistant")).to_have_count(5,timeout=10000)
        page.wait_for_function("!streaming")
        page.set_viewport_size({"width":390,"height":844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"')
        assert page.locator(".composer").evaluate("el=>el.scrollWidth<=el.clientWidth")
        assert not errors,errors
        browser.close()
    print("PASS: real login steering, pending/apply/reload, single final, queue isolation, quote snapshots and jumps, stale-target draft and mobile")
finally:
    gate.set()
    for agent in list(busref._running_agents.values()):agent.stop()
    case.tearDown();case.doCleanups()
    for item in reversed(patches):item.stop()
