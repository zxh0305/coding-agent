"""Project-first database browsing without creating a session or sending a message."""
import sys,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect
case=ApiTestBase();case.setUp();case.set_global('p1','a-small');db.create_user('db-browser','browser-test-pass')
root=Path(case.tmp)/'project';root.mkdir()
c=sqlite3.connect(root/'demo.db');c.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY,title TEXT)');c.executemany('INSERT INTO notes(title) VALUES(?)',[(f'row {i}',) for i in range(220)])
for i in range(210):c.execute('CREATE TABLE "item_%03d"(value TEXT)'%i)
c.commit();c.close()
try:
    with db._conn() as c:before=c.execute('SELECT count(*) FROM sessions').fetchone()[0]
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1365,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(case.base);page.locator('#login-user').fill('db-browser');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible();page.locator('#new-task').click()
        page.locator('#database-chip').click();expect(page.locator('#db-project-guide')).to_be_visible();page.locator('#db-choose-project').click();page.evaluate('path=>navTo(path)',str(root));page.locator('#m-choose').click()
        expect(page.locator('.database-dialog')).to_be_visible();expect(page.locator('#db-project-guide')).to_be_hidden();assert page.evaluate('currentSession') is None
        form=page.locator('#db-connect');form.locator('[name=name]').fill('Demo');form.locator('[name=path]').fill('demo.db');form.locator('[type=submit]').click();expect(page.locator('#db-state')).to_contain_text('已保存')
        expect(page.locator('#db-table-list button')).to_have_count(100);page.locator('#db-table-next').click();expect(page.locator('#db-table-page')).to_contain_text('2');page.locator('#db-table-next').click();expect(page.locator('#db-table-list button')).to_have_count(11)
        page.locator('#db-search').fill('notes');expect(page.locator('#db-table-list button')).to_have_count(1);page.locator('#db-table-list button').click();expect(page.locator('#db-result')).to_contain_text('title');expect(page.locator('#db-breadcrumb')).to_contain_text('notes')
        page.locator('#db-data').click();expect(page.locator('#db-result')).to_contain_text('row 0');page.locator('#db-row-next').click();expect(page.locator('#db-result')).to_contain_text('row 100');page.locator('#db-row-next').click();expect(page.locator('#db-result')).to_contain_text('row 219');expect(page.locator('#db-row-next')).to_be_disabled()
        page.locator('.database-dialog').screenshot(path='/tmp/db-tree-desktop.png')
        # Hold an old data-page response while switching to schema; it must not overwrite fields.
        page.evaluate("""() => {window.__dbOriginalApi=api;api=async(path,options)=>{const data=await window.__dbOriginalApi(path,options);if(path==='/api/databases' && options && JSON.parse(options.body).action==='rows')await new Promise(resolve=>setTimeout(resolve,900));return data;};}""")
        page.locator('#db-row-prev').click();page.locator('#db-fields').click();expect(page.locator('#db-result-title')).to_have_text('字段结构');expect(page.locator('#db-result')).to_contain_text('title');page.wait_for_timeout(1100);expect(page.locator('#db-result-title')).to_have_text('字段结构');assert 'row 100' not in page.locator('#db-result').inner_text();page.evaluate('()=>{api=window.__dbOriginalApi;}')
        page.locator('#db-close').click();page.locator('#database-chip').click();expect(page.locator('#db-table-list button')).to_have_count(100)
        assert page.evaluate('currentSession') is None
        with db._conn() as c:assert c.execute('SELECT count(*) FROM sessions').fetchone()[0]==before
        page.set_viewport_size({'width':390,'height':844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"');page.locator('.database-dialog').screenshot(path='/tmp/db-tree-mobile.png');assert not errors,errors;browser.close()
    print('PASS: project picker, no message/session, saved connection reuse, table search/catalog paging, field/data tabs, data paging, mobile')
finally:case.tearDown()
