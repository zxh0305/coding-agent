import json
from pathlib import Path
from unittest.mock import patch

from services.final_answer import invalid_reply
from services.verification import VerificationPolicy
from tests.test_wrapup import WrapupTestBase,RecordingLLM,asst,call,tool_call_message

LEAK='<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="run_bash">unexecuted</｜｜DSML｜｜ invoke>'

class FinalAnswerRecoveryTests(WrapupTestBase):
    def test_raw_protocol_retries_once_without_executing_tools(self):
        model=RecordingLLM([asst(LEAK),asst('实际完成的工作和限制')]);agent=self.make_agent(model)
        with patch('agent.execute_tool',side_effect=AssertionError('must not execute')):
            events=list(agent.run('分析'))
        self.assertEqual(len(model.requests),2);self.assertIsNone(model.requests[-1]['tools'])
        self.assertEqual(events[-1][1]['completion_status'],'recovered')
        self.assertEqual(events[-1][1]['answer'],'实际完成的工作和限制')
        self.assertEqual(sum(kind=='done' for kind,_ in events),1)

    def test_split_protocol_tokens_do_not_flash_in_answer_stream(self):
        class Model:
            def __init__(self):self.calls=0
            def chat_stream(self,messages,tools=None,cancel=None):
                self.calls+=1
                if self.calls==1:
                    for text in ('<','｜｜DSML','｜｜ calls>',LEAK):yield 'delta',text
                    yield 'message',asst(LEAK)
                else:
                    yield 'delta','恢复后的正文';yield 'message',asst('恢复后的正文')
        events=list(self.make_agent(Model()).run('分析'))
        visible=''.join(p['delta'] for kind,p in events if kind=='answer_delta')
        self.assertEqual(visible,'恢复后的正文')

    def test_failed_retry_is_explicit_and_not_retried_forever(self):
        model=RecordingLLM([asst(LEAK),asst(LEAK)]);agent=self.make_agent(model)
        events=list(agent.run('分析'))
        self.assertEqual(len(model.requests),2)
        self.assertEqual(events[-1][1]['completion_status'],'invalid_final')
        self.assertEqual(events[-1][1]['stopped_reason'],'invalid_final_response')
        self.assertNotIn('DSML',events[-1][1]['answer'])
        self.assertEqual(agent.history[-1]['_stats']['completion_status'],'invalid_final')

    def test_recover_only_current_valid_candidate_with_limitation(self):
        model=RecordingLLM([asst('本轮草稿'),asst(LEAK),asst(LEAK)]);agent=self.make_agent(model)
        class Policy:
            sent=False
            def before_answer(self,*args,**kwargs):
                yield from ()
                if not self.sent:self.sent=True;return {'final':True,'feedback':'只总结，检查受阻'}
            def summary_text(self):return '自动验收记录：验证受阻'
        agent.completion_policy=Policy()
        events=list(agent.run('分析'))
        self.assertEqual(len(model.requests),3)
        answer=events[-1][1]['answer']
        self.assertIn('本轮草稿',answer);self.assertIn('尚未完成最终校验',answer);self.assertIn('验证受阻',answer)
        self.assertNotIn('DSML',answer);self.assertEqual(events[-1][1]['completion_status'],'recovered_draft')
        self.assertIsNone(model.requests[1]['tools']);self.assertIsNone(model.requests[2]['tools'])

    def test_recovery_connection_error_has_honest_fallback(self):
        agent=self.make_agent(RecordingLLM([asst(LEAK),RuntimeError('offline')]))
        events=list(agent.run('分析'));self.assertEqual(events[-1][1]['completion_status'],'invalid_final')

    def test_stopping_recovery_does_not_preserve_protocol_as_answer(self):
        class Model:
            def __init__(self):self.calls=0
            def chat_stream(self,messages,tools=None,cancel=None):
                self.calls+=1
                if self.calls==2:cancel.set()
                yield 'message',asst(LEAK)
        agent=self.make_agent(Model());events=list(agent.run('分析'))
        self.assertTrue(events[-1][1]['stopped'])
        self.assertNotIn('DSML',events[-1][1]['answer'])

    def test_fenced_protocol_documentation_remains_valid(self):
        answer='协议示例：\n```xml\n'+LEAK+'\n```\n以上只是示例。'
        self.assertFalse(invalid_reply(asst(answer)))
        self.assertTrue(invalid_reply(tool_call_message(call('x','run_bash',command='unused'))))

    def test_multi_project_parent_is_not_hashed_as_single_project(self):
        agent=self.make_agent(RecordingLLM([tool_call_message(call('read','run_bash',command='pwd')),asst('两个项目的分析结论')]))
        root=Path(agent.ctx.workspace)
        for name in ('first','second'):
            (root/name).mkdir();(root/name/'package.json').write_text('{}')
        policy=VerificationPolicy(root)
        with patch('snapshot.working_tree_fingerprint',side_effect=AssertionError('do not scan parent')):
            policy.start(agent)
        self.assertIn('多个独立项目',policy.baseline_error);agent.completion_policy=policy
        events=list(agent.run('分析项目关系'))
        self.assertEqual(len(agent.llm.requests),2)
        self.assertIn('两个项目的分析结论',events[-1][1]['answer']);self.assertIn('未执行跨项目自动验收',events[-1][1]['answer'])
        self.assertEqual(policy.assessment,'blocked')
