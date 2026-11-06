"""Real queue API and browser: accepted work survives refresh and cross-tab edits."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
import db
from services import submission_queue as queue
from playwright.sync_api import sync_playwright, expect

case=ApiTestBase();case.setUp();case.set_global("p1","a-small")
try:
    with patch("routes.sessions._start_submission"), sync_playwright() as pw:
        browser=pw.chromium.launch()
        def new_page():
            page=browser.new_page(viewport={"width":1280,"height":900})
            page.add_init_script("""window.EventSource=class {
              constructor(){setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'caught_up',running:false}),lastEventId:''}),0);}
              addEventListener(){} close(){}
            };""")
            page.add_init_script("localStorage.setItem('auth_token',"+repr(case.token)+");")
            page.goto(case.base);expect(page.locator("#layout")).to_be_visible()
            page.evaluate('switchSession("s1")');page.wait_for_function('bootBuffer===null && !workspacePending')
            return page
        case.call("POST","/api/sessions/s1/messages",{"message":"first queued","nonce":"first"})
        case.call("POST","/api/sessions/s1/messages",{"message":"second queued","nonce":"second"})
        page=new_page(); errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        expect(page.locator("#server-queue-bar")).to_contain_text("first queued")
        page.locator("#server-queue-bar .queue-item").last.get_by_text("上移",exact=True).click()
        expect(page.locator("#server-queue-bar .q-text").first).to_contain_text("second queued")
        page.locator("#server-queue-bar .queue-item").first.get_by_text("编辑",exact=True).click()
        page.get_by_label("排队需求正文").fill("edited second")
        page.get_by_role("button",name="保存修改",exact=True).click()
        expect(page.locator(".queue-edit-dialog")).not_to_be_visible()
        expect(page.locator("#server-queue-bar .q-text").first).to_contain_text("edited second")
        other=new_page()
        expect(other.locator("#server-queue-bar")).to_contain_text("edited second")
        other.locator("#server-queue-bar .queue-item").last.get_by_text("移除",exact=True).click()
        page.evaluate("ServerQueueUI.refresh()")
        expect(page.locator("#server-queue-bar .queue-item")).to_have_count(1)
        page.evaluate("setStreaming(true)")
        page.locator("#input").fill("added while running");page.locator("#input").press("Enter")
        expect(page.locator("#server-queue-bar")).to_contain_text("added while running")
        assert page.locator("#chat .bubble.user").count()==0, "queued input must not appear as an executed turn"
        def unavailable(route):route.fulfill(status=404,json={"error":"unknown endpoint"})
        page.route("**/api/sessions/s1/queue",unavailable)
        page.evaluate("ServerQueueUI.refresh()")
        expect(page.locator(".server-queue-status")).to_contain_text("重启后端")
        before=case.call("GET","/api/sessions/s1/queue")[1]["items"]
        page.locator("#input").fill("keep locally until server supports queue")
        page.locator("#input").press("Enter")
        expect(page.locator("#queue-bar")).to_contain_text("发送失败")
        self_items=case.call("GET","/api/sessions/s1/queue")[1]["items"]
        assert len(self_items)==len(before), "unsupported queue must not fall through to the old submit API"
        page.locator("#queue-bar").get_by_title("删除",exact=True).click()
        page.unroute("**/api/sessions/s1/queue",unavailable)
        page.evaluate("ServerQueueUI.refresh()")
        expect(page.locator(".server-queue-status")).to_have_count(0)
        page.reload();expect(page.locator("#layout")).to_be_visible()
        page.evaluate('switchSession("s1")');page.wait_for_function('bootBuffer===null && !workspacePending')
        expect(page.locator("#server-queue-bar .queue-item")).to_have_count(2)
        page.locator("#server-queue-bar .queue-item").first.get_by_text("编辑",exact=True).click()
        page.get_by_label("排队需求正文").fill("keep draft on conflict")
        current=case.call("GET","/api/sessions/s1/queue")[1]["items"][0]
        case.call("POST","/api/sessions/s1/queue",{"mid":current["mid"],"revision":current["revision"],"action":"edit","text":"external edit"})
        page.get_by_role("button",name="保存修改",exact=True).click()
        expect(page.locator(".queue-edit-dialog")).to_contain_text("队列已变化")
        expect(page.get_by_label("排队需求正文")).to_have_value("keep draft on conflict")
        page.get_by_role("button",name="取消",exact=True).click()
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function('document.documentElement.dataset.mode === "mobile"')
        assert page.locator(".composer").evaluate("el=>el.scrollWidth<=el.clientWidth")
        page.evaluate('switchSession("s2")');page.wait_for_function('bootBuffer===null && !workspacePending')
        expect(page.locator("#server-queue-bar")).not_to_be_visible()
        head=queue.next_entry("s1")
        queue.take_next("s1",head["workspace"],"interrupted-test")
        db.mark_running_turns_interrupted()
        page.evaluate('switchSession("s1")');page.wait_for_function('bootBuffer===null && !workspacePending')
        expect(page.locator("#server-queue-bar")).to_contain_text("执行中断")
        page.evaluate("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copied=text}}})")
        page.get_by_role("button",name="复制需求",exact=True).click()
        page.wait_for_function("window.copied==='external edit'")
        page.set_viewport_size({"width":320,"height":844})
        page.wait_for_function("innerWidth===320")
        assert page.locator(".composer").evaluate("el=>el.scrollWidth<=el.clientWidth")
        page.get_by_role("button",name="移除记录",exact=True).click()
        expect(page.get_by_role("button",name="复制需求",exact=True)).to_have_count(0)
        expect(page.locator("#server-queue-bar .queue-item")).to_have_count(1)
        expect(page.get_by_role("button",name="继续排队",exact=True)).to_be_visible()
        assert not errors,errors
        browser.close()
    print("PASS: durable queue, edit, reorder, cancel, cross-tab conflict, busy submit, refresh and mobile")
finally:
    case.tearDown();case.doCleanups()
