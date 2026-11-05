import json
import unittest
import db
from tests.test_session_model import ApiTestBase, SessionModelBase


class TurnCheckStorageTests(SessionModelBase):
    def test_history_roundtrip_and_restart(self):
        mid = "input"
        db.set_assessment("s1", mid, "verifying")
        ident = db.start_check("s1", mid, "unit", 1, {"cmd":"make test", "argv":["make","test"]})
        self.assertEqual(db.get_checks("s1", mid)[0]["status"], "running")
        db.mark_running_turns_interrupted()
        self.assertEqual(db.get_checks("s1", mid)[0]["status"], "interrupted")
        self.assertEqual(db.get_assessments("s1", [mid])[mid]["assessment"], "interrupted")
        self.assertFalse(db.finish_check("s1", ident, "passed", {"ok":True}))
        db.init_db()
        self.assertEqual(db.get_checks("s1", mid)[0]["status"], "interrupted")

    def test_truncate_and_delete_remove_records(self):
        message = {"role":"user","content":"change","_mid":"input"}
        db.save_messages("s1", [message], {})
        ident = db.start_check("s1", "input", "unit", 1, {"cmd":"test"})
        db.finish_check("s1", ident, "failed", {"ok":False}, "failure")
        db.set_assessment("s1", "input", "finished", "failed")
        db.truncate_from("s1", "input")
        self.assertEqual(db.get_checks("s1"), [])
        self.assertEqual(db.get_assessments("s1", ["input"]), {})
        db.start_check("s2", "input", "unit", 1, {})
        db.delete_session("s2")
        self.assertEqual(db.get_checks("s2"), [])


class TurnCheckApiTests(ApiTestBase):
    def test_ownership_and_lazy_output(self):
        ident = db.start_check("s1", "input", "unit", 1, {"cmd":"make test"})
        db.finish_check("s1", ident, "failed", {"cmd":"make test", "ok":False}, "first\nsecond")
        db.set_assessment("s1", "input", "finished", "failed")
        code, result = self.call("GET", "/api/sessions/s1/checks?user_mid=input")
        self.assertEqual(code, 200)
        self.assertEqual(result["checks"][0]["status"], "failed")
        code, result = self.call("GET", f"/api/sessions/s1/checks/{ident}/output?offset=2&limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(result["lines"], ["second"])
        self.assertEqual(self.call("GET", f"/api/sessions/s2/checks/{ident}/output")[0], 400)
        self.token = db.create_token(db.create_user("checks-other", "pw")["id"])
        self.assertEqual(self.call("GET", "/api/sessions/s1/checks")[0], 404)
