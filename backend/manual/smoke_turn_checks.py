"""Real check storage + browser protocol replay, without model calls."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

case = ApiTestBase()
case.setUp()
try:
    message = {"role":"user", "content":"修复问题", "_mid":"check-input"}
    db.save_messages("s1", [message], {})
    db.turn_started("s1", "check-input")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width":1280,"height":900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("""
            window.EventSource=class {
              constructor(url) { window.source=this;
                setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'caught_up',running:false}),lastEventId:''}),0);
              }
              addEventListener(){} close(){}
            };
            window.emit=e=>source.onmessage({data:JSON.stringify(e),lastEventId:''});
        """)
        page.add_init_script("localStorage.setItem('auth_token'," + repr(case.token) +
                             ");localStorage.setItem('auth_username','tester');")
        page.goto(case.base)
        expect(page.locator("#layout")).to_be_visible()
        page.evaluate('switchSession("s1")')
        page.wait_for_function('bootBuffer === null && !workspacePending')
        def emit(event):
            page.evaluate("(event)=>window.emit(event)", event)
        emit({"type":"turn_start","input":"修复问题","user_mid":"check-input"})
        emit({"type":"round","round":1,"mid":"candidate"})
        emit({"type":"answer_delta","mid":"candidate","delta":"待验证的回复"})
        emit({"type":"turn_phase","user_mid":"check-input","phase":"verifying"})
        ident = db.start_check("s1", "check-input", "unit", 1, {"cmd":"make test","attempt":1})
        emit({"type":"check_update","user_mid":"check-input","check_id":ident,
              "cmd":"make test","attempt":1,"status":"running"})
        expect(page.locator("#conversation-status")).to_contain_text("运行检查")
        assert page.locator("#chat .bubble.assistant").count() == 0
        db.finish_check("s1", ident, "failed", {"cmd":"make test","attempt":1,"ok":False}, "failure <script>bad</script>")
        emit({"type":"check_update","user_mid":"check-input","check_id":ident,"cmd":"make test",
              "attempt":1,"status":"failed","ok":False,"output":"failure <script>bad</script>"})
        emit({"type":"turn_phase","user_mid":"check-input","phase":"repairing","assessment":"failed"})
        expect(page.locator("#conversation-status")).to_contain_text("修复检查失败")
        passed = db.start_check("s1", "check-input", "unit", 2, {"cmd":"make test","attempt":2})
        db.finish_check("s1", passed, "passed", {"cmd":"make test","attempt":2,"ok":True}, "passed")
        emit({"type":"check_update","user_mid":"check-input","check_id":passed,"cmd":"make test",
              "attempt":2,"status":"passed","ok":True,"output":""})
        emit({"type":"round","round":2,"mid":"final"})
        emit({"type":"done","mid":"final","answer":"修复并验证通过","elapsed_s":2,"usage":{}})
        db.turn_finished("s1", "check-input", "done")
        db.set_assessment("s1", "check-input", "finished", "passed")
        emit({"type":"turn_phase","user_mid":"check-input","phase":"finished","assessment":"passed"})
        emit({"type":"turn_end","user_mid":"check-input"})
        expect(page.locator("#chat .bubble.assistant")).to_have_count(1)
        card = page.locator('details[data-check-mid="check-input"]').first
        expect(card.locator("summary").first).to_contain_text("1 项通过")
        db.save_messages("s1", [
            {"role":"assistant","content":"未验收的旧候选","_completion_candidate":True},
            {"role":"assistant","content":"修复并验证通过"}
        ], {})
        page.reload()
        expect(page.locator("#layout")).to_be_visible()
        page.evaluate('switchSession("s1")')
        page.wait_for_function('bootBuffer === null && !workspacePending')
        expect(page.locator("#chat .bubble.assistant")).to_have_count(1)
        card = page.locator('details[data-check-mid="check-input"]').first
        card.locator("summary").first.click()
        expect(card).to_contain_text("make test")
        expect(card).to_contain_text("查看前次检查")
        assert not page.evaluate("!!window.bad")
        emit({"type":"turn_phase", "user_mid":"blocked-input", "phase":"finalizing",
              "assessment":"blocked", "reason":"项目解释器已失效"})
        blocked = page.locator('details[data-check-mid="blocked-input"]')
        expect(blocked).to_have_count(1)
        expect(blocked.locator("summary")).to_contain_text("检查受阻")
        blocked.locator("summary").click()
        expect(blocked).to_contain_text("项目解释器已失效")
        page.set_viewport_size({"width":390,"height":844})
        assert page.locator("#chat").evaluate("(el)=>el.scrollWidth<=el.clientWidth")
        assert not errors, errors
        browser.close()
    print("PASS: verifying/repairing phases, single final answer, durable results, prior attempts, history and mobile")
finally:
    case.tearDown()
    case.doCleanups()
