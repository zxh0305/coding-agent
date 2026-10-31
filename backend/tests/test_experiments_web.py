"""Management API tests: temporary files/database and mock model transport only."""
import copy
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest import mock
from http.server import ThreadingHTTPServer

import app
import db
import routes.experiments as routes
from experiments.web import Experiments, WebError, page, safe
from experiments.runner import run
from experiments.recording import Recorder, sha256
from tests.test_experiments import configuration, Transport, client_factory


def body():
    return dict(case='order_report', agents=[{k: m.get(k, 60) for k in
                ('id', 'provider_id', 'model', 'max_rounds', 'timeout')} for m in configuration()['agents']])


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manager = Experiments(root/'runs', root/'meta', model_check=lambda m: None)

    def history(self):
        with mock.patch('llm_client._http_post_json', side_effect=Transport()):
            result = run(configuration(), self.manager.root, client_factory=client_factory)
        return result['run_id']

    def test_history_requires_explicit_owner_and_never_changes_runs(self):
        rid = self.history()
        root = self.manager.root/rid
        before = {str(p): (sha256(p), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
        self.assertEqual(self.manager.listing(1), [])
        with self.assertRaises(WebError): self.manager.detail(rid, 1)
        self.manager.assign(rid, 1)
        detail = self.manager.detail(rid, 1, True)
        self.assertEqual(detail['status'], 'completed')
        self.assertTrue(detail['passed'])
        self.assertEqual(detail['node_models']['A'], ['model-data-returned'])
        self.assertEqual(len(detail['artifacts']), 5)
        self.assertEqual(self.manager.listing(2), [])
        with self.assertRaises(WebError): self.manager.detail(rid, 2)
        self.manager.logs(rid, 1, 'requests', 0, 2)
        self.assertEqual(before, {str(p): (sha256(p), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()})

    def test_whitelist_and_model_failure(self):
        for key in ('prompt_file', 'workspace', 'command', 'output_root', 'validator'):
            data=body();data['agents'][0][key]='anything'
            with self.assertRaises(WebError):self.manager.config(data)
        data=body();data['path']='../../'
        with self.assertRaises(WebError):self.manager.config(data)
        self.manager.model_check=lambda m: (_ for _ in ()).throw(ValueError('disabled'))
        with self.assertRaises(WebError):self.manager.create(body(),1)
        self.assertIsNone(self.manager.active)

    def test_start_is_async_one_run_and_stop_is_idempotent(self):
        entered, released = threading.Event(), threading.Event()
        def execute(config, root, *, run_id, cancel_event):
            entered.set();cancel_event.wait(3);released.set()
            return dict(nodes={'A':'stopped'}, evidence_complete=True)
        self.manager.executor=execute
        rid=self.manager.create(body(),1)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(WebError) as err:self.manager.create(body(),2)
        self.assertEqual(err.exception.status,409)
        with self.assertRaises(WebError):self.manager.stop(rid,2)
        self.assertEqual(self.manager.stop(rid,1)['status'],'stopping')
        self.assertTrue(released.wait(2))
        # Acquiring this lock also serializes with the worker's final metadata write.
        for _ in range(100):
            if self.manager.active is None:break
            threading.Event().wait(.01)
        self.assertEqual(self.manager.detail(rid,1)['status'],'stopped')
        self.assertEqual(self.manager.stop(rid,1)['status'],'stopped')

    def test_cancel_before_first_node_and_during_request(self):
        control=threading.Event();control.set()
        with mock.patch('llm_client._http_post_json') as post:
            result=run(configuration(),self.manager.root,client_factory=client_factory,cancel_event=control)
            post.assert_not_called()
        self.assertTrue(all(s=='stopped' for s in result['nodes'].values()))
        self.assertTrue(all(v['status']=='not_evaluated' for v in result['final'].values()))
        control=threading.Event(); transport=Transport()
        def stopping(*args):
            response=transport(*args);control.set();return response
        with mock.patch('llm_client._http_post_json',side_effect=stopping):
            result=run(configuration(),self.manager.root,client_factory=client_factory,cancel_event=control)
        self.assertTrue(all(s=='stopped' for s in result['nodes'].values()))
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['usage']),1)

    def test_restart_interrupts_without_rerunning_or_modifying_history(self):
        rid='a'*32
        self.manager.save_meta(rid,dict(owner_id=1,status='running',created_at='time',instance='old'))
        other=Experiments(self.manager.root,self.manager.metadata)
        self.assertEqual(other.detail(rid,1)['status'],'interrupted')
        self.assertEqual(other.listing(1)[0]['status'],'interrupted')
        self.assertFalse((self.manager.root/rid).exists())

    def test_cursor_and_incremental_aggregate_never_rescan(self):
        rid='a'*32;root=self.manager.root/rid;root.mkdir(parents=True)
        rec=Recorder(root,rid)
        self.manager.save_meta(rid,dict(owner_id=1,status='running'))
        for n in range(4):rec.event('node_state',dict(node_id='A'),status='running')
        first=self.manager.logs(rid,1,'events',0,2)
        second=self.manager.logs(rid,1,'events',first['cursor'],2)
        self.assertEqual(len(first['items']),2);self.assertEqual(len(second['items']),2)
        self.assertNotEqual(first['items'][0]['offset'],second['items'][0]['offset'])
        self.manager.detail(rid,1)
        import experiments.web as web
        original=web.page;seen=[]
        def spy(path,cursor,limit):seen.append((path.name,cursor));return original(path,cursor,limit)
        with mock.patch('experiments.web.page',side_effect=spy):self.manager.detail(rid,1)
        self.assertGreater(next(c for n,c in seen if n=='events.jsonl'),0)
        with self.assertRaises(WebError):page(root/'events.jsonl',1,2)
        with (root/'events.jsonl').open('ab') as f:f.write(b'{"partial":')
        p=page(root/'events.jsonl',second['cursor'],10)
        self.assertEqual(p['cursor'],second['cursor'])

    def test_corrupt_records_and_artifact_traversal(self):
        rid=self.history();self.manager.assign(rid,1)
        root=self.manager.root/rid
        for aid in ('../../.env','A/../../.env','A/missing'):
            with self.assertRaises(WebError):self.manager.artifact(rid,1,aid)
        path=self.manager.artifact(rid,1,'D/report.md');path.unlink();path.symlink_to(root/'case'/'rules.md')
        with self.assertRaises(WebError):self.manager.artifact(rid,1,'D/report.md')
        (root/'summary.json').write_text('broken')
        self.assertEqual(self.manager.listing(1)[0]['status'],'corrupt')
        with self.assertRaises(WebError):self.manager.detail(rid,1)

    def test_simultaneous_start_has_one_winner(self):
        gate = threading.Barrier(3)
        release = threading.Event()
        def execute(config, root, *, run_id, cancel_event):
            release.wait(3)
            return dict(nodes={'A': 'stopped'}, evidence_complete=True)
        self.manager.executor = execute
        winners, errors = [], []
        def create():
            gate.wait()
            try: winners.append(self.manager.create(body(), 1))
            except WebError as e: errors.append(e.status)
        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads: thread.start()
        gate.wait()
        for thread in threads: thread.join(2)
        self.assertEqual(len(winners), 1)
        self.assertEqual(errors, [409])
        self.manager.stop(winners[0], 1)
        release.set()
        for _ in range(100):
            if self.manager.active is None: break
            threading.Event().wait(.01)

    def test_nested_tool_json_and_connection_strings_are_redacted(self):
        raw = safe({'result': '{"api_key":"TOP_SECRET","input_tokens":12}',
                    'text': 'postgresql://user:pw@host/db Authorization: Basic SECRET'})
        self.assertNotIn('TOP_SECRET', json.dumps(raw))
        self.assertNotIn('user:pw', json.dumps(raw))
        self.assertNotIn('Basic SECRET', json.dumps(raw))
        self.assertEqual(json.loads(raw['result'])['input_tokens'], 12)

    def test_unknown_usage_and_redaction(self):
        with mock.patch('llm_client._http_post_json',side_effect=Transport(usage=False)):
            r=run(configuration(),self.manager.root,client_factory=client_factory)
        self.manager.assign(r['run_id'],1)
        d=self.manager.detail(r['run_id'],1)
        self.assertEqual(d['usage'][0]['tokens'],{})
        self.assertGreater(d['usage'][0]['unknown_usage_requests'],0)
        value=safe(dict(api_key='private',authorization='Bearer private',body='Bearer abc sk-1234567890 https://user:pw@host/private',prompt_tokens=12))
        self.assertNotIn('private',json.dumps(value));self.assertEqual(value['prompt_tokens'],12)


class APIExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.old=db.DB_PATH;db.DB_PATH=Path(self.temp.name)/'test.db';db.init_db()
        self.addCleanup(setattr,db,'DB_PATH',self.old)
        self.user=db.create_user('webtest','pass');self.token=db.create_token(self.user['id'])
        self.manager=Experiments(Path(self.temp.name)/'runs',Path(self.temp.name)/'meta',model_check=lambda m:None)
        self.patch=mock.patch.object(routes,'manager',self.manager);self.patch.start();self.addCleanup(self.patch.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.server.daemon_threads=True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def get(self,path,token=True,body=None):
        request=urllib.request.Request(self.base+path,headers={'Authorization':'Bearer '+self.token} if token else {},
                                       data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request) as r:return r.status,r.read(),r.headers
        except urllib.error.HTTPError as e:return e.code,e.read(),e.headers

    def test_auth_owner_controlled_preview_and_download(self):
        self.assertEqual(self.get('/api/experiments',False)[0],401)
        with mock.patch('llm_client._http_post_json',side_effect=Transport()):
            result=run(configuration(),self.manager.root,client_factory=client_factory)
        rid=result['run_id'];base='/api/experiments/'+rid
        self.assertEqual(self.get(base)[0],404)
        self.manager.assign(rid,self.user['id'])
        status,data,_=self.get('/api/experiments');self.assertEqual(status,200)
        self.assertNotIn(b'PRIVATE_TEST_KEY',data)
        self.assertEqual(self.get(base+'/artifact?id=D%2Freport.md')[0],200)
        status,data,headers=self.get(base+'/download?id=D%2Freport.md')
        self.assertEqual(status,200);self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(self.get(base+'/download?id=..%2F.env')[0],404)
        self.assertEqual(self.get(base+'/events?cursor=-1')[0],400)
        self.assertEqual(self.get('/api/experiments',body={'case':'order_report','path':'/tmp'})[0],400)
        self.assertEqual(self.get('/api/experiments/other')[0],404)


if __name__=='__main__':unittest.main()
