"""Real login + visible shared Chromium, with isolated test data."""
import json,sys,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from browser_tools import BrowserManager,browser_type,browser_snapshot
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect
class Target(BaseHTTPRequestHandler):
    def do_GET(self):
        data=b'''<title>Interactive test</title><h1>Shared browser</h1><label>Name<input name="name" placeholder="Your name"></label><button onclick="document.querySelector('h1').textContent='Hello '+document.querySelector('input').value;localStorage.setItem('name',document.querySelector('input').value)">Submit</button>'''
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
case=ApiTestBase();case.setUp();db.create_user('window-tester','browser-test-pass');uid=db.get_user_by_name('window-tester')['id'];db.create_session('window-test',uid)
manager=BrowserManager('window-test');manager._profile_dir=lambda:Path(case.tmp)/'browser-profile'
server=ThreadingHTTPServer(('127.0.0.1',0),Target);threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_address[1]}'
try:
    with patch('browser_tools.manager_for',return_value=manager),sync_playwright() as pw:
        driver=pw.chromium.launch();page=driver.new_page(viewport={'width':1365,'height':960});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(case.base);page.locator('#login-user').fill('window-tester');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible()
        page.evaluate("switchSession('window-test')");page.wait_for_function('bootBuffer===null && !workspacePending')
        page.locator('#browser-chip').click();expect(page.locator('#browser-panel')).to_be_visible();page.locator('#browser-address').fill(url);page.locator('#browser-navigate button').click()
        expect(page.locator('#browser-status')).to_contain_text('你正在操作',timeout=15000);assert manager.state()['open'] and not manager.headless
        manager._call(lambda:manager._page.get_by_label('Name').fill('manual-user'));manager._call(lambda:manager._page.get_by_text('Submit',exact=True).click())
        assert manager._call(lambda:manager._page.locator('h1').inner_text())=='Hello manual-user';assert page.locator('#browser-view img').count()==0
        page.get_by_role('button',name='交回 Agent',exact=True).click();expect(page.locator('#browser-status')).to_contain_text('Agent 可操作')
        ctx=SimpleNamespace(browser=manager,cancel_event=threading.Event());result=[]
        thread=threading.Thread(target=lambda:result.append(browser_type('Name','agent-user',ctx)));thread.start();thread.join(10);assert json.loads(result[0])['ok']
        assert manager._call(lambda:manager._page.get_by_label('Name').input_value())=='agent-user';assert json.loads(browser_snapshot(ctx))['title']=='Interactive test'
        page.reload();expect(page.locator('#layout')).to_be_visible();page.evaluate("switchSession('window-test')");page.wait_for_function('bootBuffer===null && !workspacePending');page.locator('#browser-chip').click();expect(page.locator('#browser-status')).to_contain_text('Agent 可操作')
        manager.close();assert json.loads(manager.open(url))['ok'];assert manager._call(lambda:manager._page.evaluate("localStorage.getItem('name')"))=='manual-user'
        page.set_viewport_size({'width':390,'height':844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"');page.wait_for_timeout(350);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2');assert not errors,errors
        page.screenshot(path='/tmp/coding-agent-interactive-browser.png');driver.close()
    print('PASS: real login, visible native window, shared direct/agent input, cross-thread safety, takeover/release, no screenshot gallery, reload, persistent storage, mobile')
finally:
    manager.close();server.shutdown();server.server_close();case.tearDown()
