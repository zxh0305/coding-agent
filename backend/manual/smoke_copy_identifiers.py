"""Copy actual persisted IDs, optimistic backfill and navigation without model calls."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

case = ApiTestBase()
case.setUp()
try:
    db.save_messages("s1", [{"role":"user", "content":"排查请求", "_mid":"requirement-one"}], {})
    db.save_messages("s2", [{"role":"user", "content":"另一项目", "_mid":"requirement-two"}], {})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width":1280, "height":900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
          window.copied=[];
          Object.defineProperty(navigator, 'clipboard', {value:{writeText:async text=>copied.push(text)}});
          window.EventSource=class {
            constructor(){setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'caught_up',running:false}),lastEventId:''}),0);}
            close(){} addEventListener(){}
          };
        """)
        page.add_init_script("localStorage.setItem('auth_token'," + repr(case.token) + ");")
        page.goto(case.base)
        expect(page.locator("#layout")).to_be_visible()
        expect(page.locator("#copy-session-id")).to_be_disabled()
        def switch(sid):
            page.evaluate("sid=>switchSession(sid)", sid)
            page.wait_for_function("bootBuffer===null && !workspacePending")
        switch("s1")
        page.locator("#copy-session-id").click()
        assert page.evaluate("copied.at(-1)") == "s1"
        page.locator('[data-mid="requirement-one"]').hover()
        page.locator('[data-mid="requirement-one"]').locator("..").locator('.copy-requirement-id').click()
        assert page.evaluate("copied.at(-1)") == "requirement-one"
        page.evaluate("userBubble('刚发送', [], null)")
        last = page.locator('[data-role="user"]').last.locator("..")
        last.hover()
        count = page.evaluate("copied.length")
        last.locator(".copy-requirement-id").click()
        assert page.evaluate("copied.length") == count
        page.evaluate("tagLastUntaggedUser('server-assigned-id')")
        last.locator(".copy-requirement-id").click()
        assert page.evaluate("copied.at(-1)") == "server-assigned-id"
        switch("s2")
        page.locator("#copy-session-id").click()
        assert page.evaluate("copied.at(-1)") == "s2"
        page.locator('[data-mid="requirement-two"]').hover()
        page.locator(".copy-requirement-id").click()
        assert page.evaluate("copied.at(-1)") == "requirement-two"
        page.reload()
        expect(page.locator("#layout")).to_be_visible()
        switch("s1")
        page.set_viewport_size({"width":390, "height":844})
        page.locator(".copy-requirement-id").click()
        assert page.evaluate("copied.at(-1)") == "requirement-one"
        assert page.locator("#chat").evaluate("el=>el.scrollWidth<=el.clientWidth")
        assert page.locator(".top-bar").evaluate("el=>el.scrollWidth<=el.clientWidth")
        assert not errors, errors
        browser.close()
    print("PASS: session/requirement ID, pending and assigned IDs, navigation, reload, mobile")
finally:
    case.tearDown()
    case.doCleanups()
