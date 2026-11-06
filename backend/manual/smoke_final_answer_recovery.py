"""Login/HTTP/Agent/SSE regression for tool-protocol leaks and final status."""
import sys,threading,time,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect
LEAK='<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="run_bash">must not execute</｜｜DSML｜｜ invoke>'
class Model:
    def chat(self,*args,**kwargs):return {"role":"assistant","content":"回归测试"}
    def chat_stream(self,messages,tools=None,cancel=None):
        recovery='上一条未形成可用的最终文字答复' in str(messages[-1].get('content'))
        fail=any(m.get('role')=='user' and m.get('content')=='invalid' for m in messages)
        answer='已完成文字恢复，没有执行额外命令。' if recovery and not fail else LEAK
        yield 'delta',answer;yield 'message',{'role':'assistant','content':answer}
case=ApiTestBase();case.setUp();case.set_global('p1','a-small');db.create_user('final-tester','browser-test-pass');uid=db.get_user_by_name('final-tester')['id']
workspace=Path(case.tmp)/'workspace';workspace.mkdir();db.set_setting(f'default_workspace:{uid}',str(workspace))
patches=[patch('app.create_client',side_effect=lambda *a,**kw:Model()),patch('services.turn._spawn_title_generation'),patch('services.turn._spawn_memory_extraction')]
for item in patches:item.start()
try:
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1365,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(case.base);page.locator('#login-user').fill('final-tester');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible()
        page.locator('#new-task').click();page.locator('#input').fill('recover');page.locator('#input').press('Enter')
        expect(page.locator('#chat .bubble.assistant')).to_contain_text('已完成文字恢复',timeout=15000);page.wait_for_function('!streaming')
        sid=page.evaluate('currentSession')
        with db._conn() as conn:assert conn.execute('select state from session_turns where session_id=?',(sid,)).fetchone()[0]=='done'
        page.locator('#new-task').click();page.locator('#input').fill('invalid');page.locator('#input').press('Enter')
        expect(page.locator('#chat .bubble.assistant')).to_contain_text('本轮未正常完成',timeout=15000);page.wait_for_function('!streaming');sid=page.evaluate('currentSession')
        with db._conn() as conn:assert conn.execute('select state,error from session_turns where session_id=?',(sid,)).fetchone()['state']=='error'
        assert 'DSML' not in page.locator('#chat .bubble.assistant').inner_text()
        page.reload();expect(page.locator('#layout')).to_be_visible();page.evaluate('sid=>switchSession(sid)',sid)
        expect(page.locator('#chat .bubble.assistant')).to_contain_text('本轮未正常完成');assert 'DSML' not in page.locator('#chat .bubble.assistant').inner_text()
        page.set_viewport_size({'width':390,'height':844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"');assert not errors,errors
        browser.close()
    print('PASS: real login, raw protocol recovery, one final, persistent failed delivery status, reload, mobile; no commands dispatched')
finally:
    for item in reversed(patches):item.stop()
    case.tearDown()
