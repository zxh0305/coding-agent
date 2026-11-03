"""Reproduce short reasoning followed by done in the same browser frame.
Checks the disclosure, responsive sizing and sidebar text alignment in a temp DB.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect
import db
case=ApiTestBase();case.setUp()
try:
    db.set_session_title('s1','日常问候开场')
    db.set_session_workspace('s1', str(Path(__file__).resolve().parents[2]))
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':950})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.add_init_script('window.EventSource=class {constructor(){}close(){}addEventListener(){}}')
        page.add_init_script('localStorage.setItem("auth_token",'+repr(case.token)+');localStorage.setItem("auth_username","tester");')
        page.goto(case.base);page.evaluate('switchSession("s1")')
        page.evaluate('''() => {
          finishBoot({running:false});myNonce='layout';userBubble('你好',[]);
          const events=[{type:'turn_start',nonce:'layout',user_mid:'uLayout',input:'你好'},
            {type:'round',round:1,mid:'aLayout'},
            {type:'reasoning_delta',delta:'用户在问候，简短回应即可。'},
            {type:'answer_delta',mid:'aLayout',delta:'你好！有什么可以帮你的？'},
            {type:'done',mid:'aLayout',answer:'你好！有什么可以帮你的？',elapsed_s:1.7,usage:{}},
            {type:'turn_end',user_mid:'uLayout'}];
          events.forEach((e,i)=>applyEvent(e,i+1));
        }''')
        trace=page.locator('#chat details.trace').first
        print('collapsed?', not trace.evaluate('(e)=>e.open'), 'body:',page.locator('.think-seg-body').all_text_contents())
        failures=[]
        if page.locator('.think-seg-body').text_content()!='用户在问候，简短回应即可。':failures.append('reasoning lost when done arrives before animation frame')
        if trace.locator('.proc-line').is_visible():failures.append('closed details still displays its process content')
        trace.locator(':scope > summary').click()
        expect(trace.locator('.proc-line')).to_be_visible()
        size=trace.locator('.proc-line').bounding_box();print('expanded short process size:',size)
        if size['height']>100:failures.append('short reasoning reserves an empty fixed-height box')
        if size['width']<500:failures.append('process column shrinks to its short heading')
        page.screenshot(path='/tmp/chat-layout-short-expanded.png')
        trace.locator(':scope > summary').click()
        page.screenshot(path='/tmp/chat-layout-short-collapsed.png')
        # Sidebar title and date should be on one line; preview should use full row width.
        page.evaluate('''() => {
          sessionsCache=[{id:'s1',title:'日常问候开场',updated:Date.now()/1000,preview:'你想看麒麟鲸的哪方面？我这边可以继续帮你分析。',workspace:'/tmp/mai_portal',state:'idle'},
            {id:'s2',title:'审查提交优化空间',updated:1791878520,preview:'**已修复并提交**（a95b4ef），相关检查通过。',workspace:'/tmp/coding-agent',state:'idle'}];
          renderSessions(sessionsCache);
        }''')
        row=page.locator('.task').filter(has_text='日常问候开场')
        name=row.locator('.t-name').bounding_box();time=row.locator('.t-time').bounding_box()
        if abs(name['y']-time['y'])>5:failures.append('date is centered between title and preview')
        if '**' in page.locator('.task').filter(has_text='审查提交优化空间').inner_text(): failures.append('preview exposes markdown markers')
        page.screenshot(path='/tmp/chat-layout-sidebar.png')
        # A finished history process uses the same closed disclosure and sizing.
        page.evaluate("""() => {
          const node=blocksRenderer.renderBlock({kind:'process',running:false,steps:0,elapsed:1.7,items:[{kind:'reasoning',text:'历史思考也应按内容排版。'}]});
          node.id='history-process-test';chatEl.appendChild(node);
        }""")
        history=page.locator('#history-process-test')
        if history.locator('.proc-line').is_visible():failures.append('history process expands by default')
        history.locator(':scope > summary').click()
        if history.locator('.proc-line').bounding_box()['height']>100:failures.append('history short reasoning leaves empty space')
        page.evaluate("document.querySelector('#history-process-test').remove()")
        # Long reasoning must scroll inside the process, with no page-wide overflow.
        page.evaluate("""() => {
          const box=document.querySelector('#chat .proc-line');
          box.querySelector('.think-seg-body').textContent='长思考内容用于检查限高与换行。\\n'.repeat(100);
        }""")
        trace.locator(':scope > summary').click()
        if not trace.locator('.proc-line').evaluate('(e)=>e.clientHeight<=180 && e.scrollHeight>e.clientHeight'):failures.append('long reasoning is not bounded')
        trace.locator(':scope > summary').click()
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_function('document.documentElement.dataset.mode === "mobile" && document.querySelector(".sidebar").getBoundingClientRect().right<=1')
        trace.locator(':scope > summary').click()
        expect(trace.locator('.proc-line')).to_be_visible()
        if not trace.evaluate('(e)=>e.getBoundingClientRect().right<=innerWidth'):failures.append('process overflows mobile viewport')
        if not page.evaluate('document.documentElement.scrollWidth<=innerWidth'):failures.append('page overflows mobile viewport')
        assert not errors,errors
        browser.close()
        assert not failures,failures
        print('PASS: same-frame reasoning, native disclosure, short process sizing, sidebar alignment, mobile bounds')
finally:
    case.tearDown();case.doCleanups()
