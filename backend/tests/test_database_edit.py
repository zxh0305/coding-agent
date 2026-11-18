"""Cell editing: one-row UPDATE, literal preview, and the guards around it."""
import tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch,MagicMock
import db
from services import databases as config,database_runtime as runtime

class CellEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.patch=patch.object(db,'DB_PATH',self.root/'app.db');self.patch.start()
        sqlite3.connect(self.root/'demo.db').close()
        self.ident=config.save(1,str(self.root),dict(name='demo',kind='sqlite',path='demo.db'))
        runtime.execute(1,str(self.root),self.ident,"CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT, qty INTEGER NOT NULL, price REAL, meta TEXT)")
        runtime.execute(1,str(self.root),self.ident,"INSERT INTO items(id,name,qty,price) VALUES(1,'alpha',5,1.5)")
        self.root=str(self.root)
    def tearDown(self):runtime.forget(1,self.root,self.ident);self.patch.stop();self.tmp.cleanup()
    def rows(self):return config.query(1,self.root,self.ident,'SELECT id,name,qty,price FROM items')['rows']

    def test_preview_quotes_without_executing(self):
        result=config.update_preview(1,self.root,self.ident,'items','name',{'id':'1'},"O'Reilly & sons")
        self.assertEqual(result['sql'],"""UPDATE "items" SET "name" = 'O''Reilly & sons' WHERE "id" = 1""")
        self.assertEqual(result['new_value'],"O'Reilly & sons")
        self.assertEqual(self.rows(),[[1,'alpha',5,1.5]])

    def test_preview_keeps_column_types(self):
        self.assertIn('"qty" = 7 WHERE',config.update_preview(1,self.root,self.ident,'items','qty',{'id':'1'},'7')['sql'])
        self.assertIn('"price" = 2.25 WHERE',config.update_preview(1,self.root,self.ident,'items','price',{'id':'1'},'2.25')['sql'])
        self.assertIn('"name" = \'7\' WHERE',config.update_preview(1,self.root,self.ident,'items','name',{'id':'1'},'7')['sql'])
        self.assertIn('"meta" = NULL WHERE',config.update_preview(1,self.root,self.ident,'items','meta',{'id':'1'},None)['sql'])

    def test_preview_never_echoes_unquoted_injection(self):
        evil="x' OR '1'='1"
        sql=config.update_preview(1,self.root,self.ident,'items','name',{'id':'1'},evil)['sql']
        self.assertIn("'x'' OR ''1''=''1'",sql)
        runtime.update(1,self.root,self.ident,'items','name',{'id':'1'},evil)
        self.assertEqual(self.rows(),[[1,evil,5,1.5]])
        with self.assertRaisesRegex(ValueError,'没有匹配'):
            runtime.update(1,self.root,self.ident,'items','name',{'id':"2 OR 1=1"},'x')

    def test_update_changes_one_row_and_reports_statement(self):
        result=runtime.update(1,self.root,self.ident,'items','qty',{'id':'1'},'9')
        self.assertEqual(result['affected_rows'],1);self.assertIn('SET "qty" = 9',result['sql'])
        self.assertEqual(self.rows(),[[1,'alpha',9,1.5]])

    def test_missing_row_and_bad_value_leave_data_untouched(self):
        with self.assertRaisesRegex(ValueError,'没有匹配到这一行'):
            runtime.update(1,self.root,self.ident,'items','qty',{'id':'99'},'1')
        with self.assertRaisesRegex(ValueError,'不允许为空'):
            runtime.update(1,self.root,self.ident,'items','qty',{'id':'1'},None)
        self.assertEqual(self.rows(),[[1,'alpha',5,1.5]])

    def test_validation_rejects_unknown_columns_and_bad_input(self):
        for key,column,value in (({'nope':'1'},'name','x'),({'id':'1'},'nope','x'),({'id':'1'},'name',5),({},'name','x'),({'id':'1'},'name','x'*200001),({'x'*300:'1'},'name','x'),({'id':'1'},'n'*300,'x')):
            with self.subTest(key=key,column=column),self.assertRaises(ValueError):config.update_preview(1,self.root,self.ident,'items',column,key,value)
        with self.assertRaisesRegex(ValueError,'不属于当前项目'):
            config.update_preview(2,self.root,self.ident,'items','name',{'id':'1'},'x')

    def test_edit_identity_reports_primary_key_and_refuses_views(self):
        self.assertEqual(config.edit_identity(1,self.root,self.ident,'items'),{'editable':True,'primary':['id'],'unique':[],'reason':''})
        runtime.execute(1,self.root,self.ident,'CREATE VIEW item_names AS SELECT id,name FROM items')
        view=config.edit_identity(1,self.root,self.ident,'item_names')
        self.assertFalse(view['editable']);self.assertIn('唯一索引',view['reason'])

    def test_unique_index_is_used_without_primary_key(self):
        runtime.execute(1,self.root,self.ident,'CREATE TABLE codes(code TEXT NOT NULL UNIQUE, label TEXT)')
        runtime.execute(1,self.root,self.ident,"INSERT INTO codes VALUES('a','old')")
        info=config.edit_identity(1,self.root,self.ident,'codes')
        self.assertEqual(info['editable'],True);self.assertEqual(info['unique'],[['code']])
        runtime.update(1,self.root,self.ident,'codes','label',{'code':'a'},'new')
        self.assertEqual(config.query(1,self.root,self.ident,'SELECT label FROM codes')['rows'],[['new']])

    def test_null_key_values_only_match_null_rows(self):
        runtime.execute(1,self.root,self.ident,'CREATE TABLE tags(name TEXT, note TEXT)')
        runtime.execute(1,self.root,self.ident,"INSERT INTO tags VALUES('a','x'),(NULL,'x')")
        runtime.update(1,self.root,self.ident,'tags','note',{'name':None},'y')
        self.assertEqual(config.query(1,self.root,self.ident,'SELECT name,note FROM tags')['rows'],[['a','x'],[None,'y']])

    def test_recording_the_apps_own_database_is_refused(self):
        Path(self.root,'app.db').touch()
        own=config.save(1,self.root,dict(name='own',kind='sqlite',path=str(Path(self.root)/'app.db')))
        self.addCleanup(runtime.forget,1,self.root,own)
        with self.assertRaisesRegex(ValueError,'应用自身'):
            config.update_preview(1,self.root,own,'anything','anything',{'id':'1'},'x')
        self.assertFalse(config.edit_identity(1,self.root,own,'anything')['editable'])

    def test_quoted_identifiers_and_app_sqlite_reason(self):
        runtime.execute(1,self.root,self.ident,'CREATE TABLE "odd ""name"""("col one" TEXT)')
        runtime.execute(1,self.root,self.ident,'INSERT INTO "odd ""name""" VALUES(\'v\')')
        sql=config.update_preview(1,self.root,self.ident,'odd "name"','col one',{'col one':'v'},'w')['sql']
        self.assertIn('"odd ""name""" SET "col one" = \'w\'',sql)

class MySQLEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=str(Path(self.tmp.name).resolve())
        self.patch=patch.object(db,'DB_PATH',Path(self.root)/'app.db');self.patch.start()
        self.ident=config.save(1,self.root,dict(name='mysql',kind='mysql',host='127.0.0.1',port=3306,database='demo',user='u',password='p'))
    def tearDown(self):runtime.forget(1,self.root,self.ident);self.patch.stop();self.tmp.cleanup()
    def fake(self,rowcount=1,error=None):
        conn=MagicMock();cursor=conn.cursor.return_value;cursor.rowcount=rowcount
        conn.cursor.return_value.__enter__.return_value=cursor
        # Metadata lookups (information_schema) run before the UPDATE reaches the fake.
        def answer(sql,params=None):
            if 'information_schema.COLUMNS' in sql:cursor.fetchall.return_value=[('id','int(11)'),('name','varchar(20)'),('seen','datetime'),('amount','decimal(10,2)'),('flag','tinyint(1)')]
            elif error:raise error
        cursor.execute.side_effect=answer
        return conn,cursor
    def test_preview_uses_backticks_and_column_types(self):
        conn,_=self.fake()
        with patch.object(config,'_connect',return_value=conn):
            sql=config.update_preview(1,self.root,self.ident,'users','name',{'id':'10'},"a'b")['sql']
            self.assertEqual(sql,"UPDATE `users` SET `name` = 'a''b' WHERE `id` = 10")
            self.assertIn('`seen` = \'2026-01-02 03:04:05\'',config.update_preview(1,self.root,self.ident,'users','seen',{'id':'10'},'2026-01-02 03:04:05')['sql'])
            self.assertIn('`amount` = 12.50',config.update_preview(1,self.root,self.ident,'users','amount',{'id':'10'},'12.50')['sql'])
            self.assertIn('`flag` = 1',config.update_preview(1,self.root,self.ident,'users','flag',{'id':'10'},'1')['sql'])
            self.assertIn("`name` = 'NULL'",config.update_preview(1,self.root,self.ident,'users','name',{'id':'10'},'NULL')['sql'])
    def test_update_commits_once(self):
        conn,cursor=self.fake()
        with patch.object(config,'_connect',return_value=conn):
            result=runtime.update(1,self.root,self.ident,'users','name',{'id':'10'},'bob')
        self.assertEqual(result['affected_rows'],1);conn.commit.assert_called_once();conn.rollback.assert_not_called()
        self.assertIn('%s',cursor.execute.call_args_list[-1][0][0])
    def test_lost_connection_is_unconfirmed_and_not_retried(self):
        conn,cursor=self.fake(error=Exception(2013,'lost'))
        with patch.object(config,'_connect',return_value=conn):
            with self.assertRaisesRegex(ValueError,'未确认'):runtime.update(1,self.root,self.ident,'users','name',{'id':'10'},'bob')
        self.assertEqual(cursor.execute.call_count,2)  # metadata lookup + exactly one UPDATE
        conn.commit.assert_not_called()
    def test_zero_and_multi_row_updates_are_refused(self):
        for rowcount,message in ((0,'没有匹配到这一行'),(2,'匹配了多行')):
            conn,_=self.fake(rowcount=rowcount)
            with patch.object(config,'_connect',return_value=conn),self.assertRaisesRegex(ValueError,message):
                runtime.update(1,self.root,self.ident,'users','name',{'id':'10'},'bob')
            conn.commit.assert_not_called();conn.rollback.assert_called()
    def test_permission_error_is_explained(self):
        conn,_=self.fake(error=Exception(1142,'denied'))
        with patch.object(config,'_connect',return_value=conn),self.assertRaisesRegex(ValueError,'修改权限'):
            runtime.update(1,self.root,self.ident,'users','name',{'id':'10'},'bob')

class KeyTypeTests(CellEditTests):
    """A non-string cell value must never reach string concatenation."""
    def test_numeric_key_values_are_rejected(self):
        for raw in (1,1.5,True,['1']):
            with self.assertRaisesRegex(ValueError,'定位字段值无效'):
                config.update_preview(1,self.root,self.ident,'items','name',{'id':raw},'x')
