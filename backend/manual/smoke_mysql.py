"""Real isolated MySQL + application/Agent/CLI integration; never uses existing servers."""
import json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pymysql,db
from services import databases as service
from database_tools import database_query
from tests.test_session_model import ApiTestBase
case=ApiTestBase();case.setUp();process=None
try:
    root=Path(case.tmp);data=root/'mysql';data.mkdir();mysqld=shutil.which('mysqld')
    with (root/'mysql-init.log').open('wb') as log:
        subprocess.run([mysqld,'--no-defaults','--initialize-insecure','--datadir='+str(data)],stdout=log,stderr=log,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    with (root/'mysql.log').open('wb') as log:
        process=subprocess.Popen([mysqld,'--no-defaults','--datadir='+str(data),'--bind-address=127.0.0.1','--port='+str(port),'--socket='+str(root/'mysql.sock'),'--mysqlx=0','--innodb-buffer-pool-size=32M'],stdout=log,stderr=log)
    for _ in range(150):
        try:admin=pymysql.connect(host='127.0.0.1',port=port,user='root',autocommit=True);break
        except pymysql.Error:time.sleep(.2)
    else:raise RuntimeError('isolated MySQL failed to start')
    with admin.cursor() as cursor:
        cursor.execute('CREATE DATABASE demo');cursor.execute('CREATE TABLE demo.notes(id INT PRIMARY KEY,title VARCHAR(100), INDEX title_idx(title))');cursor.executemany('INSERT INTO demo.notes VALUES(%s,%s)',[(i,'hello') for i in range(220)])
        cursor.execute("CREATE USER 'reader'@'127.0.0.1' IDENTIFIED BY 'isolated-test-password'");cursor.execute("GRANT SELECT ON demo.* TO 'reader'@'127.0.0.1'")
    db.create_session('mysql-test',case.uid,workspace=str(root))
    body=dict(name='MySQL test',kind='mysql',host='127.0.0.1',port=port,user='reader',password='isolated-test-password',database='demo')
    code,reply=case.call('POST','/api/databases',dict(session_id='mysql-test',action='save',**body));assert code==200,reply;ident=reply['id']
    result=service.query(case.uid,str(root),ident,'SELECT * FROM notes ORDER BY id');assert len(result['rows'])==200 and result['limited']
    assert service.catalog(case.uid,str(root),ident)['items'][0]['name']=='notes'
    assert service.table_schema(case.uid,str(root),ident,'notes')['rows'][0][0]=='id'
    assert len(service.table_rows(case.uid,str(root),ident,'notes',2)['rows'])==20
    assert service.test_connection(case.uid,str(root),ident)['ok']
    assert 'notes' in str(service.query(case.uid,str(root),ident,'SHOW TABLES;'))
    assert 'title' in str(service.query(case.uid,str(root),ident,'DESCRIBE notes'))
    assert 'title_idx' in str(service.query(case.uid,str(root),ident,'SHOW INDEX FROM notes'))
    assert 'title' in str(service.tables(case.uid,str(root),ident))
    for sql in ['DELETE FROM notes','WITH x AS (SELECT 1) DELETE FROM notes','SELECT 1; DELETE FROM notes',"SELECT * INTO OUTFILE '/tmp/unwanted-db-test' FROM notes"]:
        try:service.query(case.uid,str(root),ident,sql);raise AssertionError('write accepted')
        except ValueError:pass
    assert json.loads(database_query(ident,'SELECT count(*) FROM notes',SimpleNamespace(session_id='mysql-test',workspace=str(root))))['result']['rows']==[[220]]
    code,result=case.call('POST','/api/databases',dict(session_id='mysql-test',action='query',id=ident,sql='SELECT count(*) FROM notes'));assert code==200 and result['rows']==[[220]]
    assert 'isolated-test-password' not in json.dumps(case.call('GET','/api/databases?session_id=mysql-test')[1])
    # CLI uses the same temporary registry by redirecting db.DB_PATH in-process.
    import database_cli,io
    from unittest.mock import patch
    from contextlib import redirect_stdout
    output=io.StringIO()
    with patch.object(sys,'argv',['database_cli','query','--owner',str(case.uid),'--workspace',str(root),'--connection',ident,'--sql','SELECT count(*) FROM notes']),redirect_stdout(output):database_cli.main()
    assert json.loads(output.getvalue())['rows']==[[220]]
    with admin.cursor() as cursor:cursor.execute('SELECT VERSION()');version=cursor.fetchone()[0]
    from services import database_runtime as runtime
    with admin.cursor() as cursor:
        cursor.execute("CREATE USER 'writer'@'127.0.0.1' IDENTIFIED BY 'isolated-writer-password'");cursor.execute("GRANT ALL ON demo.* TO 'writer'@'127.0.0.1'")
    writer=service.save(case.uid,str(root),dict(body,name='Writer',user='writer',password='isolated-writer-password'))
    runtime.execute(case.uid,str(root),writer,'CREATE TABLE write_test(id INT PRIMARY KEY,title VARCHAR(100))')
    assert runtime.execute(case.uid,str(root),writer,"INSERT INTO write_test VALUES(1,'test')")['affected_rows']==1
    runtime.execute(case.uid,str(root),writer,"UPDATE write_test SET title='changed'")
    assert runtime.execute(case.uid,str(root),writer,'SELECT title FROM write_test')['rows']==[['changed']]
    runtime.execute(case.uid,str(root),writer,'DELETE FROM write_test');runtime.execute(case.uid,str(root),writer,'DROP TABLE write_test')
    try:runtime.execute(case.uid,str(root),ident,"UPDATE notes SET title='forbidden'");raise AssertionError('readonly account accepted write')
    except ValueError as error:assert '权限' in str(error),error
    limited=runtime.execute(case.uid,str(root),ident,'SELECT * FROM notes ORDER BY id');assert len(limited['rows'])==200 and limited['limited'] and limited['connected']
    assert runtime.execute(case.uid,str(root),ident,'SELECT count(*) FROM notes')['rows']==[[220]]
    assert runtime.status(case.uid,str(root),writer)['connected'];runtime.disconnect(case.uid,str(root),writer);assert not runtime.status(case.uid,str(root),writer)['connected']
    from playwright.sync_api import sync_playwright,expect
    db.create_user('mysql-ui','browser-test-pass');ui_uid=db.get_user_by_name('mysql-ui')['id'];db.create_session('mysql-ui',ui_uid,workspace=str(root))
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(case.base);page.locator('#login-user').fill('mysql-ui');page.locator('#login-pass').fill('browser-test-pass');page.locator('#login-submit').click();expect(page.locator('#layout')).to_be_visible();page.evaluate("switchSession('mysql-ui')");page.wait_for_function('bootBuffer===null && !workspacePending');page.locator('#database-chip').click()
        form=page.locator('#db-connect');form.locator('[name=kind]').select_option('mysql')
        for key in ('name','host','port','user','password','database'):form.locator('[name='+key+']').fill(str(body[key]))
        form.locator('button').first.click();expect(page.locator('#db-state')).to_contain_text('已保存');page.locator('#db-test').click();expect(page.locator('#db-state')).to_contain_text('连接成功')
        page.locator('.db-query-section > summary').click();page.locator('#db-sql').fill('SELECT count(*) FROM notes');page.locator('#db-run').click();expect(page.locator('#db-result')).to_contain_text('220');assert not errors,errors;browser.close()
    admin.close();print('PASS: real MySQL '+version+'; API/Agent/CLI query, structure, row limit, write rejection and credential masking')
finally:
    if process:
        process.terminate()
        try:process.wait(20)
        except subprocess.TimeoutExpired:process.kill();process.wait()
    case.tearDown()
