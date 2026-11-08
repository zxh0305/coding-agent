"""Saved SQL survives reload/login; live multi-connection controls and SQL writes."""
import sys,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import db
from services import databases,database_runtime
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright,expect
case=ApiTestBase();case.setUp();case.set_global('p1','a-small');db.create_user('sql-workspace','browser-test-pass');uid=db.get_user_by_name('sql-workspace')['id'];root=Path(case.tmp)/'project';root.mkdir();db.create_session('sql-workspace',uid,workspace=str(root))
for name in ('first','second'):
    c=sqlite3.connect(root/(name+'.db'));c.execute('CREATE TABLE notes(title TEXT)');c.execute('INSERT INTO notes VALUES(?)',(name,));c.commit();c.close();databases.save(uid,str(root),dict(name=name,kind='sqlite',path=name+'.db'))
try:
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(case.base);page.locator('#login-user').fill('sql-workspace');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible();page.locator('#database-chip').click();expect(page.locator('.db-source-row')).to_have_count(2)
        expect(page.locator('.db-source-row').first.locator('.db-status-dot')).to_have_attribute('data-connected','true');expect(page.locator('.db-source-row').nth(1).locator('.db-status-dot')).to_have_attribute('data-connected','false')
        page.locator('.db-source-row').nth(1).locator('.db-source-power').click();expect(page.locator('.db-source-row').nth(1).locator('.db-status-dot')).to_have_attribute('data-connected','true')
        page.locator('.db-source-row').first.locator('.db-source-power').click();expect(page.locator('.db-source-row').first.locator('.db-status-dot')).to_have_attribute('data-connected','false');page.locator('.db-source-row').first.locator('.db-source-power').click();expect(page.locator('#db-table-list button')).to_have_count(1)
        page.locator('.db-source-row').nth(1).locator('.db-source-select').click();expect(page.locator('#db-connection-meta')).to_contain_text('second.db');expect(page.locator('#db-table-list button')).to_have_count(1)
        page.locator('#db-rename').click();page.locator('.db-rename-dialog input').fill('重命名连接');page.locator('.db-rename-dialog [type=submit]').click();expect(page.locator('.db-source-row').nth(1)).to_contain_text('重命名连接');page.locator('#db-test').click();expect(page.locator('#db-state')).to_contain_text('测试成功');page.locator('.db-query-section > summary').click();page.locator('#db-sql').fill('SELECT * FROM notes');page.locator('#db-snippet-name').fill('常用查询');page.locator('#db-snippet-save').click();expect(page.locator('#db-state')).to_contain_text('已保存')
        page.reload();expect(page.locator('#layout')).to_be_visible();page.locator('#database-chip').click();page.locator('.db-query-section > summary').click();expect(page.locator('#db-snippet-list')).to_contain_text('常用查询');expect(page.locator('#db-connection-meta')).to_contain_text('second.db');expect(page.locator('#db-sql')).to_have_value('SELECT * FROM notes')
        page.locator('#db-snippet-name').fill('改后查询');page.locator('#db-sql').fill('SELECT count(*) FROM notes');page.locator('#db-snippet-save').click();expect(page.locator('#db-state')).to_contain_text('已保存');expect(page.locator('#db-snippet-list option')).to_have_count(2)
        page.locator('#db-snippet-new').click();page.locator('#db-snippet-name').fill('新增记录');page.locator('#db-sql').fill("INSERT INTO notes VALUES('new')");page.locator('#db-snippet-save').click();expect(page.locator('#db-state')).to_contain_text('已保存')
        page.locator('#db-run').click();expect(page.locator('#db-state')).to_contain_text('影响 1 行')
        for sql in ("UPDATE notes SET title='changed' WHERE title='new'","DELETE FROM notes WHERE title='changed'",'CREATE TABLE extra(id INTEGER)','ALTER TABLE extra ADD COLUMN title TEXT','DROP TABLE extra'):
            page.locator('#db-sql').fill(sql);page.locator('#db-run').click();expect(page.locator('#db-state')).to_contain_text('执行成功')
        page.locator('#db-sql').fill('SELECT 1;\nSELECT 2');page.locator('#db-sql').evaluate('node=>node.setSelectionRange(10,node.value.length)');page.locator('#db-run').click();expect(page.locator('#db-result tbody')).to_contain_text('2');
        page.locator('#db-snippet-delete').click();expect(page.locator('#db-state')).to_contain_text('已删除');expect(page.locator('#db-snippet-list option')).to_have_count(2)
        fresh_context=browser.new_context();fresh=fresh_context.new_page();fresh.goto(case.base);fresh.locator('#login-user').fill('sql-workspace');fresh.locator('#login-pass').fill('browser-test-pass');fresh.locator('#login-submit').click();expect(fresh.locator('#layout')).to_be_visible();fresh.evaluate("switchSession('sql-workspace')");fresh.wait_for_function('bootBuffer===null && !workspacePending');fresh.locator('#database-chip').click();fresh.locator('.db-source-row').nth(1).locator('.db-source-select').click();fresh.locator('.db-query-section > summary').click();expect(fresh.locator('#db-snippet-list')).to_contain_text('改后查询');fresh.locator('#db-snippet-list').select_option(label='改后查询');expect(fresh.locator('#db-sql')).to_have_value('SELECT count(*) FROM notes');fresh_context.close()
        page.locator('.database-dialog').screenshot(path='/tmp/database-saved-sql.png');page.set_viewport_size({'width':390,'height':844});page.wait_for_function('document.documentElement.dataset.mode==="mobile"');assert page.locator('.database-dialog').bounding_box()['width']<=390;assert not errors,errors;browser.close()
    print('PASS: multi connection green/gray toggles, reload-persistent snippet CRUD/draft, explicit SQL CRUD/DDL commit and mobile')
finally:database_runtime.close_all();case.tearDown()
