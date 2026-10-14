"""
记忆 HTTP 接口的测试（真实服务 + 真实临时库）
================================================================================

cd backend && python3 -m unittest tests.test_memory_api -v

记忆 v2 之前记忆没有 HTTP 面——只有模型(读索引)与提取器(写文件)。这个模块是
第一个给人用的入口（列/读/改/删/换层），所以它是**唯一未被任何既有测试覆盖的
写入面**，必须自带一套归属与越界用例。

复用 test_session_model.ApiTestBase（真实 ThreadingHTTPServer + token），
与其它接口测试同一套跑法。
"""

import json
import os
from pathlib import Path

import memory
from tests.test_session_model import ApiTestBase


class MemoryApiBase(ApiTestBase):
    """带一个绑定了真实工作区的会话，两层记忆各就位。"""

    def setUp(self):
        super().setUp()
        self.ws = Path(self.tmp) / "proj"
        self.ws.mkdir(parents=True, exist_ok=True)
        db_module = __import__("db")
        db_module.set_session_workspace("s1", str(self.ws))
        self.personal = memory.memory_dir_for_user(self.uid)
        self.project = memory.memory_dir_for_project(str(self.ws))

    # ---------- 夹具 ----------

    def seed_entry(self, layer: str, name: str, body: str = "正文",
                   description: str = "摘要", mtype: str = "project"):
        d = self.personal if layer == "personal" else self.project
        memory.write_entry(d, name, body, description=description, mtype=mtype,
                           owner="user" if layer == "personal" else None)

    def qs(self, layer: str, name: str = "", sid: str = "s1") -> str:
        s = f"?layer={layer}"
        if sid:
            s += f"&session_id={sid}"
        if name:
            s += f"&name={name}"
        return s


class TestMemoryList(MemoryApiBase):

    def test_personal_layer_lists_entries(self):
        self.seed_entry("personal", "pnpm-not-npm.md", description="用 pnpm")
        status, data = self.call("GET", "/api/memory" + self.qs("personal"))
        self.assertEqual(status, 200)
        self.assertEqual(data["layer"], "personal")
        names = [m["file"] for m in data["memories"]]
        self.assertIn("pnpm-not-npm.md", names)
        row = next(m for m in data["memories"] if m["file"] == "pnpm-not-npm.md")
        self.assertEqual(row["owner"], "user")
        self.assertTrue(row["has_frontmatter"])

    def test_layers_are_separate(self):
        """个人层与项目层互不串味——这是双层记忆的全部意义。"""
        self.seed_entry("personal", "only-personal.md", description="个人")
        self.seed_entry("project", "only-project.md", description="项目")
        _, p = self.call("GET", "/api/memory" + self.qs("personal"))
        _, j = self.call("GET", "/api/memory" + self.qs("project"))
        self.assertEqual([m["file"] for m in p["memories"]], ["only-personal.md"])
        self.assertEqual([m["file"] for m in j["memories"]], ["only-project.md"])

    def test_read_single_entry(self):
        self.seed_entry("personal", "x.md", body="正文内容", description="摘要")
        status, data = self.call("GET", "/api/memory" + self.qs("personal", "x.md"))
        self.assertEqual(status, 200)
        self.assertEqual(data["body"], "正文内容")
        self.assertEqual(data["description"], "摘要")

    def test_unknown_layer_is_400(self):
        status, data = self.call("GET", "/api/memory?layer=bogus")
        self.assertEqual(status, 400)
        self.assertIn("未知的层", data["error"])

    def test_missing_entry_is_404(self):
        status, _ = self.call("GET", "/api/memory" + self.qs("personal", "nope.md"))
        self.assertEqual(status, 404)

    def test_unauthenticated_is_401(self):
        status, _ = self.call("GET", "/api/memory" + self.qs("personal"), token=False)
        self.assertEqual(status, 401)

    def test_project_layer_needs_owned_session(self):
        """拿别人的会话 id 解析不出项目记忆（不能靠一个 id 读到别人的目录）。"""
        db_module = __import__("db")
        db_module.create_user("other", "pw")
        other_uid = db_module.get_user_by_name("other")["id"]
        db_module.create_session("s-other", other_uid)
        db_module.set_session_workspace("s-other", str(Path(self.tmp) / "other-ws"))
        status, _ = self.call("GET", "/api/memory?layer=project&session_id=s-other")
        # 归属不符 → 解析不出会话 → 400（与"没有会话"同一处理，不泄露存在性）
        self.assertEqual(status, 400)


class TestMemorySave(MemoryApiBase):

    def test_save_creates_entry_and_index_line(self):
        status, data = self.call("POST", "/api/memory", {
            "layer": "personal", "name": "new-pref.md",
            "body": "用户偏好：先给方案", "description": "先给方案"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue((self.personal / "new-pref.md").is_file())
        index = memory.read_index_text(self.personal)
        self.assertIn("new-pref.md", index, "索引行必须同步，否则模型看不到")

    def test_save_overwrites_same_name(self):
        self.seed_entry("personal", "same.md", body="旧", description="旧描述")
        self.call("POST", "/api/memory", {
            "layer": "personal", "name": "same.md", "body": "新", "description": "新描述"})
        d = memory.read_entry(self.personal, "same.md")
        self.assertEqual(d["body"], "新")
        self.assertNotIn("旧描述", memory.read_index_text(self.personal))
        self.assertEqual(memory.read_index_text(self.personal).count("same.md"), 1,
                         "覆盖不该在索引里留重复行")

    def test_description_falls_back_to_first_line(self):
        """只改正文时不该让索引退化成空钩子——描述从正文首行兜底。"""
        self.call("POST", "/api/memory", {
            "layer": "personal", "name": "x.md", "body": "# 用 pnpm\n换行内容"})
        row = memory.list_entries(self.personal)[0]
        self.assertEqual(row["description"], "用 pnpm")

    def test_illegal_filename_rejected(self):
        for bad in ("../evil.md", "/tmp/evil.md", "Up.md", "a_b.md", "x.txt"):
            status, data = self.call("POST", "/api/memory",
                                     {"layer": "personal", "name": bad, "body": "x"})
            self.assertEqual(status, 400, f"{bad!r} 应被拒")
            self.assertIn("文件名", data["error"])
        self.assertFalse((Path(self.tmp) / "evil.md").exists())

    def test_missing_name_rejected(self):
        status, _ = self.call("POST", "/api/memory", {"layer": "personal", "body": "x"})
        self.assertEqual(status, 400)

    def test_save_to_project_layer(self):
        status, _ = self.call("POST", "/api/memory", {
            "layer": "project", "session_id": "s1",
            "name": "test-cmd.md", "body": "python3 -m unittest", "description": "测试"})
        self.assertEqual(status, 200)
        self.assertTrue((self.project / "test-cmd.md").is_file())


class TestMemoryMove(MemoryApiBase):

    def test_move_project_to_personal_stamps_owner(self):
        """换层：内容搬过去，owner 戳上——这样它才会在【别的项目】里也出现。"""
        self.seed_entry("project", "shared-pref.md", body="用 pnpm", description="用 pnpm")
        status, data = self.call("POST", "/api/memory/move", {
            "name": "shared-pref.md", "from": "project", "to": "personal",
            "session_id": "s1"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse((self.project / "shared-pref.md").exists(), "源层应已删除")
        self.assertTrue((self.personal / "shared-pref.md").is_file())
        entry = memory.read_entry(self.personal, "shared-pref.md")
        self.assertEqual(entry["body"], "用 pnpm", "正文必须原样搬过去")
        # 索引两层各自同步：源层不再有它，目标层有它
        self.assertNotIn("shared-pref.md", memory.read_index_text(self.project))
        self.assertIn("shared-pref.md", memory.read_index_text(self.personal))

    def test_moved_entry_visible_in_other_project(self):
        """核心验收：搬进个人层后，【另一个项目】也能看到它。"""
        self.seed_entry("project", "shared-pref.md", description="用 pnpm")
        self.call("POST", "/api/memory/move", {
            "name": "shared-pref.md", "from": "project", "to": "personal",
            "session_id": "s1"})
        other_proj = memory.memory_dir_for_project(str(Path(self.tmp) / "another"))
        blocks = memory.memory_blocks(other_proj, self.uid)
        self.assertIn("shared-pref.md", blocks.split(memory.PROJECT_HEADING)[0],
                      "个人层条目必须在任何项目里都可见")

    def test_move_personal_to_project_clears_owner(self):
        self.seed_entry("personal", "x.md", description="d")
        self.call("POST", "/api/memory/move", {
            "name": "x.md", "from": "personal", "to": "project", "session_id": "s1"})
        self.assertTrue((self.project / "x.md").is_file())
        self.assertFalse((self.personal / "x.md").exists())

    def test_move_missing_entry_is_404(self):
        status, _ = self.call("POST", "/api/memory/move", {
            "name": "nope.md", "from": "project", "to": "personal", "session_id": "s1"})
        self.assertEqual(status, 404)

    def test_move_same_layer_rejected(self):
        status, _ = self.call("POST", "/api/memory/move", {
            "name": "x.md", "from": "personal", "to": "personal"})
        self.assertEqual(status, 400)

    def test_move_preserves_body_of_uncurated_file(self):
        """没有 frontmatter 的老记忆（33/74 条）也能搬，且正文一字不丢。"""
        raw = "# 标题\n\n这是模型当年直接写的正文，没有 frontmatter。\n"
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "legacy.md").write_text(raw, encoding="utf-8")
        status, _ = self.call("POST", "/api/memory/move", {
            "name": "legacy.md", "from": "project", "to": "personal", "session_id": "s1"})
        self.assertEqual(status, 200)
        entry = memory.read_entry(self.personal, "legacy.md")
        self.assertIn("这是模型当年直接写的正文", entry["body"])
        self.assertIn("标题", entry["body"])


class TestMemoryDelete(MemoryApiBase):

    def test_delete_removes_file_and_index(self):
        self.seed_entry("personal", "gone.md", description="待删")
        status, data = self.call("DELETE", "/api/memory/gone.md" + self.qs("personal"))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse((self.personal / "gone.md").exists())
        self.assertNotIn("gone.md", memory.read_index_text(self.personal))

    def test_delete_is_idempotent(self):
        status, data = self.call("DELETE", "/api/memory/never.md" + self.qs("personal"))
        self.assertEqual(status, 200)
        self.assertFalse(data["existed"])

    def test_delete_illegal_name_rejected(self):
        status, _ = self.call("DELETE", "/api/memory/..%2Fevil.md" + self.qs("personal"))
        self.assertIn(status, (400, 404))


class TestMemoryContentSafety(MemoryApiBase):

    def test_sensitive_entry_flagged_and_not_injected(self):
        """含凭据的记忆：UI 里标记出来、且不进注入——但文件仍在（用户自己的数据）。"""
        memory.write_entry(self.personal, "credentials.md",
                           "服务器 192.168.1.1 账号 root 密码 hunter2",
                           description="服务器 192.168.1.1 密码", mtype="reference")
        _, data = self.call("GET", "/api/memory" + self.qs("personal"))
        row = next(m for m in data["memories"] if m["file"] == "credentials.md")
        self.assertTrue(row["sensitive"], "UI 必须标出含敏感信息")
        # 注入侧：不进 system
        blocks = memory.memory_blocks(self.project, self.uid)
        self.assertNotIn("hunter2", blocks)

    def test_uncurated_file_flagged(self):
        """没有 frontmatter 的老记忆要在列表里被标出来，而不是显示成空白。"""
        self.personal.mkdir(parents=True, exist_ok=True)
        (self.personal / "legacy.md").write_text("# 只有标题\n正文\n", encoding="utf-8")
        _, data = self.call("GET", "/api/memory" + self.qs("personal"))
        row = next(m for m in data["memories"] if m["file"] == "legacy.md")
        self.assertFalse(row["has_frontmatter"])
        self.assertEqual(row["description"], "")
