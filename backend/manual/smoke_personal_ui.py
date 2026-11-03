"""Personal UI: layout, loading lock, view-state hints, mobile and empty-state checks.
Temporary DB + simulated SSE. No real model calls or persistent user-data changes.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect
import db

case=ApiTestBase();case.setUp()
try:
    db.set_session_title('s1','整理项目界面与交互')
    db.set_session_title('s2','排查消息显示问题')
    messages=[{'role':'user','content':'帮我整理一下项目，先看看有哪些地方可以优化。','_mid':'ui-user'},
              {'role':'assistant','content':'可以先从三处入手：\n\n1. **聊天显示**：保持消息顺序，避免重连后重复。\n2. **输入区域**：固定发送按钮的位置，工具按宽度排列。\n3. **界面状态**：加载、处理和重连时给出明确提示。\n\n可以先检查改动范围，再运行项目验证：\n\n```bash\ngit diff --stat\npython3 -m unittest discover -s backend/tests\n```\n\n现有功能继续保留，优先做小幅调整。','_mid':'ui-answer'}]
    db.save_messages('s1',messages,{})
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.add_init_script('''window.sources=[];window.EventSource=class {
          constructor(url){this.url=url;sources.push(this);setTimeout(()=>{if(!this.closed)this.emit({type:'caught_up',running:false});},0);}
          emit(evt){if(!this.closed)this.onmessage?.({data:JSON.stringify(evt),lastEventId:''});}
          close(){this.closed=true;}addEventListener(){}
        };''')
        page.add_init_script('localStorage.setItem("auth_token",'+repr(case.token)+');localStorage.setItem("auth_username","tester");')
        page.goto(case.base)
        expect(page.locator('.empty-hint')).to_be_visible()
        page.screenshot(path='/tmp/personal-ui-empty.png')
        # A slow initial history load keeps the draft editable and refuses early sends.
        held=[]
        page.route('**/api/sessions/s1/messages?*',lambda route:held.append(route))
        page.evaluate('void (window.navigation = switchSession("s1"))')
        expect(page.locator('#conversation-status')).to_have_text('正在加载对话')
        expect(page.locator('#send')).to_be_disabled()
        expect(page.locator('#input')).to_be_editable()
        page.locator('#input').fill('尚未发送的草稿');page.locator('#input').press('Enter')
        assert page.locator('#input').input_value()=='尚未发送的草稿'
        payload={'messages':[{'role':'user','mid':'ui-user','ord':1024,'content':messages[0]['content']}, {'role':'assistant','mid':'ui-answer','ord':2048,'content':messages[1]['content']}], 'has_more':False,'user_index':[]}
        held[0].fulfill(json=payload)
        page.evaluate('window.navigation')
        page.unroute('**/api/sessions/s1/messages?*')
        expect(page.locator('#conversation-title')).to_have_text('整理项目界面与交互')
        expect(page.locator('#conversation-status')).to_have_text('已连接')
        expect(page.locator('#send')).to_be_enabled()
        expect(page.locator('.empty-hint')).not_to_be_visible()
        assert page.locator('#input').input_value()=='尚未发送的草稿'
        page.locator('#input').fill('继续检查界面细节，保持现有功能可用。')
        page.locator('#toast').click()
        assert page.locator('#chat .bubble.assistant').first.evaluate('(e)=>{const s=getComputedStyle(e);return s.borderTopStyle==="solid" && parseFloat(s.borderTopWidth)>=1 && s.backgroundColor!=="rgba(0, 0, 0, 0)";}'), 'assistant answer must have a display card'
        page.screenshot(path='/tmp/personal-ui-desktop.png')
        # State text remains correct when resizing; a display-mode change cannot clear it.
        page.evaluate('es.onerror();cancelReconnectTimer(true)')
        expect(page.locator('#conversation-status')).to_contain_text('正在重连')
        page.evaluate('sources.filter(s=>!s.closed).at(-1).emit({type:"caught_up",running:false})')
        page.evaluate('setStreaming(true)')
        expect(page.locator('#send')).to_contain_text('停止')
        expect(page.locator('#input')).to_have_attribute('placeholder','继续输入以排队后续修改')
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_function('document.documentElement.dataset.mode === "mobile" && document.querySelector(".sidebar").getBoundingClientRect().right <= 1')
        expect(page.locator('#input')).to_have_attribute('placeholder','继续输入以排队后续修改')
        page.evaluate('setStreaming(false)')
        expect(page.locator('#input')).to_have_attribute('placeholder','输入问题或任务，点发送')
        for w in (390,320):
            page.set_viewport_size({'width':w,'height':844})
            page.wait_for_function('(w)=>innerWidth===w',arg=w)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'page has horizontal overflow'
            assert page.locator('#send').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;}'), 'send is outside screen'
            assert page.locator('.top-bar').evaluate('(e)=>e.scrollWidth<=e.clientWidth'), 'header overflows'
            before=page.locator('#send').bounding_box()
            page.locator('.toolbar-tools').evaluate('(e)=>e.scrollLeft=e.scrollWidth')
            assert page.locator('#send').bounding_box()==before,'send moves with tool scrolling'
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path='/tmp/personal-ui-mobile.png')
        # A new task must be editable after browsing an archived task.
        page.evaluate('setComposerArchived(true)')
        expect(page.locator('#input')).to_be_disabled()
        page.evaluate('newTask()')
        expect(page.locator('#input')).to_be_editable()
        expect(page.locator('#send')).to_be_enabled()
        expect(page.locator('#conversation-status')).to_have_text('准备就绪')
        expect(page.locator('.empty-hint')).to_be_visible()
        assert not errors, errors
        browser.close()
        print('PASS: loading lock, draft preservation, state hints, empty-state removal, desktop/mobile bounds, fixed send, archive-to-new reset.')
finally:
    case.tearDown();case.doCleanups()
