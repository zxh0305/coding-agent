"""Isolated browser smoke test. Requires Python Playwright and Chromium.
Run: python3 backend/manual/smoke_skills_interaction.py
No model calls are made; all data is in a temporary database.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

case = ApiTestBase()
case.setUp()
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width':1400,'height':1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(case.base)
        page.evaluate('(token) => { localStorage.setItem("auth_token",token); localStorage.setItem("auth_username","tester"); }', case.token)
        page.reload()
        page.locator('#skills-chip').click()
        expect(page.locator('#skill-picker')).to_be_visible()
        expect(page.locator('#skills-dialog')).not_to_be_visible()
        page.locator('#sp-manage').click()
        page.locator('#sk-example').click()
        page.locator('#sk-select').click()
        expect(page.locator('#skill-tray')).to_contain_text('代码审查 · 本次')
        expect(page.locator('#skills-dialog')).not_to_be_visible()
        # Configure a session scope from the selected tag and favorite from the picker.
        page.locator('#skill-tray button').first.click()
        page.get_by_label('当前会话', exact=True).check()
        expect(page.locator('#skill-tray')).to_contain_text('本会话')
        page.get_by_role('button', name='← 全部技能').click()
        page.get_by_role('button', name='收藏 代码审查', exact=True).click()
        page.locator('#sp-close').click()
        expect(page.locator('#skill-favorites')).to_contain_text('代码审查')
        page.reload()
        expect(page.locator('#skill-tray')).to_contain_text('本会话')
        expect(page.locator('#skill-favorites')).to_contain_text('代码审查')
        old = page.evaluate('SkillUI.selection()[0].version')
        # Save does not silently apply a new revision to the current selection.
        page.locator('#skill-tray button').first.click()
        page.get_by_role('button', name='编辑技能', exact=True).click()
        page.locator('#sk-body').fill('新的代码审查指令：检查边界条件。')
        page.locator('#sk-form button[type=submit]').click()
        expect(page.locator('#sk-status')).to_contain_text('当前选择保持原版本')
        assert page.evaluate('SkillUI.selection()[0].version') == old
        page.locator('#sk-close').click()
        page.locator('#skill-tray button').first.click()
        page.get_by_role('button', name='应用新版本', exact=True).click()
        assert page.evaluate('SkillUI.selection()[0].version') != old
        expect(page.locator('#skill-tray')).to_contain_text('本会话')
        page.locator('#sp-close').click()
        # Switch sessions: pinned skills belong only to the original draft/session.
        page.evaluate('switchSession("s1")')
        expect(page.locator('#skill-tray')).not_to_be_visible()
        page.locator('#skill-favorites button').first.click()
        page.locator('#skill-tray button').first.click()
        page.get_by_label('当前会话', exact=True).check()
        page.locator('#sp-close').click()
        page.evaluate('switchSession("s2")')
        expect(page.locator('#skill-tray')).not_to_be_visible()
        page.evaluate('switchSession("s1")')
        expect(page.locator('#skill-tray')).to_contain_text('本会话')
        # Add another one-time skill with a Chinese name and automatic ID.
        page.locator('#skills-chip').click(); page.locator('#sp-new').click()
        page.locator('#sk-name').fill('论文润色')
        page.locator('#sk-body').fill('保留科学结论，改善学术表达。')
        page.locator('#sk-select').click()
        expect(page.locator('#skill-tray')).to_contain_text('论文润色 · 本次')
        page.locator('#skills-chip').click()
        page.screenshot(path='/tmp/skills-quick-picker.png')
        page.locator('#sp-close').click()
        # Actual send path (HTTP stub avoids calling any model).
        requests = []
        def submit(route):
            requests.append(route.request.post_data_json)
            route.fulfill(status=200, json={'session_id':'s1'})
        page.route('**/api/sessions/s1/messages', submit)
        page.locator('#input').fill('检查这个项目')
        with page.expect_response(lambda r: '/api/sessions/s1/messages' in r.url and r.request.method == 'POST'):
            page.locator('#input').press('Enter')
        page.wait_for_function('document.querySelector("#input").value === ""')
        expect(page.locator('#skill-tray')).to_contain_text('代码审查 · 本会话')
        expect(page.locator('#skill-tray')).not_to_contain_text('论文润色')
        assert len(requests) == 1 and len(requests[0]['skills']) == 2
        # Queue a message, update the library, and ensure the queued snapshot is unchanged.
        page.locator('#input').fill('排队继续检查')
        page.locator('#input').press('Enter')
        expect(page.locator('#queue-bar')).to_contain_text('代码审查')
        queued_version = page.evaluate('pendingQueue[0].skills[0].version')
        page.locator('#skill-tray button').first.click()
        page.get_by_role('button', name='编辑技能', exact=True).click()
        page.locator('#sk-body').fill('再次修改后的指令')
        page.locator('#sk-form button[type=submit]').click()
        expect(page.locator('#sk-status')).to_contain_text('已保存')
        assert page.evaluate('pendingQueue[0].skills[0].version') == queued_version
        page.locator('#sk-close').click()
        # Dollar completion picks a skill without sending the message.
        page.locator('#input').fill('请使用 $论文')
        expect(page.locator('#skill-picker')).to_be_visible()
        page.locator('#input').press('Enter')
        assert page.locator('#input').input_value() == '请使用 '
        assert len(requests) == 1
        # Completing a skill already pinned must not deselect it.
        page.locator('#input').fill('$代码')
        expect(page.locator('#skill-picker')).to_be_visible()
        page.locator('#input').press('Enter')
        expect(page.locator('#skill-tray')).to_contain_text('代码审查 · 本会话')
        assert len(requests) == 1
        # Screenshot the scope settings.
        page.locator('#skill-tray button').first.click()
        page.screenshot(path='/tmp/skills-scope.png')
        page.set_viewport_size({'width':390,'height':844})
        expect(page.locator('#skill-picker')).to_be_visible()
        page.wait_for_function('''() => {const r=document.querySelector('#skill-picker').getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight;}''')
        page.screenshot(path='/tmp/skills-quick-mobile.png')
        assert not errors, errors
        browser.close()
        print('PASS: picker, favorites, scopes, version application, session isolation, send, queue snapshot, $ completion, mobile; no page errors.')
finally:
    case.tearDown()
    case.doCleanups()
