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

    def test_slim_strips_body(self):
        full = self.save()
        slim = skills.slim([full])
        self.assertEqual(slim, [{"id": "review", "name": full["name"], "version": full["version"]}])
        self.assertEqual(skills.slim(None), [])
        self.assertEqual(skills.slim([]), [])

    def test_delete_removes_empty_dir(self):
        self.save()
        target = skills.path_for(1, 'review')
        self.assertTrue(target.exists())
        self.assertTrue(skills.delete(1, 'review'))
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())  # 空壳目录一并清掉
        self.assertFalse(skills.delete(1, 'review'))  # 幂等：再删仍成功

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
    def test_project_catalog_submission_and_conflicts(self):
        from routes import busref
        from threading import Event
        from urllib.parse import urlencode
        self.set_global("p1", "a-small")
        project = Path(self.tmp) / "a"
        other = Path(self.tmp) / "b"
        project.mkdir()
        other.mkdir()
        db.set_session_workspace("s1", str(project.resolve()))
        db.set_session_workspace("s2", str(other.resolve()))
        base = {"id": "review", "name": "代码审查", "expected_version": ""}
        personal = self.call("POST", "/api/skills", dict(base, body="通用"))[1]["skill"]
        def create(sid, body):
            code, result = self.call("POST", "/api/skills?session_id=" + sid,
                                     dict(base, source="project", body=body))
            self.assertEqual(code, 200, result)
            return result["skill"]
        a, b = create("s1", "审查 A"), create("s2", "审查 B")
        self.assertNotEqual(a["id"], b["id"])
        self.assertTrue((project / ".agents/skills/review/SKILL.md").is_file())
        for sid, skill in (("s1", a), ("s2", b)):
            catalog = self.call("GET", "/api/skills?summary=1&session_id=" + sid)[1]["skills"]
            self.assertEqual([s["id"] for s in catalog], [skill["id"], personal["id"]])
            self.assertTrue(all("body" not in s for s in catalog))
            self.assertEqual(self.call("GET", "/api/skills?" + urlencode(
                {"id": skill["id"], "session_id": sid}))[1]["skill"]["body"], skill["body"])
        self.assertEqual(self.call("POST", "/api/sessions/s2/messages",
                                   {"message": "go", "skills": [a]})[0], 400)
        event = Event()
        with patch.object(busref, "_run_round", side_effect=lambda *args: event.set()) as run:
            code, _ = self.call("POST", "/api/sessions", {"message": "go", "workspace": str(project),
                                                           "skills": [a, personal]})
            self.assertEqual(code, 200)
            self.assertTrue(event.wait(2))
            self.assertEqual([s["body"] for s in run.call_args.args[3]["_skills"]], ["审查 A", "通用"])
        self.assertEqual(self.call("POST", "/api/skills?session_id=s1",
                                   dict(base, id=a["id"], body="changed"))[0], 409)
        self.assertEqual(self.call("DELETE", "/api/skills?" + urlencode(
            {"session_id": "s2", "id": a["id"]}))[0], 400)

    def test_project_root_symlink_cannot_redirect_writes(self):
        project = Path(self.tmp) / "project"
        project.mkdir()
        (project / ".agents").symlink_to(self.tmp, target_is_directory=True)
        with self.assertRaises(ValueError):
            skills.save(self.uid, {"id": "test", "body": "x", "source": "project"}, project)
        self.assertFalse((Path(self.tmp) / "skills/test/SKILL.md").exists())

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

    def test_listing_is_slim_and_lazy_fetch_returns_bodies(self):
        """传输面瘦身契约：列表只带元数据；全文只走按 mid 懒取的通道。"""
        skill = self.call('POST','/api/skills',
                          dict(id='review',name='代码审查',body='检查边界条件'))[1]['skill']
        db.save_messages('s1', [{'role':'user','content':'看看这个','_mid':'m1',
                                 '_skills':[skill]}], {})
        _, page = self.call('GET','/api/sessions/s1/messages')
        row = next(m for m in page['messages'] if m.get('mid') == 'm1')
        self.assertEqual(row['skills'][0]['id'], 'review')
        self.assertNotIn('body', row['skills'][0])          # 列表不带正文
        code, r = self.call('GET','/api/skills?mid=m1&session_id=s1')
        self.assertEqual(code,200)
        self.assertEqual(r['skills'][0]['body'], '检查边界条件')  # 懒取带正文

    def test_lazy_fetch_ownership(self):
        """懒取是新增的读面：session 须属于当前用户，mid 须落在该会话名下。"""
        self.call('POST','/api/skills',dict(id='review',name='代码审查',body='检查边界条件'))
        db.save_messages('s1', [{'role':'user','content':'x','_mid':'m1',
                                 '_skills':[skills.read(self.uid,'review')]}], {})
        for query in ('mid=m1',                    # 缺 session_id
                      'mid=nope&session_id=s1',    # 消息不在该会话
                      'mid=m1&session_id=ghost'):  # 会话不存在
            code, _ = self.call('GET','/api/skills?'+query)
            self.assertEqual(code,400, query)
        real = self.token                          # 别人的会话：换他的 token 来读
        self.token = db.create_token(db.create_user('skill-other','pw')['id'])
        try:
            code, _ = self.call('GET','/api/skills?mid=m1&session_id=s1')
            self.assertEqual(code,400)
        finally:
            self.token = real

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
