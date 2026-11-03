"""Skills ownership, revision pinning, persistence and HTTP contract."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import db
import skills
from tests.test_session_model import ApiTestBase


class SkillStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(db, 'DB_PATH', Path(self.tmp.name) / 'db.sqlite')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self, uid=1, body='检查边界条件'):
        return skills.save(uid, dict(id='review', name='代码审查', description='检查代码', body=body))

    def test_user_isolation_and_path_validation(self):
        self.save()
        self.assertEqual(skills.listing(2), [])
        for name in ('../review', '/tmp/x', '', 'a/b', '中文'):
            with self.assertRaises(ValueError): skills.path_for(1, name)
        with self.assertRaises(ValueError): skills.resolve(2, [{'id':'review','version':'x'}])

    def test_symlink_escape(self):
        skills.root(1).mkdir(parents=True)
        (skills.root(1) / 'escape').symlink_to(self.tmp.name, target_is_directory=True)
        with self.assertRaises(ValueError): skills.path_for(1, 'escape')

    def test_revision_and_snapshot(self):
        old = self.save()
        snapshot = skills.resolve(1, [old])
        self.save(body='新的指令')
        self.assertEqual(snapshot[0]['body'], '检查边界条件')
        with self.assertRaises(ValueError): skills.resolve(1, [old])
        self.assertIn('检查边界条件', skills.context(snapshot))
        self.assertEqual(skills.context([]), '')

    def test_limits(self):
        with self.assertRaises(ValueError): self.save(body='x' * 49000)
        with self.assertRaises(ValueError): skills.resolve(1, [None] * 7)
        with self.assertRaises(ValueError): skills.resolve(1, [None])

    def test_snapshot_survives_database_restore(self):
        db.init_db()
        uid = db.create_user('skill-test', 'password')['id']
        db.create_session('s1', uid)
        msg = {'role':'user','content':'检查这个项目','_skills':[self.save(uid)]}
        db.save_messages('s1', [msg], {})
        restored = db.get_messages('s1')
        self.assertEqual(restored[0]['_skills'], msg['_skills'])


class SkillApiTest(ApiTestBase):
    def test_crud_and_auth(self):
        self.assertEqual(self.call('GET', '/api/skills', token=False)[0], 401)
        data = dict(id='review',name='代码审查',body='检查边界条件')
        code, result = self.call('POST','/api/skills',data)
        self.assertEqual(code,200)
        self.assertEqual(result['skill']['body'],data['body'])
        self.assertEqual(len(self.call('GET','/api/skills')[1]['skills']),1)
        self.assertEqual(self.call('DELETE','/api/skills?id=review')[0],200)
        self.assertEqual(self.call('GET','/api/skills')[1]['skills'],[])

    def test_bad_selection_does_not_create_session(self):
        before = len(db.list_sessions(self.uid))
        code, _ = self.call('POST','/api/sessions',{'message':'test','skills':[{'id':'missing','version':'x'}]})
        self.assertEqual(code,400)
        self.assertEqual(len(db.list_sessions(self.uid)),before)

    def test_model_context_is_turn_scoped(self):
        from agent import Agent
        class FakeLLM:
            model = 'test'
            def chat_stream(self, **kwargs):
                yield 'text', 'done'
                yield 'done', {}
        a = Agent(FakeLLM(), workspace=Path(self.tmp))
        # Stop before streaming: inspect _run initialization and constructed system context.
        s = skills.save(self.uid, dict(id='review', body='SENTINEL_SKILL_INSTRUCTION'))
        it = a.run('one', {'role':'user','content':'one','_skills':[s]})
        next(it)
        self.assertIn('SENTINEL_SKILL_INSTRUCTION', a._system_content())
        it.close()
        it = a.run('two', {'role':'user','content':'two'})
        next(it)
        self.assertNotIn('SENTINEL_SKILL_INSTRUCTION', a._system_content())
        self.assertNotIn('_skills', str(a._messages_for_model()))
        it.close()
