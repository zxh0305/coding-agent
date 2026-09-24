"""
会话级模型选择的单元测试（cd backend && python3 -m unittest tests.test_session_model -v）
================================================================================

动机：激活模型原本只存在 settings 的全局单例里（active_model 一条记录），
切一次模型等于把所有会话都改了——而工作区、权限模式、附件都是会话私有的。
改造后模型跟会话走：sessions.provider_id/model 为空 = 跟随全局默认（新任务的
初始模型）。全部用例跑在临时库里，不碰项目根的 agent_data.db。

覆盖三层：
1. 存储与迁移：会话自选模型可读写；未选过 = NULL；老库（v14，无这两列）
   迁移后补列且老会话仍为 NULL（行为逐字节不变）；迁移幂等。
2. 解析链（app._resolve_active / _active_window）：会话自选 → 全局默认 →
   第一个可用供应商，逐级回退，且**不篡改**用户的显式选择（只是回退）。
3. 接口：/api/active-model 带 session_id 只改该任务、不带改全局默认；
   /api/config 按会话返回模型与窗口；别人的/不存在的 session_id 一律按默认处理。
"""

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app
import db


class SessionModelBase(unittest.TestCase):
    """每个用例独占一个临时库（重定向 db.DB_PATH，模式同 test_storage）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="session_model_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_user("tester", "pw")
        self.uid = db.get_user_by_name("tester")["id"]
        db.create_session("s1", self.uid)
        db.create_session("s2", self.uid)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    # ---------- 夹具 ----------

    def seed(self):
        """两个供应商：A(p1) 带 a-big/a-small，B(p2) 带 b-vision（标注视觉）。
        init_db 播种的 default 供应商与 active_model 一并清掉：夹具只留本用例
        自己造的世界，避免"没设过默认"的用例被播种值干扰。"""
        db.delete_provider("default")
        db.set_setting("active_model", {})
        db.upsert_provider("p1", "A", "http://a", "k1", True, "openai", 100000)
        db.upsert_model("p1", "a-big", 200000, True)
        db.upsert_model("p1", "a-small", 8000, True)
        db.upsert_provider("p2", "B", "http://b", "k2", True, "openai", 50000)
        db.upsert_model("p2", "b-vision", 64000, True, vision=True)

    def set_global(self, pid: str, model: str):
        db.set_setting("active_model", {"provider_id": pid, "model": model})


# ---------------------------------------------------------------------------
# 一、存储与迁移
# ---------------------------------------------------------------------------

class TestSessionModelStorage(SessionModelBase):

    def test_unset_is_null_and_follows_default(self):
        """没单独选过的会话：库里是 NULL，读取返回 None（= 跟随全局默认）。"""
        self.assertIsNone(db.get_session_model("s1"))
        with db._conn() as conn:
            row = conn.execute("SELECT provider_id, model FROM sessions WHERE id='s1'").fetchone()
        self.assertIsNone(row["provider_id"])
        self.assertIsNone(row["model"])

    def test_set_then_get_roundtrip_and_isolation(self):
        """写入只影响该会话：同库另一个会话仍是 NULL。"""
        self.seed()
        db.set_session_model("s1", "p1", "a-big")
        self.assertEqual(db.get_session_model("s1"), {"provider_id": "p1", "model": "a-big"})
        self.assertIsNone(db.get_session_model("s2"))

    def test_overwrite_same_session(self):
        self.seed()
        db.set_session_model("s1", "p1", "a-small")
        db.set_session_model("s1", "p2", "b-vision")
        self.assertEqual(db.get_session_model("s1"), {"provider_id": "p2", "model": "b-vision"})

    def test_get_unknown_session_returns_none(self):
        self.assertIsNone(db.get_session_model("nope"))

    def test_legacy_db_upgrade_keeps_sessions_on_default(self):
        """老库（v14，此前没有这两列）迁移后：列补齐、老会话仍为 NULL。"""
        if sqlite3.sqlite_version_info < (3, 35, 0):
            self.skipTest("DROP COLUMN 需要 SQLite ≥ 3.35")
        self.seed()
        db.set_setting("active_model", {"provider_id": "p1", "model": "a-big"})
        with db._conn() as conn:  # 退回老库形态：删掉新列 + 版本号退回 14
            conn.execute("ALTER TABLE sessions DROP COLUMN provider_id")
            conn.execute("ALTER TABLE sessions DROP COLUMN model")
            conn.execute("PRAGMA user_version = 14")
        db.init_db()          # 再跑一次启动迁移
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(sessions)")}
            rows = conn.execute("SELECT id, provider_id, model FROM sessions").fetchall()
        self.assertIn("provider_id", cols)
        self.assertIn("model", cols)
        self.assertEqual(len(rows), 2)                    # 老会话没被迁移弄丢
        for r in rows:
            self.assertIsNone(r["provider_id"])           # 一律跟随全局默认
            self.assertIsNone(r["model"])
        prov, model = app._resolve_active("s1")
        self.assertEqual((prov["id"], model), ("p1", "a-big"))

    def test_migration_idempotent(self):
        """迁移跑两遍：版本号与列集合都不变（不会重复 ALTER 报 duplicate column）。"""
        db.init_db()
        first = None
        with db._conn() as conn:
            first = ({r["name"] for r in conn.execute("PRAGMA table_xinfo(sessions)")},
                     conn.execute("PRAGMA user_version").fetchone()[0])
        db.init_db()
        with db._conn() as conn:
            second = ({r["name"] for r in conn.execute("PRAGMA table_xinfo(sessions)")},
                      conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(first, second)
        self.assertEqual(second[1], db.SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# 二、解析链：会话自选 → 全局默认 → 第一个可用供应商
# ---------------------------------------------------------------------------

class TestResolveChain(SessionModelBase):

    def test_session_wins_over_global(self):
        self.seed()
        self.set_global("p1", "a-big")
        db.set_session_model("s2", "p2", "b-vision")
        prov, model = app._resolve_active("s2")
        self.assertEqual((prov["id"], model), ("p2", "b-vision"))
        prov1, model1 = app._resolve_active("s1")       # 未选过 → 全局默认
        self.assertEqual((prov1["id"], model1), ("p1", "a-big"))

    def test_no_sid_uses_global(self):
        self.seed()
        self.set_global("p2", "b-vision")
        prov, model = app._resolve_active()
        self.assertEqual((prov["id"], model), ("p2", "b-vision"))

    def test_session_falls_back_when_provider_deleted(self):
        """会话选的供应商被删：回退全局默认，且**不**改写会话里的选择。"""
        self.seed()
        self.set_global("p1", "a-big")
        db.set_session_model("s1", "p2", "b-vision")
        db.delete_provider("p2")
        prov, model = app._resolve_active("s1")
        self.assertEqual((prov["id"], model), ("p1", "a-big"))
        self.assertEqual(db.get_session_model("s1"), {"provider_id": "p2", "model": "b-vision"})

    def test_session_falls_back_when_provider_disabled(self):
        self.seed()
        self.set_global("p1", "a-big")
        db.set_session_model("s1", "p2", "b-vision")
        db.upsert_provider("p2", "B", "http://b", None, False)   # 停用 B
        prov, model = app._resolve_active("s1")
        self.assertEqual((prov["id"], model), ("p1", "a-big"))

    def test_session_model_renamed_falls_to_sibling_in_same_provider(self):
        """会话选的模型名失效但供应商还在：用该供应商第一个启用的模型，不跨供应商。"""
        self.seed()
        self.set_global("p1", "a-big")
        db.set_session_model("s1", "p1", "gone")
        prov, model = app._resolve_active("s1")
        self.assertEqual(prov["id"], "p1")
        self.assertIn(model, ("a-big", "a-small"))

    def test_session_with_no_enabled_model_falls_back_global(self):
        self.seed()
        self.set_global("p1", "a-big")
        db.set_session_model("s1", "p2", "b-vision")
        db.upsert_model("p2", "b-vision", 64000, False)          # 该供应商下全停用
        prov, model = app._resolve_active("s1")
        self.assertEqual((prov["id"], model), ("p1", "a-big"))

    def test_all_broken_falls_to_first_available_provider(self):
        self.seed()
        prov, model = app._resolve_active("s1")                  # 连全局默认都没设
        self.assertIn(prov["id"], ("p1", "p2"))
        self.assertIn(model, ("a-big", "a-small", "b-vision"))

    def test_window_follows_session_model(self):
        """窗口（容量分母 / 压缩基准）按该会话的模型算，各会话可以不同。"""
        self.seed()
        self.set_global("p1", "a-small")                         # 全局：8000
        db.set_session_model("s1", "p1", "a-big")                # s1：200000
        self.assertEqual(app._active_window("s1"), 200000)
        self.assertEqual(app._active_window("s2"), 8000)         # 未选过 → 全局
        self.assertEqual(app._active_window(), 8000)

    def test_vision_selection_follows_session(self):
        """看图后端按会话主模型判断：主模型非视觉时借视觉模型。"""
        self.seed()
        self.set_global("p1", "a-big")                           # 非视觉
        db.set_session_model("s1", "p2", "b-vision")             # 视觉模型
        self.assertEqual(app._resolve_vision_model("s1"), (dict(db.get_provider("p2")), "b-vision"))
        prov, model = app._resolve_vision_model("s2")            # 借用标注了视觉的那个
        self.assertEqual((prov["id"], model), ("p2", "b-vision"))

    def test_vision_backend_binds_session(self):
        """_vision_backend_for：工具层只传两个位置参数，落在同一会话上。"""
        self.seed()
        self.set_global("p1", "a-big")
        db.set_session_model("s1", "p2", "b-vision")
        seen = {}

        class FakeClient:
            def chat(self, messages):
                return {"content": "看图结果"}

        orig = app.create_client
        def fake_create(fmt, api_key, base_url, model, timeout=60):
            seen["model"] = model
            return FakeClient()
        app.create_client = fake_create
        self.addCleanup(lambda: setattr(app, "create_client", orig))
        out = app._vision_backend_for("s1")([], "这是什么")
        self.assertEqual(seen["model"], "b-vision")              # 用该会话的视觉模型
        self.assertEqual(out, "看图结果")


# ---------------------------------------------------------------------------
# 三、接口层：起真实 HTTP 服务，验证带/不带 session_id 的两种语义
# ---------------------------------------------------------------------------

class ApiTestBase(SessionModelBase):
    """起一个真实服务（随机端口）跑接口；token 直接取自 db 层。"""

    def setUp(self):
        super().setUp()
        self.seed()
        self.token = db.create_token(self.uid)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method: str, path: str, body=None, token=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + self.token)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class TestActiveModelApi(ApiTestBase):

    def test_config_without_sid_is_global_default(self):
        self.set_global("p1", "a-small")
        status, cfg = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual((cfg["model"], cfg["session_id"]), ("a-small", ""))

    def test_config_with_sid_reflects_that_session(self):
        self.set_global("p1", "a-small")
        db.set_session_model("s1", "p1", "a-big")
        _, cfg1 = self.call("GET", "/api/config?session_id=s1")
        _, cfg2 = self.call("GET", "/api/config?session_id=s2")
        self.assertEqual((cfg1["model"], cfg1["session_id"]), ("a-big", "s1"))
        self.assertEqual((cfg2["model"], cfg2["session_id"]), ("a-small", "s2"))

    def test_switch_with_sid_only_changes_that_session(self):
        """核心回归：切一个会话的模型，别的会话与全局默认都不动。"""
        self.set_global("p1", "a-small")
        status, cfg = self.call("POST", "/api/active-model",
                                {"provider_id": "p2", "model": "b-vision", "session_id": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(cfg["model"], "b-vision")
        _, other = self.call("GET", "/api/config?session_id=s2")
        self.assertEqual(other["model"], "a-small")              # 兄弟会话不受影响
        _, glob = self.call("GET", "/api/config")
        self.assertEqual(glob["model"], "a-small")               # 全局默认不受影响
        _, again = self.call("GET", "/api/config?session_id=s1")
        self.assertEqual(again["model"], "b-vision")             # 本会话已切换

    def test_switch_without_sid_changes_global_default_only(self):
        self.set_global("p1", "a-small")
        db.set_session_model("s1", "p1", "a-big")
        status, cfg = self.call("POST", "/api/active-model",
                                {"provider_id": "p2", "model": "b-vision"})
        self.assertEqual(status, 200)
        self.assertEqual(cfg["model"], "b-vision")
        _, glob = self.call("GET", "/api/config")
        self.assertEqual(glob["model"], "b-vision")              # 默认已改
        _, s1 = self.call("GET", "/api/config?session_id=s1")
        self.assertEqual(s1["model"], "a-big")                   # 老会话保留自己的选择

    def test_switch_window_returned_matches_model(self):
        self.set_global("p1", "a-small")
        _, cfg = self.call("POST", "/api/active-model",
                           {"provider_id": "p1", "model": "a-big", "session_id": "s1"})
        self.assertEqual(cfg["context_window"], 200000)
        _, ctx = self.call("GET", "/api/context?session_id=s1")
        self.assertEqual(ctx["window"], 200000)                  # 容量分母同源

    def test_switch_rejects_unknown_model(self):
        status, err = self.call("POST", "/api/active-model",
                                {"provider_id": "p1", "model": "nope", "session_id": "s1"})
        self.assertEqual(status, 400)
        self.assertIn("没有模型", err["error"])

    def test_switch_rejects_unknown_provider(self):
        status, _ = self.call("POST", "/api/active-model",
                              {"provider_id": "zz", "model": "a-big"})
        self.assertEqual(status, 400)

    def test_switch_on_foreign_session_is_404(self):
        db.create_user("other", "pw")
        other_uid = db.get_user_by_name("other")["id"]
        db.create_session("theirs", other_uid)
        status, _ = self.call("POST", "/api/active-model",
                              {"provider_id": "p1", "model": "a-big", "session_id": "theirs"})
        self.assertEqual(status, 404)

    def test_config_with_foreign_sid_falls_back_to_default(self):
        """别人的 session_id 走读接口：按默认处理，不泄露也不报错。"""
        db.create_user("other", "pw")
        other_uid = db.get_user_by_name("other")["id"]
        db.create_session("theirs", other_uid)
        db.set_session_model("theirs", "p2", "b-vision")
        self.set_global("p1", "a-small")
        status, cfg = self.call("GET", "/api/config?session_id=theirs")
        self.assertEqual(status, 200)
        self.assertEqual((cfg["model"], cfg["session_id"]), ("a-small", ""))

    def test_models_lists_active_of_session(self):
        db.set_session_model("s1", "p2", "b-vision")
        _, data = self.call("GET", "/api/models?session_id=s1")
        self.assertEqual(data["active"], "b-vision")
        self.assertEqual(data["active_provider"], "p2")


if __name__ == "__main__":
    unittest.main()
