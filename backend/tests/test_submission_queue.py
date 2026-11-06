import json
import threading
import time
from unittest.mock import patch

import db
from routes import busref
from services import submission_queue as queue
from tests.test_session_model import ApiTestBase
from tests.test_wrapup import DbTestBase


class QueueStorageTest(DbTestBase):
    def put(self, nonce, text, attachments=None):
        message = {"role":"user","content":text,"_mid":nonce,"_input_text":text}
        db.record_submission(1,nonce,"hash-"+nonce,"s1",nonce,self.tmp,[text,message,attachments or []])

    def test_edit_preserves_receipt_and_claim_uses_latest_payload(self):
        self.put("one","original")
        queue.change("s1",1,"one",1,"edit","edited")
        original = db.submission_receipt(1,"one")
        self.assertEqual(json.loads(original["payload"])[0], "original")
        self.assertEqual(queue.entries("s1")[0]["text"], "edited")
        taken = queue.take_next("s1",self.tmp,"worker")
        self.assertEqual(json.loads(taken["payload"])[1]["content"], "edited")
        with self.assertRaises(queue.QueueConflict):
            queue.change("s1",1,"one",2,"edit","too late")

    def test_order_and_stale_versions(self):
        self.put("one","one");self.put("two","two")
        queue.change("s1",1,"two",1,"up")
        self.assertEqual([item["mid"] for item in queue.entries("s1")], ["two","one"])
        with self.assertRaises(queue.QueueConflict):
            queue.change("s1",1,"one",1,"cancel")
        current = queue.entries("s1")[1]
        queue.change("s1",1,"one",current["revision"],"cancel")
        self.assertIsNone(db.submission_receipt(1,"one")["payload"])
        self.assertEqual(db.submission_receipt(1,"one")["worker"],"cancelled")

    def test_workspace_and_archive_pause_until_explicit_resume(self):
        self.put("one","one")
        self.assertIsNone(queue.take_next("s1","other","worker"))
        item = queue.entries("s1")[0]
        self.assertTrue(item["reason"])
        self.assertIsNone(queue.take_next("s1",self.tmp,"worker"))
        queue.change("s1",1,"one",item["revision"],"resume")
        db.archive_session("s1",1)
        self.assertIsNone(queue.take_next("s1",self.tmp,"worker"))
        self.assertIn("归档",queue.entries("s1")[0]["reason"])

    def test_attachment_parts_and_skill_snapshot_survive_edit(self):
        parts = [{"type":"image_url","image_url":{"url":"data:image/png;base64,test"}},
                 {"type":"text","text":"用户附带了以下文件：notes"}]
        message = {"role":"user","content":parts,"_mid":"one","_input_text":"", "_skills":[{"id":"project:test","body":"snapshot"}]}
        db.record_submission(1,"one","hash","s1","one",self.tmp,["[附件]",message,[{"name":"image","kind":"image"}]])
        queue.change("s1",1,"one",1,"edit","new request")
        payload = json.loads(queue.take_next("s1",self.tmp,"w")["payload"])
        self.assertEqual(payload[1]["content"][1:],parts)
        self.assertEqual(payload[1]["_skills"][0]["body"],"snapshot")

    def test_restart_migration_preserves_unclaimed_receipts(self):
        self.put("one","one"); self.put("two","two")
        db.claim_submission(1,"two","old-worker")
        with db._conn() as conn:
            conn.execute("DROP TABLE submission_queue")
            conn.execute("PRAGMA user_version=32")
        db.init_db()
        self.assertEqual([item["mid"] for item in queue.entries("s1")],["one"])
        db.init_db()
        self.assertEqual(len(queue.entries("s1")),1)

    def test_large_preview_is_bounded_and_detail_is_not_truncated(self):
        text = "x"*10000
        self.put("one",text)
        item = queue.entries("s1")[0]
        self.assertEqual(len(item["text"]),4000)
        self.assertTrue(item["text_truncated"])
        self.assertEqual(queue.detail("s1","one")["text"],text)

    def test_text_attachment_only_edit_keeps_reference(self):
        content = "用户附带了以下文件：notes.txt"
        message = {"role":"user","content":content,"_input_text":"","_mid":"one"}
        db.record_submission(1,"one","hash","s1","one",self.tmp,["[附件] notes.txt",message,[{"kind":"file","name":"notes.txt"}]])
        queue.change("s1",1,"one",1,"edit","please review")
        payload = json.loads(queue.take_next("s1",self.tmp,"w")["payload"])
        self.assertEqual(payload[1]["content"],[{"type":"text","text":"please review"},{"type":"text","text":content}])

    def test_restart_keeps_interrupted_input_without_reexecuting(self):
        self.put("one","first");self.put("two","later")
        queue.take_next("s1",self.tmp,"worker")
        db.mark_running_turns_interrupted()
        items=queue.entries("s1")
        self.assertEqual(items[0]["state"],"interrupted")
        self.assertEqual(queue.detail("s1","one")["text"],"first")
        self.assertTrue(items[1]["reason"])
        self.assertIsNone(queue.take_next("s1",self.tmp,"new-worker"))
        queue.change("s1",1,"one",items[0]["revision"],"cancel")
        queue.change("s1",1,"two",items[1]["revision"],"resume")
        self.assertEqual(queue.take_next("s1",self.tmp,"new-worker")["mid"],"two")

    def test_completed_turn_not_misreported_after_queue_cleanup_crash(self):
        self.put("one","first");queue.take_next("s1",self.tmp,"worker")
        db.turn_finished("s1","one","done")
        db.mark_running_turns_interrupted()
        self.assertEqual(queue.entries("s1"),[])


class QueueApiTest(ApiTestBase):
    def test_owner_revision_cancel_and_lost_original_reply(self):
        self.set_global("p1","a-small")
        body = {"nonce":"original","message":"queued"}
        with patch("routes.sessions._start_submission"):
            code, receipt = self.call("POST","/api/sessions/s1/messages",body)
            self.assertEqual(code,200)
            _, data = self.call("GET","/api/sessions/s1/queue")
            item = data["items"][0]
            command = {"mid":item["mid"],"revision":item["revision"],"action":"edit","text":"updated"}
            self.assertEqual(self.call("POST","/api/sessions/s1/queue",command)[0],200)
            self.assertEqual(self.call("POST","/api/sessions/s1/queue",command)[0],409)
            self.assertEqual(self.call("GET","/api/sessions/foreign/queue")[0],404)
            command.update(revision=2,action="cancel")
            self.assertEqual(self.call("POST","/api/sessions/s1/queue",command)[0],200)
            self.assertEqual(self.call("POST","/api/sessions/s1/messages",body)[1]["mid"],receipt["mid"])
            self.assertEqual(self.call("GET","/api/sessions/s1/queue")[1]["items"],[])

    def test_one_consumer_claims_only_after_active_turn_finishes(self):
        self.set_global("p1","a-small")
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        executed = []
        def run(sid,agent,plain,message,nonce,atts,accept):
            with busref._session_lock(sid):
                accepted = accept()
                if not accepted:
                    return
                executed.append(accepted[0])
                if len(executed)==1:
                    entered.set(); release.wait(5)
                db.turn_finished(sid,accepted[1]["_mid"],"done")
                if len(executed)==3: completed.set()
        with patch.object(busref,"_run_round",side_effect=run):
            self.call("POST","/api/sessions/s1/messages",{"nonce":"a","message":"first"})
            self.assertTrue(entered.wait(2))
            self.call("POST","/api/sessions/s1/messages",{"nonce":"b","message":"second"})
            self.call("POST","/api/sessions/s1/messages",{"nonce":"c","message":"third"})
            items = self.call("GET","/api/sessions/s1/queue")[1]["items"]
            self.assertEqual(len(items),2)
            self.assertIsNone(db.submission_receipt(self.uid,"b")["worker"])
            self.call("POST","/api/sessions/s1/queue",{"mid":items[1]["mid"],"revision":items[1]["revision"],"action":"up"})
            items = self.call("GET","/api/sessions/s1/queue")[1]["items"]
            self.call("POST","/api/sessions/s1/queue",{"mid":items[1]["mid"],"revision":items[1]["revision"],"action":"edit","text":"edited second"})
            release.set(); self.assertTrue(completed.wait(3))
            deadline = time.monotonic()+2
            while time.monotonic()<deadline:
                with db._conn() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM submission_queue WHERE state='finished'").fetchone()[0]
                if count==3: break
                time.sleep(.01)
            self.assertEqual(count,3)
            self.assertEqual(executed,["first","third","edited second"])
