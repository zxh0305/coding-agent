import sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import db
from services import databases as service
from tests.test_session_model import ApiTestBase
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.patch=patch.object(db,'DB_PATH',self.root/'app.db');self.patch.start();self.addCleanup(self.patch.stop)
        conn=sqlite3.connect(self.root/'sample.db');conn.execute('create table notes(id integer primary key, title text)');conn.executemany('insert into notes(title) values(?)',[('test',)]*220);conn.commit();conn.close()
        self.ident=service.save(1,str(self.root),dict(name='local',kind='sqlite',path='sample.db'))
    def test_query_bounded_and_structure(self):
        result=service.query(1,str(self.root),self.ident,'SELECT * FROM notes');self.assertEqual(len(result['rows']),200);self.assertTrue(result['limited']);self.assertIn('notes',str(service.tables(1,str(self.root),self.ident)))
    def test_writes_and_escape_denied(self):
        for sql in ['DELETE FROM notes','SELECT 1; DELETE FROM notes','WITH x AS (SELECT 1) DELETE FROM notes','SELECT load_extension(\'bad\')','PRAGMA writable_schema=ON','ATTACH DATABASE \'other.db\' AS other']:
            with self.assertRaises(ValueError):service.query(1,str(self.root),self.ident,sql)
        self.assertEqual(service.query(1,str(self.root),self.ident,'SELECT count(*) FROM notes')['rows'],[[220]])
    def test_owner_project_and_credentials(self):
        for uid,workspace in [(2,str(self.root)),(1,'other')]:
            with self.assertRaises(ValueError):service.query(uid,workspace,self.ident,'SELECT 1')
        ident=service.save(1,str(self.root),dict(name='mysql',kind='mysql',host='localhost',user='reader',database='demo',password='secret'))
        self.assertNotIn('secret',str(service.connections(1,str(self.root))));self.assertEqual(service._file().stat().st_mode & 0o777,0o600)
        service.remove(1,str(self.root),ident);self.assertEqual(len(service.connections(1,str(self.root))),1)
    def test_edit_mysql_preserves_password_without_returning_it(self):
        body=dict(name='mysql',kind='mysql',host='localhost',user='reader',database='demo',password='secret')
        ident=service.save(1,str(self.root),body)
        result=service.save(1,str(self.root),dict(body,id=ident,host='db.example',password=''))
        self.assertEqual(result,ident)
        self.assertEqual(service._get(1,str(self.root),ident)['password'],'secret')
        self.assertEqual(service._get(1,str(self.root),ident)['host'],'db.example')
        self.assertEqual(len(service.connections(1,str(self.root))),2)
        with self.assertRaises(ValueError):service.save(2,str(self.root),dict(body,id=ident))

    def test_catalog_and_table_details_are_separate_and_paginated(self):
        c=sqlite3.connect(self.root/'sample.db')
        for i in range(210):c.execute('CREATE TABLE "item_%03d" (value TEXT)'%i)
        c.commit();c.close()
        first=service.catalog(1,str(self.root),self.ident,search='item_')
        self.assertEqual(len(first['items']),100);self.assertTrue(first['has_more'])
        last=service.catalog(1,str(self.root),self.ident,page=2,search='item_')
        self.assertEqual(len(last['items']),10);self.assertFalse(last['has_more'])
        fields=service.table_schema(1,str(self.root),self.ident,'notes')
        self.assertEqual([r[0] for r in fields['rows']],['id','title'])
        rows=service.table_rows(1,str(self.root),self.ident,'notes',2)
        self.assertEqual(len(rows['rows']),20);self.assertFalse(rows['has_more'])
        with self.assertRaises(ValueError):service.table_schema(1,str(self.root),self.ident,'notes; DROP TABLE notes')

    def test_application_database_can_be_browsed_but_not_modified(self):
        db.init_db()
        ident=service.save(1,str(self.root),dict(name='Agent internal',kind='sqlite',path=str(db.DB_PATH)))
        self.assertTrue(service.test_connection(1,str(self.root),ident)['ok'])
        names=[item['name'] for item in service.catalog(1,str(self.root),ident)['items']]
        self.assertIn('sessions',names)
        self.assertIn('id',[r[0] for r in service.table_schema(1,str(self.root),ident,'sessions')['rows']])
        self.assertEqual(service.query(1,str(self.root),ident,'SELECT count(*) FROM sessions')['rows'],[[0]])
        with self.assertRaises(ValueError):service.query(1,str(self.root),ident,'DELETE FROM sessions')

    def test_missing_file_not_created(self):
        with self.assertRaises(ValueError):service.save(1,str(self.root),dict(name='bad',kind='sqlite',path='missing.db'))
        self.assertFalse((self.root/'missing.db').exists())
class DatabaseApiTests(ApiTestBase):
    def test_auth_and_scope(self):
        self.assertEqual(self.call('GET','/api/databases?session_id=foreign')[0],404)
        self.assertEqual(self.call('GET','/api/databases?session_id=s1',token=False)[0],401)
    def test_project_connections_need_no_session_or_messages(self):
        root=str(Path(self.tmp).resolve())
        with db._conn() as c:before=c.execute('SELECT count(*) FROM sessions').fetchone()[0]
        code,result=self.call('GET','/api/databases?workspace='+__import__('urllib.parse',fromlist=['quote']).quote(root))
        self.assertEqual(code,200);self.assertEqual(result['connections'],[])
        with db._conn() as c:self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],before)
        self.assertEqual(self.call('GET','/api/databases?workspace='+root,token=False)[0],401)

    def test_agent_and_cli_share_project_executor(self):
        import json,io,sys
        from types import SimpleNamespace
        from contextlib import redirect_stdout
        from database_tools import database_query,database_connections
        import database_cli
        root=Path(self.tmp);db.create_session('db-scope',self.uid,workspace=str(root))
        c=sqlite3.connect(root/'sample.db');c.execute('create table sample(value text)');c.execute("insert into sample values('hello')");c.commit();c.close()
        ident=service.save(self.uid,str(root),dict(name='test',kind='sqlite',path='sample.db'))
        ctx=SimpleNamespace(session_id='db-scope',workspace=root)
        self.assertTrue(json.loads(database_query(ident,'SELECT * FROM sample',ctx))['ok'])
        self.assertNotIn('path',database_connections(ctx))
        self.assertFalse(json.loads(database_query(ident,'DELETE FROM sample',ctx))['ok'])
        self.assertFalse(json.loads(database_query(ident,'SELECT 1',SimpleNamespace(session_id='db-scope',workspace='other')))['ok'])
        output=io.StringIO()
        with patch.object(sys,'argv',['database_cli','query','--owner',str(self.uid),'--workspace',str(root),'--connection',ident,'--sql','SELECT * FROM sample']),redirect_stdout(output):database_cli.main()
        self.assertEqual(json.loads(output.getvalue())['rows'],[['hello']])

    def test_global_workbench_without_session(self):
        status,data=self.call('GET','/api/databases?global=1')
        self.assertEqual(status,200);self.assertIn('connections',data)
        self.assertEqual(self.call('GET','/api/databases?global=1',token=False)[0],401)
