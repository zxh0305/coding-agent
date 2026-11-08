"""Typed cells, safe JSON rendering, full value viewer and keyboard interaction."""
import sys,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect
case=ApiTestBase();case.setUp();case.set_global('p1','a-small');db.create_user('render-test','browser-test-pass');uid=db.get_user_by_name('render-test')['id'];root=Path(case.tmp)/'project';root.mkdir();db.create_session('render-db',uid,workspace=str(root))
payload='{"active": true, "retries": 3, "id": 900719925474099312345, "text": "<img src=x onerror=alert(1)>"}'
c=sqlite3.connect(root/'demo.db');c.execute('CREATE TABLE service_config(id INTEGER PRIMARY KEY,name TEXT,settings TEXT,note TEXT)');c.executemany('INSERT INTO service_config VALUES(?,?,?,?)',[(1,'开发环境',payload,None),(2,'测试环境','{"active": false, "retries": 0}',None)]);c.commit();c.close()
from services import databases
databases.save(uid,str(root),dict(name='开发数据库',kind='sqlite',path='demo.db'))
try:
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(case.base);page.locator('#login-user').fill('render-test');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible();page.evaluate("switchSession('render-db')");page.wait_for_function('bootBuffer===null && !workspacePending');page.locator('#database-chip').click();expect(page.locator('#db-table-list button')).to_have_count(1)
        page.locator('#db-table-list button').click();expect(page.locator('#db-result')).to_contain_text('settings');page.locator('#db-data').click();expect(page.locator('.db-json-preview')).to_have_count(2);expect(page.locator('.db-json-preview').first).to_contain_text('active');assert '个字段' not in page.locator('.db-json-preview').first.inner_text();expect(page.locator('.db-value-null')).to_have_count(2)
        page.locator('.database-dialog').screenshot(path='/tmp/db-polish-desktop.png')
        page.locator('.db-json-preview').first.dblclick();expect(page.locator('.db-cell-dialog')).to_be_visible();expect(page.locator('#db-cell-value')).to_contain_text('900719925474099312345');expect(page.locator('#db-cell-value img')).to_have_count(0);expect(page.locator('.db-json-key')).to_have_count(4)
        assert '\n' in page.locator('#db-cell-value').inner_text();page.context.grant_permissions(['clipboard-read','clipboard-write'],origin=case.base);page.locator('#db-cell-copy').click();expect(page.locator('#db-cell-copy')).to_contain_text('已复制');assert page.evaluate('navigator.clipboard.readText()')==payload;page.locator('.db-cell-dialog').screenshot(path='/tmp/db-polish-cell.png');page.locator('#db-cell-close').click()
        page.locator('.db-value-number').first.click();page.locator('#db-cell-open').click();expect(page.locator('#db-cell-title')).to_have_text('id');page.keyboard.press('Escape');expect(page.locator('.database-dialog')).to_be_visible()
        page.locator('.db-value-number').first.focus();page.keyboard.press('ArrowRight');page.keyboard.press('Enter');expect(page.locator('#db-cell-title')).to_have_text('name');page.keyboard.press('Escape')
        page.locator('#db-wrap').click();expect(page.locator('#db-wrap')).to_have_attribute('aria-pressed','true')
        page.locator('.db-query-section > summary').click();page.locator('#db-sql').fill('SELECT id, name, settings, note, '+', '.join('name AS long_column_name_'+str(i) for i in range(12))+' FROM service_config');page.locator('#db-run').click();expect(page.locator('#db-state')).to_contain_text('16 列')
        page.locator('#db-result').evaluate('node=>node.scrollLeft=400')
        assert page.evaluate("""() => {const grid=document.querySelector('#db-result');const row=grid.querySelector('tbody .db-row-number');const b=row.getBoundingClientRect();const g=grid.getBoundingClientRect();const hit=document.elementFromPoint(g.left+10,b.top+b.height/2);return grid.scrollLeft>0 && !!hit?.closest('.db-row-number') && getComputedStyle(row).backgroundColor!=='rgba(0, 0, 0, 0)';}""")
        page.set_viewport_size({'width':390,'height':844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"');page.locator('.database-dialog').screenshot(path='/tmp/db-polish-mobile.png');assert not errors,errors;browser.close()
    print('PASS: typed cells, NULL/JSON rendering, large JSON integer preservation, safe text rendering, full value viewer, keyboard, wrap and mobile')
finally:case.tearDown()
