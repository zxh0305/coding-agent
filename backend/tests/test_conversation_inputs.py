import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import db
from agent import Agent
from services import conversation_inputs as inputs
from tests.test_wrapup import DbTestBase, RecordingLLM, asst
from tests.test_session_model import ApiTestBase


class TurnInputTest(DbTestBase):
    def root(self, mid="root"):
        db.save_messages("s1",[{"role":"user","content":"original","_mid":mid}],{})
        db.turn_started("s1",mid)
        return inputs.TurnInputs("s1",mid)

    def accept(self,nonce="nonce",text="保留兼容",mid="root",references=None):
        return inputs.accept("s1",1,mid,nonce,text,references or [],None)

    def test_idempotent_admission_and_sealed_rejection(self):
        controller=self.root()
        ident=self.accept()
        self.assertEqual(ident,self.accept())
        with self.assertRaises(inputs.InputConflict):self.accept(text="different")
        self.assertFalse(controller.commit())
        controller.seal()
        self.assertEqual(inputs.list_inputs("s1")[0]["state"],"expired")
        self.assertEqual(ident,self.accept())
        with self.assertRaises(inputs.InputConflict):self.accept(nonce="late")

    def test_workspace_is_bound_to_running_turn(self):
        self.root()
        with db._conn() as conn:
            conn.execute("UPDATE turn_input_gates SET workspace=?",("/original",))
            conn.execute("UPDATE sessions SET workspace=? WHERE id=?",("/changed","s1"))
        with self.assertRaises(inputs.InputConflict):
            inputs.accept("s1",1,"root","wrong","text",[],"/changed")
        self.assertTrue(inputs.accept("s1",1,"root","right","text",[],"/original"))

    def test_commit_and_admission_race_has_one_winner(self):
        for index in range(10):
            mid="root-"+str(index);controller=self.root(mid)
            barrier=threading.Barrier(2)
            def commit():barrier.wait();return controller.commit()
            def accept():
                barrier.wait()
                try:return self.accept(nonce=mid,mid=mid)
                except inputs.InputConflict:return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(commit);second=pool.submit(accept)
                committed,ident=first.result(),second.result()
            self.assertNotEqual(committed,bool(ident))

    def test_references_are_owned_bounded_and_snapshot_only(self):
        self.root()
        db.save_messages("s1",[{"role":"assistant","content":"answer `code`","_mid":"answer"}],{})
        db.save_messages("s2",[{"role":"assistant","content":"private","_mid":"other"}],{})
        refs=inputs.resolve_references("s1",[{"mid":"answer","text":"code"}])
        self.assertEqual(refs[0]["source_role"],"assistant")
        self.assertEqual(len(refs[0]["source_hash"]),64)
        for values in [[{"mid":"other","text":"private"}], [{"mid":"answer","text":"x"*8192}], refs*6]:
            with self.assertRaises(ValueError):inputs.resolve_references("s1",values)
        agent=Agent(llm=RecordingLLM([]),workspace=str(Path(self.tmp)/"workspace"),verbose=False)
        agent.history=[{"role":"user","content":"检查引用","_references":refs}]
        messages=agent._messages_for_model()
        self.assertIn("不授予额外权限",messages[0]["content"])
        self.assertNotIn("_references",messages[0])
        self.assertEqual(agent.history[0]["content"],"检查引用")

    def test_inflight_supplement_replans_without_dispatching_old_tool(self):
        controller=self.root();test=self
        class Model:
            def __init__(self):self.requests=[]
            def chat_stream(self,messages,tools=None,cancel=None):
                self.requests.append(messages)
                if len(self.requests)==1:
                    test.accept()
                    yield "message",{"role":"assistant","content":"old candidate","tool_calls":[{"id":"old","type":"function","function":{"name":"write_file","arguments":'{"path":"obsolete.txt","content":"bad"}'}}]}
                else:yield "message",asst("按补充保留兼容")
        model=Model();agent=Agent(llm=model,workspace=str(Path(self.tmp)/"workspace"),verbose=False)
        agent.input_controller=controller
        events=list(agent.run("original",{"role":"user","content":"original","_mid":"root"}))
        self.assertFalse((Path(agent.ctx.workspace)/"obsolete.txt").exists())
        self.assertEqual(sum(kind=="done" for kind,_ in events),1)
        self.assertTrue(any(m.get("content")=="保留兼容" for m in model.requests[1]))
        self.assertEqual(inputs.list_inputs("s1")[0]["state"],"applied")
        self.assertEqual([item["mid"] for item in db.user_message_index("s1")],["root"])

    def test_running_tool_finishes_once_before_supplement_is_applied(self):
        controller=self.root();test=self
        model=RecordingLLM([
            {"role":"assistant","content":"reading","tool_calls":[{"id":"read","type":"function","function":{"name":"read_file","arguments":'{"path":"README.md"}'}}]},
            asst("按新要求完成")])
        agent=Agent(llm=model,workspace=str(Path(self.tmp)/"workspace"),verbose=False)
        agent.input_controller=controller
        calls=[]
        def execute(name,args,ctx):
            calls.append(name);test.accept()
            test.assertEqual(inputs.list_inputs("s1")[0]["state"],"accepted")
            test.assertFalse(agent.cancel_event.is_set())
            return json.dumps({"ok":True,"content":"read result"})
        with patch("agent.execute_tool",execute):
            events=list(agent.run("original"))
        self.assertEqual(calls,["read_file"])
        self.assertEqual(inputs.list_inputs("s1")[0]["state"],"applied")
        self.assertEqual(sum(kind=="done" for kind,_ in events),1)

    def test_stop_expires_pending_without_second_model_request(self):
        controller=self.root();test=self
        class Model:
            def chat_stream(self,messages,tools=None,cancel=None):
                test.accept();cancel.set();yield "message",asst("partial")
        agent=Agent(llm=Model(),workspace=str(Path(self.tmp)/"workspace"),verbose=False);agent.input_controller=controller
        events=list(agent.run("original"))
        self.assertEqual(sum(kind=="done" for kind,_ in events),1)
        self.assertTrue(events[-1][1]["stopped"])
        self.assertEqual(inputs.list_inputs("s1")[0]["state"],"expired")

    def test_model_budget_wrapup_still_sees_accepted_supplement(self):
        controller=self.root();test=self
        class Model:
            def __init__(self):self.requests=[]
            def chat_stream(self,messages,tools=None,cancel=None):
                self.requests.append((messages,tools))
                if len(self.requests)==1:test.accept()
                yield "message",asst("summary")
        model=Model();agent=Agent(llm=model,max_rounds=1,workspace=str(Path(self.tmp)/"workspace"),verbose=False);agent.input_controller=controller
        with patch("agent.MAX_ROUND_EXTENSIONS",0):events=list(agent.run("original"))
        self.assertEqual(len(model.requests),2)
        self.assertIsNone(model.requests[-1][1])
        self.assertTrue(any(m.get("content")=="保留兼容" for m in model.requests[-1][0]))
        self.assertEqual(sum(kind=="done" for kind,_ in events),1)


class TurnInputApiTest(ApiTestBase):
    def test_receipt_history_and_late_input_do_not_create_new_task(self):
        db.save_messages("s1",[{"role":"user","content":"original","_mid":"root"}],{})
        db.turn_started("s1","root");controller=inputs.TurnInputs("s1","root")
        body={"user_mid":"root","nonce":"steer","message":"追加限制"}
        code,first=self.call("POST","/api/sessions/s1/inputs",body)
        self.assertEqual(code,200)
        self.assertEqual(self.call("POST","/api/sessions/s1/inputs",body)[1],first)
        self.assertEqual(self.call("POST","/api/sessions/foreign/inputs",body)[0],404)
        self.assertTrue(self.call("GET","/api/sessions/s1/inputs?nonce=steer")[1]["accepted"])
        messages=self.call("GET","/api/sessions/s1/messages")[1]["messages"]
        self.assertEqual(messages[0]["supplements"][0]["text"],"追加限制")
        controller.seal()
        self.assertEqual(self.call("POST","/api/sessions/s1/inputs",dict(body,nonce="late"))[0],409)
        self.assertEqual(db.count_messages("s1"),1)
        self.assertEqual(self.call("GET","/api/sessions/s1/queue")[1]["items"],[])
