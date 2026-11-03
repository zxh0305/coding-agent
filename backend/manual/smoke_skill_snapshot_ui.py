"""Historical skill inspection must not resize messages or shift chat scroll.
Uses a temporary database and real snapshot API, with simulated SSE and no model calls.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect
import db, skills
case=ApiTestBase();case.setUp()
try:
    old_body='阅读相关代码和上下文，优先找出会影响实际行为的问题。\n逐项说明问题、触发条件、影响和修改建议，并标注文件位置。\n按严重程度排序，不编造未运行的测试结果。'
    original=skills.save(case.uid,{'id':'review','name':'代码审查','body':old_body})
    other=skills.save(case.uid,{'id':'summary','name':'内容整理','body':'保留重点，说明尚未确定的内容。'})
    db.set_session_title('s1','检查当前项目功能')
    db.save_messages('s1',[
      {'role':'user','content':'你现在有什么功能','_mid':'uOld','_skills':[original]},
      {'role':'assistant','content':'我是你的本地编程助手，可以帮你读代码、检查问题和运行测试。','_mid':'aOld'},
      {'role':'user','content':'再帮我整理一下','_mid':'uOther','_skills':[other]},
      {'role':'assistant','content':'可以，请告诉我需要整理的内容。','_mid':'aOther'}],{})
    skills.save(case.uid,{'id':'review','name':'代码审查','body':'THIS_IS_THE_LATEST_VERSION_NOT_THE_MESSAGE_SNAPSHOT'})
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1400,'height':950})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.add_init_script('window.EventSource=class {constructor(){}close(){}addEventListener(){}}')
        page.add_init_script('localStorage.setItem("auth_token",'+repr(case.token)+');localStorage.setItem("auth_username","tester");')
        def show_session():
            expect(page.locator('#layout')).to_be_visible()
            page.wait_for_function('sessionsCache.some(s=>s.id==="s1")')
            page.evaluate('switchSession("s1")');page.evaluate('finishBoot({running:false})')
        page.goto(case.base);show_session()
        badge=page.locator('button.ub-skill').first;pop=page.locator('#message-skill-popover')
        body=page.locator('#message-skill-body')
        def geometry():
            return page.evaluate('''() => ({boxes:[...document.querySelectorAll('#chat .bubble')].map(e=>{const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}),height:chatEl.scrollHeight,scroll:chatEl.scrollTop})''')
        assert original['version'][:8] not in badge.inner_text(),'version hash must not clutter the badge'
        before=geometry();badge.click();expect(pop).to_be_visible();expect(body).to_have_text(old_body)
        assert geometry()==before,'opening skill resizes or moves chat'
        expect(page.locator('#message-skill-version')).to_have_text(original['version'][:8])
        page.screenshot(path='/tmp/skill-snapshot-open.png',animations='disabled')
        badge.click();expect(pop).not_to_be_visible();assert geometry()==before,'same-chip close moves chat'
        badge.focus();badge.press('Enter');expect(pop).to_be_visible()
        page.keyboard.press('Escape');expect(pop).not_to_be_visible();expect(badge).to_be_focused()
        badge.click();expect(pop).to_be_visible();page.locator('#message-skill-close').click()
        expect(pop).not_to_be_visible();assert geometry()==before,'explicit close moves chat'
        badge.click();page.locator('#conversation-title').click();expect(pop).not_to_be_visible()
        # API failure remains local to the popup, with an explicit retry.
        page.reload();show_session()
        badge=page.locator('button.ub-skill').first
        page.route('**/api/skills?mid=uOld*',lambda route:route.fulfill(status=500,json={'error':'临时加载失败'}))
        before=geometry();badge.click();expect(page.locator('#message-skill-retry')).to_be_visible()
        assert geometry()==before,'load failure changes message layout'
        page.unroute('**/api/skills?mid=uOld*');page.locator('#message-skill-retry').click();expect(body).to_have_text(old_body)
        page.locator('#message-skill-close').click()
        # A slow old response cannot replace another chip's contents.
        page.reload();show_session()
        held=[];page.route('**/api/skills?mid=uOld*',lambda route:held.append(route))
        page.locator('button.ub-skill').first.click();expect(page.locator('#message-skill-status')).to_have_text('正在加载')
        page.locator('#message-skill-close').click();page.locator('button.ub-skill').nth(1).click()
        expect(body).to_have_text(other['body']);assert held
        with page.expect_response(lambda r:'mid=uOld' in r.url):held[0].fulfill(json={'skills':[original]})
        expect(body).to_have_text(other['body'])
        page.locator('#message-skill-close').click();page.unroute('**/api/skills?mid=uOld*')
        # Cached snapshot is usable on narrow screens, with no viewport or chat overflow.
        for w in (390,320):
            page.set_viewport_size({'width':w,'height':650})
            page.wait_for_function('document.documentElement.dataset.mode === "mobile" && document.querySelector(".sidebar").getBoundingClientRect().right <= 1')
            badge=page.locator('button.ub-skill').first
            badge.scroll_into_view_if_needed();before=geometry();badge.click();expect(body).to_have_text(old_body)
            assert geometry()==before,'mobile skill open shifts chat'
            assert pop.evaluate('(e)=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}'),'popup outside viewport'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'page overflow'
            if w==390:page.screenshot(path='/tmp/skill-snapshot-mobile.png',animations='disabled')
            page.locator('#message-skill-close').click();assert geometry()==before,'mobile close shifts chat'
        page.locator('button.ub-skill').first.click();expect(pop).to_be_visible()
        page.evaluate('switchSession("s2");');expect(pop).not_to_be_visible()
        assert not errors,errors
        browser.close();print('PASS: snapshot fidelity, identical chat geometry, toggle/Escape/outside close, keyboard focus, retry, stale response, mobile and session isolation')
finally:
    case.tearDown();case.doCleanups()
