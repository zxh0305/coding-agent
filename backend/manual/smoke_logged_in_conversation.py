"""Real login, HTTP submissions, Agent loop, queue consumer and SSE. Only the model is controlled."""
import binascii
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from routes import busref
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

gate = threading.Event()
model_entered = threading.Event()
stop_entered = threading.Event()

def sample_png():
    def chunk(kind, data):
        return struct.pack("!I",len(data))+kind+data+struct.pack("!I",binascii.crc32(kind+data)&0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack("!IIBBBBB",8,8,8,6,0,0,0))+
            chunk(b"IDAT",zlib.compress((b"\x00"+b"\xff\xff\xff\xff"*8)*8))+chunk(b"IEND",b""))

class ControlledModel:
    def chat(self, messages, **kwargs):
        return {"role":"assistant","content":"浏览器对话回归"}

    def chat_stream(self, messages, tools=None, cancel=None):
        users = [message for message in messages if message.get("role")=="user"]
        content = users[-1].get("content", "")
        if isinstance(content,list):
            text = next((part.get("text", "") for part in content if part.get("type")=="text"), "")
        else:
            text = content
        yield "reasoning_delta", "正在检查本轮输入。"
        if "round1" in text or "stop-round" in text:
            (model_entered if "round1" in text else stop_entered).set()
            deadline = time.monotonic()+60
            while not gate.is_set() and time.monotonic()<deadline:
                if cancel and cancel.wait(.05):
                    yield "message", {"role":"assistant","content":"已收到停止请求"}
                    return
        if "tool-round" in text and messages[-1].get("role")!="tool" and tools:
            yield "message", {"role":"assistant","content":"读取项目说明。","tool_calls":[{
                "id":"test-read", "type":"function", "function":{"name":"read_file","arguments":'{"path":"README.txt"}'}}]}
            return
        answer = "完成：" + text
        for chunk in [answer[:3],answer[3:]]:
            if cancel and cancel.wait(.04): break
            yield "delta",chunk
        yield "usage", {"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}
        yield "message", {"role":"assistant","content":answer}


case=ApiTestBase();case.setUp();case.set_global("p1","a-small")
db.create_user("browser-tester","browser-test-pass")
test_uid=db.get_user_by_name("browser-tester")["id"]
workspace=Path(case.tmp)/"workspace";workspace.mkdir()
(workspace/"README.txt").write_text("Isolated browser regression project.")
db.set_setting(f"default_workspace:{test_uid}",str(workspace))
patches=[patch("app.create_client",side_effect=lambda *args,**kwargs:ControlledModel()),
         patch("services.turn._spawn_memory_extraction",return_value=None),
         patch("services.turn._spawn_title_generation",return_value=None)]
for item in patches:item.start()
try:
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        errors=[]
        def login_page():
            page=browser.new_page(viewport={"width":1365,"height":960})
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto(case.base)
            expect(page.locator("#login-page")).to_be_visible()
            page.locator("#login-user").fill("browser-tester");page.locator("#login-pass").fill("browser-test-pass")
            page.locator("#login-submit").click()
            expect(page.locator("#layout")).to_be_visible()
            return page
        page=login_page()
        page.locator("#new-task").click()
        page.locator("#input").fill("round1 保持运行供排队测试")
        page.locator("#input").press("Enter")
        assert model_entered.wait(5)
        page.wait_for_function("!!currentSession && !!activeTurnMid")
        sid=page.evaluate("currentSession")
        page.locator("#file-input").set_input_files({"name":"sample.png","mimeType":"image/png",
            "buffer":sample_png()})
        page.wait_for_function("attachments.length===1 && attachments.every(a=>!!a.data || a.uploaded)")
        page.locator("#input").fill("image-round 附件需求");page.locator("#input").press("Enter")
        expect(page.locator("#server-queue-bar")).to_contain_text("image-round")
        page.locator("#input").fill("tool-round 工具读取需求");page.locator("#input").press("Enter")
        expect(page.locator("#server-queue-bar .queue-item")).to_have_count(2)
        page.locator("#server-queue-bar .queue-item").first.get_by_text("编辑",exact=True).click()
        page.get_by_label("排队需求正文").fill("image-round 已编辑的附件需求")
        page.get_by_role("button",name="保存修改",exact=True).click()
        expect(page.locator(".queue-edit-dialog")).not_to_be_visible()
        page.locator("#server-queue-bar .queue-item").last.get_by_text("上移",exact=True).click()
        expect(page.locator("#server-queue-bar .q-text").first).to_contain_text("tool-round")
        other=login_page();other.evaluate("sid=>switchSession(sid)",sid)
        other.wait_for_function("bootBuffer===null && !workspacePending")
        expect(other.locator("#server-queue-bar .queue-item")).to_have_count(2)
        expect(other.locator("#chat .bubble.user")).to_have_count(1)
        page.reload();expect(page.locator("#layout")).to_be_visible()
        page.evaluate("sid=>switchSession(sid)",sid);page.wait_for_function("bootBuffer===null && !workspacePending")
        expect(page.locator("#server-queue-bar .queue-item")).to_have_count(2)
        gate.set()
        expect(page.locator("#chat .bubble.assistant")).to_have_count(3,timeout=15000)
        page.wait_for_function("!streaming && inFlight.size===0")
        expect(page.locator("#chat .bubble.user")).to_have_count(3)
        users=page.locator("#chat .bubble.user .ub-text").all_text_contents()
        assert "round1" in users[0] and "tool-round" in users[1] and "image-round" in users[2],users
        expect(page.locator("#chat .bubble.user .msg-img")).to_have_count(1)
        page.wait_for_function("document.querySelector('#chat .bubble.user .msg-img')?.naturalWidth>0")
        expect(other.locator("#chat .bubble.assistant")).to_have_count(3)
        expect(other.locator("#chat .bubble.user .msg-img")).to_have_count(1)
        page.screenshot(path="/tmp/logged-in-conversation-desktop.png")
        page.reload();expect(page.locator("#layout")).to_be_visible()
        page.evaluate("sid=>switchSession(sid)",sid);page.wait_for_function("bootBuffer===null && !workspacePending")
        expect(page.locator("#chat .bubble.assistant")).to_have_count(3)
        expect(page.locator("#chat .bubble.user .msg-img")).to_have_count(1)
        gate.clear()
        page.locator("#input").fill("stop-round 验证停止");page.locator("#input").press("Enter")
        assert stop_entered.wait(5)
        page.wait_for_timeout(400)
        page.locator("#send").click()
        expect(page.locator("#chat .bubble.assistant")).to_have_count(4,timeout=10000)
        page.wait_for_function("!streaming")
        expect(page.locator("#chat .bubble.assistant").last).to_contain_text("停止")
        page.locator("#input").fill("final-round 停止后继续对话");page.locator("#input").press("Enter")
        expect(page.locator("#chat .bubble.assistant")).to_have_count(5,timeout=10000)
        page.wait_for_function("!streaming")
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function('document.documentElement.dataset.mode==="mobile"')
        assert page.locator(".composer").evaluate("el=>el.scrollWidth<=el.clientWidth")
        page.screenshot(path="/tmp/logged-in-conversation-mobile.png")
        assert not errors,errors
        browser.close()
    print("PASS: real login + Agent + SSE, five turns, queued images, tools, edits, reorder, two clients, reload, stop and resume")
finally:
    gate.set()
    for agent in list(busref._running_agents.values()):agent.stop()
    case.tearDown();case.doCleanups()
    for item in reversed(patches):item.stop()
