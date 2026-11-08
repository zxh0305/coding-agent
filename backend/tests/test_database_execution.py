import tempfile,unittest,sqlite3,json
from pathlib import Path
from unittest.mock import patch,MagicMock
import db
from services import databases as config,database_runtime as runtime,database_snippets as snippets
class DatabaseExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.patch=patch.object(db,'DB_PATH',self.root/'app.db');self.patch.start()
        sqlite3.connect(self.root/'demo.db').close();self.ident=config.save(1,str(self.root),dict(name='demo',kind='sqlite',path='demo.db'))
    def tearDown(self):runtime.forget(1,str(self.root),self.ident);self.patch.stop();self.tmp.cleanup()
    def execute(self,sql):return runtime.execute(1,str(self.root),self.ident,sql)
    def test_crud_and_ddl_commit(self):
        self.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY,title TEXT)')
        self.assertEqual(self.execute("INSERT INTO notes(title) VALUES('first')")['affected_rows'],1)
        self.execute("UPDATE notes SET title='updated'")
        self.assertEqual(self.execute('SELECT title FROM notes')['rows'],[['updated']])
        runtime.disconnect(1,str(self.root),self.ident)
        self.assertEqual(config.query(1,str(self.root),self.ident,'SELECT title FROM notes')['rows'],[['updated']])
        self.execute('ALTER TABLE notes ADD COLUMN flag INTEGER');self.execute('DELETE FROM notes');self.execute('DROP TABLE notes')
    def test_status_is_live_and_invalidated_on_edit(self):
        self.assertFalse(runtime.status(1,str(self.root),self.ident)['connected'])
        runtime.connect(1,str(self.root),self.ident);self.assertTrue(runtime.status(1,str(self.root),self.ident)['connected'])
        config.save(1,str(self.root),dict(id=self.ident,name='renamed',kind='sqlite',path='demo.db'))
        self.assertFalse(runtime.status(1,str(self.root),self.ident)['connected'])
        runtime.connect(1,str(self.root),self.ident);runtime.disconnect(1,str(self.root),self.ident)
        self.assertFalse(runtime.status(1,str(self.root),self.ident)['connected'])
        with self.assertRaises(ValueError):runtime.connect(2,str(self.root),self.ident)
    def test_snippet_persistence_edit_delete_and_scope(self):
        ident=snippets.save(1,str(self.root),dict(name='常用查询',sql='SELECT 1'))
        self.assertEqual(snippets.list_snippets(1,str(self.root))[0]['sql'],'SELECT 1')
        snippets.save(1,str(self.root),dict(snippet_id=ident,name='改名',sql='SELECT 2'))
        self.assertEqual(len(snippets.list_snippets(1,str(self.root))),1)
        self.assertEqual(snippets.list_snippets(2,str(self.root)),[])
        with self.assertRaises(ValueError):snippets.save(2,str(self.root),dict(snippet_id=ident,name='bad',sql='SELECT 3'))
        snippets.remove(2,str(self.root),ident);self.assertEqual(len(snippets.list_snippets(1,str(self.root))),1)
        snippets.remove(1,str(self.root),ident);self.assertEqual(snippets.list_snippets(1,str(self.root)),[])
    def test_unknown_write_result_is_not_automatically_retried(self):
        fake=MagicMock();cursor=fake.cursor.return_value;cursor.execute.side_effect=Exception(2013,'timeout')
        with patch.object(config,'_get',return_value={'kind':'mysql'}),patch.object(config,'_connect',return_value=fake):
            with self.assertRaisesRegex(ValueError,'未确认'):self.execute('UPDATE notes SET title=1')
        cursor.execute.assert_called_once()

    def test_failed_statement_does_not_commit_invalid_data(self):
        self.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY)');self.execute('INSERT INTO notes VALUES(1)')
        with self.assertRaises(ValueError):self.execute('INSERT INTO notes VALUES(1)')
        self.assertEqual(self.execute('SELECT count(*) FROM notes')['rows'],[[1]])

    def test_global_listing_rename_and_connection_sql_isolation(self):
        other=config.save(1,str(self.root),dict(name='second',kind='sqlite',path='demo.db'))
        self.addCleanup(runtime.forget,1,str(self.root),other)
        self.assertEqual(len(config.connections(1,None)),2)
        self.assertEqual(config.connections(2,None),[])
        runtime.connect(1,str(self.root),self.ident)
        config.rename(1,str(self.root),self.ident,'新名称')
        self.assertTrue(runtime.status(1,str(self.root),self.ident)['connected'])
        with self.assertRaises(ValueError):config.rename(2,str(self.root),self.ident,'bad')
        snippets.save(1,str(self.root),dict(name='旧 SQL',sql='SELECT 1'))
        first=snippets.list_snippets(1,str(self.root),self.ident)
        self.assertEqual(len(first),1)
        snippets.save(1,str(self.root),dict(snippet_id=first[0]['id'],name='新 SQL',sql='SELECT 2'),self.ident)
        self.assertEqual(snippets.list_snippets(1,str(self.root),other)[0]['sql'],'SELECT 1')
        self.assertEqual(snippets.list_snippets(1,str(self.root),self.ident)[0]['sql'],'SELECT 2')
        self.assertEqual(len(snippets.list_snippets(1,str(self.root),self.ident)),1)
