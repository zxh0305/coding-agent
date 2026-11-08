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
    def test_missing_file_not_created(self):
        with self.assertRaises(ValueError):service.save(1,str(self.root),dict(name='bad',kind='sqlite',path='missing.db'))
        self.assertFalse((self.root/'missing.db').exists())
class DatabaseApiTests(ApiTestBase):
    def test_auth_and_scope(self):
        self.assertEqual(self.call('GET','/api/databases?session_id=foreign')[0],404)
        self.assertEqual(self.call('GET','/api/databases?session_id=s1',token=False)[0],401)
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
