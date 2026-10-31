"""Harness regression tests use real Agent and protocol client, never a live API."""
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from experiments.agents import ExperimentAgent, resolve_client
from experiments.cases import order_report as case
from experiments.recording import Recorder, RequestObserver, EvidenceError, usage_summary
from experiments.runner import run, validate_plan
from llm_client import OpenAIChatClient, AnthropicMessagesClient, ApiHTTPError


def configuration():
    return {'agents': [dict(id=mid, provider_id='test-provider', model=model,
                           prompt_file=f'prompts/{mid}.md',
                           allowed_tools=['read_file', 'write_file', 'list_dir'], max_rounds=4)
                       for mid, model in [('data_worker', 'model-data'), ('report_worker', 'model-report')]]}


class Response(io.BytesIO):
    headers = {'x-request-id': 'provider-request'}


class Transport:
    def __init__(self, fail_node=None, bad_node=None, usage=True, missing=False):
        self.fail_node, self.bad_node, self.usage, self.missing = fail_node, bad_node, usage, missing
        self.payloads = []
        self.oracle = case.expected()

    def __call__(self, url, headers, payload, timeout):
        self.payloads.append(json.loads(json.dumps(payload)))
        user = next(m['content'] for m in payload['messages'] if m['role'] == 'user')
        nid = user.split('节点 ')[1][0]
        if nid == self.fail_node:
            raise ApiHTTPError(400, 'private_error_body_should_not_be_exported')
        if any(m.get('role') == 'tool' for m in payload['messages']):
            message = {'content': '完成', 'finish_reason': 'stop'}
        else:
            task = next(t for t in case.TASKS if t['id'] == nid)
            calls = [dict(index=i, id=f'read-{nid}-{i}', type='function',
                          function=dict(name='read_file', arguments=json.dumps(dict(path='inputs/' + name))))
                     for i, name in enumerate(task['inputs'])]
            for i, name in enumerate(task['outputs']):
                value = self.oracle[name]
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                if nid == self.bad_node:
                    text = 'broken'
                if self.missing:
                    continue
                calls.append(dict(index=len(calls), id=f'call-{nid}-{i}', type='function',
                                  function=dict(name='write_file', arguments=json.dumps(dict(path=name, content=text)))))
            message = {'tool_calls': calls, 'finish_reason': 'tool_calls'}
        finish = message.pop('finish_reason')
        chunks = [dict(model=payload['model'] + '-returned', choices=[dict(delta=message, finish_reason=finish)])]
        if self.usage:
            chunks.append(dict(usage=dict(prompt_tokens=7, completion_tokens=3,
                                          prompt_tokens_details={'cached_tokens': 2}), choices=[]))
        raw = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
        return Response(raw.encode())


def client_factory(member, observer):
    client = OpenAIChatClient('PRIVATE_TEST_KEY', 'https://invalid.example', member['model'])
    client.request_observer = observer
    return client


class ExperimentsTests(unittest.TestCase):
    def execute(self, transport, root):
        with mock.patch('llm_client._http_post_json', side_effect=transport):
            return run(configuration(), root, client_factory=client_factory)

    def test_complete_graph_usage_model_and_full_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            transport = Transport()
            result = self.execute(transport, temp)
            self.assertTrue(result['passed'], result)
            self.assertEqual(result['execution_backend'], 'injected_client_factory')
            self.assertEqual(list(result['nodes'].values()), ['validated'] * 4)
            self.assertEqual({g['model'] for g in result['usage']}, {'model-data', 'model-report'})
            self.assertEqual(sum(g['tokens']['prompt_tokens'] for g in result['usage']), 56)
            root = Path(result['directory'])
            events = [json.loads(x) for x in (root / 'events.jsonl').read_text().splitlines()]
            positions = {(e.get('node_id'), e.get('status')): i for i, e in enumerate(events)
                         if e['event_type'] == 'node_state'}
            self.assertLess(positions['A', 'validated'], positions['B', 'running'])
            self.assertLess(positions['B', 'validated'], positions['D', 'running'])
            self.assertLess(positions['C', 'validated'], positions['D', 'running'])
            calls = [e for e in events if e['event_type'] == 'tool_request']
            results = [e for e in events if e['event_type'] == 'tool_response']
            self.assertCountEqual([e['tool_call_id'] for e in calls], [e['tool_call_id'] for e in results])
            self.assertTrue(all(e['request_id'] for e in calls))
            requests = [json.loads(x) for x in (root / 'requests.jsonl').read_text().splitlines()]
            self.assertEqual(len([r for r in requests if r['event_type'] == 'request_end']), 8)
            self.assertTrue(all(r['returned_model'].endswith('-returned') for r in requests
                                if r['event_type'] == 'request_end'))
            evidence = '\n'.join(p.read_text() for p in root.glob('*.json*'))
            self.assertNotIn('PRIVATE_TEST_KEY', evidence)
            self.assertEqual(len([m for m in transport.payloads[2]['messages'] if m['role'] == 'user']), 1)
            artifacts = json.loads((root / 'artifacts.json').read_text())
            self.assertEqual(artifacts['B/sales.json']['inputs'][0]['sha256'], artifacts['A/cleaned.csv']['sha256'])

    def test_validation_failure_blocks_descendants_and_unknown_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.execute(Transport(bad_node='A', usage=False), temp)
            self.assertFalse(result['passed'])
            self.assertEqual(result['nodes'], dict(A='validation_failed', B='blocked', C='blocked', D='blocked'))
            self.assertEqual(result['usage'][0]['unknown_usage_requests'], 2)
            self.assertEqual(result['usage'][0]['tokens'], {})

    def test_execution_failure_and_missing_files_never_succeed(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.execute(Transport(fail_node='B'), temp)
            self.assertEqual(result['nodes']['B'], 'execution_failed')
            self.assertEqual(result['nodes']['C'], 'validated')
            self.assertEqual(result['nodes']['D'], 'blocked')
            result = self.execute(Transport(missing=True), temp)
            self.assertEqual(result['nodes']['A'], 'validation_failed')

    def test_run_history_and_files_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            one = self.execute(Transport(), temp)
            two = self.execute(Transport(), temp)
            self.assertNotEqual(one['directory'], two['directory'])
            self.assertEqual(one['usage'], two['usage'])
            self.assertFalse(list(Path(temp).rglob('.agent-memory')))

    def test_file_boundary_is_enforced_before_tool_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / 'node'
            work.mkdir()
            source = work / 'inputs' / 'source.txt'
            source.parent.mkdir()
            source.write_text('immutable')
            rec = Recorder(root, 'test')
            agent = ExperimentAgent(llm=object(), workspace=work, system_prompt='test',
                                    allowed_tools=('read_file', 'write_file', 'list_dir'),
                                    recorder=rec, context={}, readable=[source], writable=[work / 'out.txt'])
            agent.pre_tool_hooks.append(lambda *args: (_ for _ in ()).throw(RuntimeError()))
            def call(name, path):
                return json.loads(agent._run_tool(dict(id='call', function=dict(name=name,
                    arguments=json.dumps(dict(path=path, content='changed'))))))
            self.assertFalse(call('write_file', 'inputs/source.txt')['ok'])
            self.assertFalse(call('write_file', '../other.txt')['ok'])
            self.assertFalse(call('run_bash', 'out.txt')['ok'])
            (work / 'link').symlink_to(root, target_is_directory=True)
            self.assertFalse(call('write_file', 'link/other.txt')['ok'])
            self.assertEqual(source.read_text(), 'immutable')
            self.assertEqual(agent._memory_blocks(), '')

    def test_strict_model_lookup_never_falls_back_or_creates_database(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.db'
            with self.assertRaises(sqlite3.OperationalError):
                resolve_client(configuration()['agents'][0], None, path)
            self.assertFalse(path.exists())
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE providers(id TEXT, enabled INTEGER, api_key TEXT, api_format TEXT, base_url TEXT)')
                conn.execute('CREATE TABLE provider_models(provider_id TEXT, name TEXT, enabled INTEGER)')
                conn.execute("INSERT INTO providers VALUES('test-provider',1,'secret','openai','https://invalid')")
                conn.execute("INSERT INTO provider_models VALUES('test-provider','different-model',1)")
            with self.assertRaises(ValueError):
                resolve_client(configuration()['agents'][0], None, path)

    def test_preflight_rejects_cycles_invalid_tools_and_unregistered_inputs(self):
        configs = configuration()['agents']
        tasks = json.loads(json.dumps(case.TASKS))
        tasks[0]['depends_on'] = ['D']
        with self.assertRaises(ValueError):
            validate_plan(tasks, configs)
        configs[0]['allowed_tools'].append('run_bash')
        with self.assertRaises(ValueError):
            validate_plan(case.TASKS, configs)

    def test_transport_retry_gets_separate_identity_and_unknown_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = Recorder(Path(temp), 'retry')
            obs = RequestObserver(rec, dict(agent_id='a', provider='p', model='m'))
            client = OpenAIChatClient('secret', 'https://invalid', 'm')
            client.request_observer = obs
            raw = b'data: {"model":"actual","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            with mock.patch('llm_client._sleep_cancellable', return_value=True), mock.patch(
                    'llm_client._http_post_json', side_effect=[ApiHTTPError(503, 'secret'), Response(raw)]):
                list(client.chat_stream([{'role': 'user', 'content': 'hello'}]))
            self.assertEqual(len(rec.requests), 2)
            self.assertNotEqual(rec.requests[0]['request_id'], rec.requests[1]['request_id'])
            self.assertEqual(rec.requests[0]['status'], 'error')
            self.assertEqual(usage_summary(rec.requests)[0]['unknown_usage_requests'], 2)

    def test_anthropic_raw_usage_and_returned_model_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = Recorder(Path(temp), 'anthropic')
            obs = RequestObserver(rec, dict(agent_id='a', provider='p', model='configured'))
            client = AnthropicMessagesClient('secret', 'https://invalid', 'configured')
            client.request_observer = obs
            data = [dict(type='message_start', message=dict(id='msg-1', model='actual',
                         usage=dict(input_tokens=11, output_tokens=0, cache_read_input_tokens=4))),
                    dict(type='content_block_start', index=0, content_block=dict(type='text', text='')),
                    dict(type='content_block_delta', index=0, delta=dict(type='text_delta', text='ok')),
                    dict(type='message_delta', delta=dict(stop_reason='end_turn'), usage=dict(output_tokens=3)),
                    dict(type='message_stop')]
            response = Response(''.join('data: ' + json.dumps(v) + '\n\n' for v in data).encode())
            with mock.patch('llm_client._http_post_json', return_value=response):
                list(client.chat_stream([dict(role='user', content='hello')]))
            self.assertEqual(rec.requests[0]['returned_model'], 'actual')
            self.assertEqual(rec.requests[0]['status'], 'completed')
            self.assertEqual(usage_summary(rec.requests)[0]['tokens'],
                             dict(input_tokens=11, output_tokens=3, cache_read_input_tokens=4))

    def test_observer_failure_never_sends_request(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = Recorder(Path(temp), 'failure')
            obs = RequestObserver(rec, dict(agent_id='a', provider='p', model='m'))
            client = OpenAIChatClient('secret', 'https://invalid', 'm')
            client.request_observer = obs
            rec.incomplete = True
            with mock.patch('llm_client._http_post_json') as post:
                with self.assertRaises(EvidenceError):
                    list(client.chat_stream([dict(role='user', content='hello')]))
                post.assert_not_called()

    def test_full_events_are_not_ui_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = Recorder(Path(temp), 'long')
            for i in range(180):
                rec.event('test', text='x' * 5000, sequence=i)
            rows = (Path(temp) / 'events.jsonl').read_text().splitlines()
            self.assertEqual(len(rows), 180)
            self.assertEqual(len(json.loads(rows[-1])['text']), 5000)

    def test_record_failure_marks_run_incomplete(self):
        original = Recorder.event
        def faulty(self, kind, *args, **kwargs):
            if kind == 'runtime_validation':
                self.incomplete = True
                raise EvidenceError('disk failure')
            return original(self, kind, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Recorder, 'event', faulty):
            result = self.execute(Transport(), temp)
            self.assertFalse(result['evidence_complete'])
            self.assertFalse(result['passed'])
            self.assertEqual(result['nodes']['B'], 'stopped')

    def test_final_answers_are_not_runtime_feedback(self):
        with tempfile.TemporaryDirectory() as temp:
            transport = Transport()
            result = self.execute(transport, temp)
            self.assertTrue(result['passed'])
            for payload in transport.payloads:
                self.assertNotIn('独立计算结果', json.dumps(payload, ensure_ascii=False))
            # A structurally valid but wrong total must fail host scoring.
            work = Path(result['directory']) / 'nodes'
            (work / 'B' / 'sales.json').write_text('{"channels":[],"total_count":0,"total_amount":"0.00"}')
            scored = case.final_validate({n: work / n for n in 'ABCD'})
            self.assertFalse(scored['B']['passed'])


if __name__ == '__main__':
    unittest.main()
