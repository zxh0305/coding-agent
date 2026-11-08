import sys,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect
case=ApiTestBase();case.setUp();case.set_global('p1','a-small');db.create_user('database-tester','browser-test-pass');uid=db.get_user_by_name('database-tester')['id'];root=Path(case.tmp)/'project';root.mkdir();db.create_session('db-test',uid,workspace=str(root))
c=sqlite3.connect(root/'demo.db');c.execute('create table notes(id integer primary key,title text)');c.execute("insert into notes(title) values('hello')");c.commit();c.close()
try:
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1365,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(case.base);page.locator('#login-user').fill('database-tester');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible();page.evaluate("switchSession('db-test')");page.wait_for_function('bootBuffer===null && !workspacePending')
        page.locator('#database-chip').click();page.locator('#db-connect [name=name]').fill('local');page.locator('#db-connect [name=path]').fill('demo.db');page.locator('#db-connect button').click();expect(page.locator('#db-state')).to_contain_text('已保存')
        page.locator('#db-tables').click();expect(page.locator('#db-result')).to_contain_text('notes');page.locator('#db-sql').fill('SELECT * FROM notes');page.locator('#db-run').click();expect(page.locator('#db-result')).to_contain_text('hello')
        page.locator('#db-sql').fill('DELETE FROM notes');page.locator('#db-run').click();expect(page.locator('#db-state')).to_contain_text('失败')
        page.set_viewport_size({'width':390,'height':844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"');assert page.locator('.database-dialog').bounding_box()['width']<=390;assert not errors,errors
        browser.close()
    print('PASS: login, project connection, structure, query, rejected write, mobile')
finally:case.tearDown()
