"""
记忆 v2（双层记忆）Step 1 的单元测试：身份解析 + 布局迁移 + frontmatter 兼容
================================================================================

cd backend && python3 -m unittest tests.test_memory_v2 -v

Step 1 只做数据模型与迁移，**不改注入**（双块注入是 Step 2、UI 是 Step 3）。
所以这里验证的是：身份解析三级、迁移幂等/合并/容错、frontmatter 向后兼容、
以及 memory_dir 的过渡期兼容守卫。

五组：
  A 桥接前缀顺序（已知最易错处，Step 2 才会真的注册子前缀，先锁住语义）
  B project_identity 三级解析
  C 布局迁移（幂等 / 合并 / 备份失败零改动 / 临时库 no-op / 不写 owner）
  D frontmatter 向后兼容
  E 目录函数与 memory_dir 兼容守卫
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import memory
from memory import (INDEX_NAME, PROJECTS_SUBDIR, USERS_SUBDIR,
                    memory_dir, memory_dir_for_project, memory_dir_for_user,
                    normalize_git_remote, parse_frontmatter,
                    project_identity, render_frontmatter, render_memory_file,
                    workspace_token)


class V2Base(unittest.TestCase):
    """每个用例一个临时数据区（重定向 db.DB_PATH，模式同 test_storage）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mem_v2_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "agent_data.db"
        # 迁移要枚举 sessions.workspace 与 users.id，所以临时库得先有 schema。
        # 注意 init_db 自身会跑迁移 26——此刻数据区还不存在，探针让它整体跳过
        # （正是 test_noop_when_no_memory_root 要锁的性质）。
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig

    # ---------- 造数据 ----------

    def make_ws(self, name="proj") -> Path:
        ws = self.tmp / name
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def git_ws(self, name="repo", remote="git@github.com:me/proj.git") -> Path:
        ws = self.make_ws(name)
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote],
                       cwd=ws, check=True)
        return ws

    def seed_bucket(self, token: str, files: dict, index_lines: list[str]):
        """在旧布局 data/memory/<token>/ 下造一个桶。"""
        d = memory._memory_root() / token
        d.mkdir(parents=True, exist_ok=True)
        for fname, body in files.items():
            (d / fname).write_text(body, encoding="utf-8")
        (d / INDEX_NAME).write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        return d

    def add_session(self, ws, sid="s1", uid=1):
        """登记一条引用该工作区的会话——迁移只从 sessions.workspace 枚举
        真实工作区，不扫目录。不建这行，桶就会被（正确地）当成无主而跳过。"""
        with db._conn() as conn:
            conn.execute("INSERT INTO sessions(id, title, created, updated, "
                         "user_id, workspace) VALUES(?,?,0,0,?,?)",
                         (sid, "t", uid, str(ws)))

    def memory_file(self, fname: str, desc: str, body="正文") -> str:
        return render_memory_file(fname[:-3], desc, "project", body)


# ---------------------------------------------------------------------------
# A. 桥接前缀顺序 —— 设计里点名"最容易出错的一处"
# ---------------------------------------------------------------------------

class TestBridgePrefixOrder(V2Base):

    def _agent_bridge_roots(self):
        from agent import Agent
        ws = self.make_ws()
        ag = Agent(llm=None, verbose=False, workspace=str(ws),
                   session_id="s-br", user_id=51)
        return ag._bridge_roots(), ws

    def _app_bridge_roots(self):
        import app as app_mod
        ws = self.make_ws()
        self.add_session(ws, sid="s-br", uid=51)
        return app_mod._bridge_roots_for("s-br")

    def test_both_prefixes_resolve_regardless_of_registration_order(self):
        """两层的文件各归各处，**与注册顺序无关**。

        设计文档原本把"长前缀必须写前面否则被吞"列为最高风险项。与其靠每个
        调用方都记得排序，不如在 _resolve 里按前缀长度降序匹配，把这个隐藏
        依赖消掉——这条用例把两种注册顺序都跑一遍，锁住"顺序无关"。
        """
        from code_tools import _resolve
        ws = self.make_ws()
        personal = (self.tmp / "u" / "1").resolve()
        project = (self.tmp / "p" / "x").resolve()
        personal.mkdir(parents=True)
        project.mkdir(parents=True)
        for bridge in (
            {".agent-memory/project": project, ".agent-memory": personal},   # 长在前
            {".agent-memory": personal, ".agent-memory/project": project},   # 短在前
        ):
            self.assertEqual(_resolve(".agent-memory/x.md", ws, bridge),
                             personal / "x.md")
            self.assertEqual(_resolve(".agent-memory/project/y.md", ws, bridge),
                             project / "y.md")
            # 深一层也不会被吞
            self.assertEqual(_resolve(".agent-memory/project/sub/z.md", ws, bridge),
                             project / "sub" / "z.md")

    def test_agent_and_gate_bridge_tables_agree(self):
        """Agent 与权限闸门的两张桥接表必须同源。

        闸门按这张表判"写入是否落在放行的桥接区"，Agent 按它解析实际路径；
        两处不一致 = 闸门放行的路径与实际落点不同（放行了一个写不到的地方，
        或实际写到了没放行的地方）。
        """
        from unittest import mock
        import agent as agent_mod
        import app as app_mod
        from code_tools import BRIDGE_MEMORY, BRIDGE_MEMORY_PROJECT
        a, _ = self._agent_bridge_roots()
        b = self._app_bridge_roots()
        self.assertEqual(set(a), set(b), "两张表的键集合必须一致")
        for k in (BRIDGE_MEMORY, BRIDGE_MEMORY_PROJECT):
            self.assertEqual(Path(a[k]), Path(b[k]), f"{k} 的落点必须一致")


# ---------------------------------------------------------------------------
# B. 项目身份三级解析
# ---------------------------------------------------------------------------

class TestProjectIdentity(V2Base):

    def test_marker_wins_over_remote(self):
        ws = self.git_ws()
        (ws / memory.PROJECT_ID_MARKER).write_text("my-project\n", encoding="utf-8")
        self.assertEqual(project_identity(ws), ("my-project", "marker"))

    def test_marker_in_git_dir_also_works(self):
        ws = self.git_ws()
        (ws / ".git" / memory.PROJECT_ID_MARKER).write_text("from-gitdir\n",
                                                            encoding="utf-8")
        self.assertEqual(project_identity(ws), ("from-gitdir", "marker"))

    def test_illegal_marker_falls_through_and_cannot_escape(self):
        """非法 marker（含 .. / 绝对路径）不得逃逸出 projects/，也不得抛异常。"""
        ws = self.git_ws()
        for bad in ("../../etc", "/etc/passwd", "a/b", "..", ""):
            (ws / memory.PROJECT_ID_MARKER).write_text(bad, encoding="utf-8")
            pid, source = project_identity(ws)
            self.assertNotEqual(source, "marker", f"非法值不该被采纳: {bad!r}")
            self.assertNotIn("/", pid or "")
            self.assertNotIn("..", pid or "")

    def test_same_remote_different_paths_share_identity(self):
        """改名/搬目录不失忆的验收：不同路径、同 remote → 同一 project_id。"""
        a = self.git_ws("repo-a")
        b = self.git_ws("repo-b", remote="git@github.com:me/proj.git")
        ida, src_a = project_identity(a)
        idb, src_b = project_identity(b)
        self.assertEqual(src_a, "git-remote")
        self.assertEqual(src_b, "git-remote")
        self.assertEqual(ida, idb, "同一仓库的两个 clone 必须共享记忆桶")

    def test_non_git_falls_back_to_path(self):
        ws = self.make_ws("scratch")
        pid, source = project_identity(ws)
        self.assertEqual(source, "path")
        self.assertEqual(pid, workspace_token(ws))

    def test_empty_workspace_has_no_project(self):
        self.assertEqual(project_identity(None), (None, "none"))
        self.assertEqual(project_identity(""), (None, "none"))

    def test_two_remotes_is_deterministic(self):
        """多 remote（实测 mai_portal 有 github + gitlab）：取名字排序第一个，
        避免"这次解析到 github、下次解析到 gitlab"而分裂成两个桶。"""
        ws = self.make_ws("multi")
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:me/a.git"],
                       cwd=ws, check=True)
        subprocess.run(["git", "remote", "add", "github", "git@github.com:me/b.git"],
                       cwd=ws, check=True)
        first = project_identity(ws)
        for _ in range(3):
            self.assertEqual(project_identity(ws), first)


class TestNormalizeGitRemote(unittest.TestCase):

    def test_all_spellings_of_one_repo_normalize_together(self):
        """同仓库的 ssh / https / 带不带 .git 必须归一到同一个值，
        否则同一个项目会因 clone 方式不同而分裂成两个记忆桶。"""
        spellings = [
            "git@github.com:zxh0305/coding-agent.git",
            "https://github.com/zxh0305/coding-agent.git",
            "https://github.com/zxh0305/coding-agent",
            "ssh://git@github.com/zxh0305/coding-agent.git",
            "git@github.com:zxh0305/coding-agent",
        ]
        got = {normalize_git_remote(s) for s in spellings}
        self.assertEqual(got, {"github.com/zxh0305/coding-agent"})

    def test_distinct_repos_stay_distinct(self):
        self.assertNotEqual(normalize_git_remote("git@github.com:me/a.git"),
                            normalize_git_remote("git@github.com:me/b.git"))

    def test_garbage_returns_none(self):
        for bad in ("", "   ", None, 123):
            self.assertIsNone(normalize_git_remote(bad))


# ---------------------------------------------------------------------------
# C. 布局迁移
# ---------------------------------------------------------------------------

class TestMigrateLayout(V2Base):

    def _seed_realistic(self):
        """造一个接近真实的旧布局：一个项目桶（有 sessions 引用）+ 用户。"""
        ws = self.tmp / "myproj"
        ws.mkdir(parents=True)
        token = workspace_token(ws)
        idx = ["- [a.md](a.md) — 记忆A", "- [b.md](b.md) — 记忆B"]
        self.seed_bucket(token, {"a.md": self.memory_file("a.md", "记忆A"),
                                 "b.md": self.memory_file("b.md", "记忆B")}, idx)
        self.add_session(ws, sid="s-main")
        return ws, token

    def test_moves_files_and_merges_index(self):
        ws, token = self._seed_realistic()
        report = memory.migrate_memory_layout()
        self.assertFalse(report["skipped"])
        dst = memory.memory_dir_for_project(ws)
        self.assertTrue((dst / "a.md").exists())
        self.assertTrue((dst / "b.md").exists())
        idx = (dst / INDEX_NAME).read_text(encoding="utf-8")
        self.assertIn("a.md", idx)
        self.assertIn("b.md", idx)
        # 旧桶已清走
        self.assertFalse((memory._memory_root() / token).exists())

    def test_is_idempotent(self):
        self._seed_realistic()
        r1 = memory.migrate_memory_layout()
        self.assertFalse(r1["skipped"])
        snapshot = sorted(p.name for p in memory._memory_root().iterdir())
        r2 = memory.migrate_memory_layout()
        self.assertTrue(r2["skipped"], "第二次必须跳过")
        self.assertEqual(sorted(p.name for p in memory._memory_root().iterdir()),
                         snapshot, "第二次不得改变任何东西")

    def test_creates_backup_before_moving(self):
        ws, token = self._seed_realistic()
        report = memory.migrate_memory_layout()
        backup = Path(report["backup"])
        self.assertTrue(backup.is_dir(), "必须留下备份")
        # 备份里应有迁移前的旧桶原貌
        old = backup / "memory" / token
        self.assertTrue((old / "a.md").exists())
        self.assertTrue((old / INDEX_NAME).exists())

    def test_backup_failure_aborts_without_touching_data(self):
        """备份失败 → 一个文件都不动（半个备份比不迁移更糟）。"""
        ws, token = self._seed_realistic()
        before = sorted(p.name for p in (memory._memory_root() / token).iterdir())
        with mock.patch("memory.shutil.copytree", side_effect=OSError("磁盘满")):
            report = memory.migrate_memory_layout()
        self.assertTrue(report["skipped"])
        self.assertIn("备份失败", report["reason"])
        self.assertTrue((memory._memory_root() / token).is_dir(), "旧桶必须原样保留")
        self.assertEqual(sorted(p.name for p in (memory._memory_root() / token).iterdir()),
                         before)

    def test_noop_when_no_memory_root(self):
        """数据区不存在（全新安装 / 临时库）→ 不建任何目录，直接跳过。

        这条保证重定向 DB_PATH 的其它测试不会凭空多出 projects/ users/。
        """
        report = memory.migrate_memory_layout()
        self.assertTrue(report["skipped"])
        self.assertFalse((memory._memory_root() / PROJECTS_SUBDIR).exists())

    def test_does_not_write_owner_field(self):
        """迁移【不写 owner】——74 条原样留作 project，由用户在 UI 里挑。

        误判成 user 会污染所有项目的上下文，且是静默不可逆方向，故不由迁移代劳。
        """
        ws, token = self._seed_realistic()
        before = {p.name: p.read_bytes()
                  for p in (memory._memory_root() / token).glob("*.md")}
        memory.migrate_memory_layout()
        dst = memory.memory_dir_for_project(ws)
        for name, content in before.items():
            self.assertEqual((dst / name).read_bytes(), content,
                             f"{name} 内容被改写了——迁移不该改动记忆正文")
            self.assertNotIn(b"owner:", content)

    def test_creates_user_buckets(self):
        # 数据区必须已存在（有记忆才有迁移可言）——否则整体跳过是正确行为，
        # 见 test_noop_when_no_memory_root。
        memory._memory_root().mkdir(parents=True, exist_ok=True)
        with db._conn() as conn:
            conn.execute("INSERT INTO users(username, password_hash, created) "
                         "VALUES('u1','x',0)")
            conn.execute("INSERT INTO users(username, password_hash, created) "
                         "VALUES('u2','x',0)")
        report = memory.migrate_memory_layout()
        self.assertEqual(report["users"], 2)
        for uid in db.all_user_ids():
            self.assertTrue((memory._memory_root() / USERS_SUBDIR / str(uid)).is_dir())

    def test_same_project_two_old_buckets_merge_without_losing_entries(self):
        """两个旧桶解析到同一项目（同 remote 的两个 clone）：索引行合并、
        文件不丢、同名冲突保留已有那份并记录。

        这是设计 §8.2 "若目标已存在则合并索引行，不覆盖文件"的验收。
        """
        ws1 = self.git_ws("clone1")
        ws2 = self.git_ws("clone2", remote="git@github.com:me/proj.git")
        self.seed_bucket(workspace_token(ws1),
                         {"only1.md": self.memory_file("only1.md", "仅第一份")},
                         ["- [only1.md](only1.md) — 仅第一份"])
        self.seed_bucket(workspace_token(ws2),
                         {"only2.md": self.memory_file("only2.md", "仅第二份")},
                         ["- [only2.md](only2.md) — 仅第二份"])
        self.add_session(ws1, sid="c1")
        self.add_session(ws2, sid="c2")
        report = memory.migrate_memory_layout()
        dst = memory.memory_dir_for_project(ws1)
        idx = (dst / INDEX_NAME).read_text(encoding="utf-8")
        self.assertIn("only1.md", idx)
        self.assertIn("only2.md", idx, "两份的索引行都必须保住，不能互相覆盖")
        self.assertTrue((dst / "only1.md").exists())
        self.assertTrue((dst / "only2.md").exists())

    def test_unknown_workspace_bucket_is_left_alone(self):
        """旧桶不对应任何现存 workspace → 迁移不碰它（不瞎猜、不误搬）。

        只在 sessions.workspace 的真实取值里枚举，绝不 iterdir 盲扫——
        盲扫正是 81 个测试垃圾桶的成因。
        """
        orphan = memory._memory_root() / "deadbeef1234"
        orphan.mkdir(parents=True)
        (orphan / "stray.md").write_text("x", encoding="utf-8")
        memory.migrate_memory_layout()
        self.assertTrue((orphan / "stray.md").exists(), "无主桶不该被搬走或删除")


# ---------------------------------------------------------------------------
# D. frontmatter 向后兼容（Step 1 只预留字段，不写入）
# ---------------------------------------------------------------------------

class TestFrontmatterCompat(unittest.TestCase):

    def test_three_field_render_is_byte_identical(self):
        """不传新参数时，渲染结果必须与旧版逐字节相同。"""
        got = render_frontmatter("x", "描述", "user")
        self.assertEqual(got, "---\nname: x\ndescription: 描述\nmetadata:\n"
                              "  type: user\n---\n")

    def test_legacy_file_parses_to_exactly_three_keys(self):
        text = render_frontmatter("x", "描述", "user") + "正文\n"
        meta, body = parse_frontmatter(text)
        self.assertEqual(set(meta), {"name", "description", "type"})
        self.assertEqual(body, "正文")

    def test_new_fields_parse_as_superset(self):
        text = render_memory_file("x", "描述", "user", "b",
                                  owner="user", scope="global",
                                  updated="2026-10-14")
        meta, _ = parse_frontmatter(text)
        self.assertEqual(meta["owner"], "user")
        self.assertEqual(meta["scope"], "global")
        self.assertEqual(meta["updated"], "2026-10-14")
        self.assertEqual(meta["type"], "user", "老字段不受影响")

    def test_missing_type_still_rejected(self):
        """三字段缺一不可这条没被放宽（损坏文件仍静默跳过）。"""
        text = "---\nname: x\ndescription: d\nmetadata:\n  owner: user\n---\nb"
        self.assertIsNone(parse_frontmatter(text)[0])

    def test_round_trip_stable_with_and_without_new_fields(self):
        a = parse_frontmatter(render_memory_file("x", "描述", "user", "b"))[0]
        b = parse_frontmatter(render_memory_file("x", "描述", "user", "b",
                                                 owner="user", scope="global"))[0]
        for k in a:
            self.assertEqual(a[k], b[k], f"{k} 在新旧渲染之间必须一致")


# ---------------------------------------------------------------------------
# E. 目录函数与 memory_dir 过渡期兼容守卫
# ---------------------------------------------------------------------------

class TestDirectoryFns(V2Base):

    def test_memory_dir_points_at_project_layer(self):
        """memory_dir（注入/提取/桥接的唯一落点）必须指向项目层。

        这条是 Step 1 里最容易被忽略、后果最重的接线：迁移把文件搬进
        projects/ 之后，若 memory_dir 仍指向旧扁平桶，注入会读到空目录，
        模型从此看不到任何记忆——而且不报错。迁移与切换是一个整体，不可拆。
        """
        ws = self.make_ws()
        self.assertEqual(memory_dir(ws), memory_dir_for_project(ws))
        self.assertIn(PROJECTS_SUBDIR, str(memory_dir(ws)))
        self.assertEqual(memory_dir(None), memory._FALLBACK_MEM_DIR)

    def test_injection_follows_migration(self):
        """端到端：迁移后 memory_dir 能读到搬过去的记忆（不再"（暂无记忆）"）。"""
        ws = self.tmp / "myproj"
        ws.mkdir(parents=True)
        token = workspace_token(ws)
        self.seed_bucket(token, {"a.md": self.memory_file("a.md", "记忆A")},
                         ["- [a.md](a.md) — 记忆A"])
        self.add_session(ws, sid="s-inj")
        self.assertIn("暂无记忆", memory.memory_index_block(memory_dir(ws)))
        memory.migrate_memory_layout()
        block = memory.memory_index_block(memory_dir(ws))
        self.assertIn("a.md", block, "迁移后注入必须能看到搬过去的记忆")
        self.assertNotIn("暂无记忆", block)

    def test_old_flat_bucket_no_longer_used(self):
        """旧扁平路径不再是落点（Step 2 会彻底删掉 workspace_token 的分桶职责）。"""
        ws = self.git_ws()
        self.assertNotEqual(memory_dir(ws),
                            memory._memory_root() / workspace_token(ws))

    def test_project_and_user_dirs_never_collide(self):
        ws = self.git_ws()
        p = memory_dir_for_project(ws)
        u = memory_dir_for_user(1)
        self.assertNotEqual(p, u)
        self.assertIn(PROJECTS_SUBDIR, str(p))
        self.assertIn(USERS_SUBDIR, str(u))

    def test_project_dir_resolution_is_lazy(self):
        """只解析路径不建目录（与 memory_dir 同一惰性契约）。"""
        ws = self.make_ws()
        p = memory_dir_for_project(ws)
        self.assertFalse(p.exists())

    def test_none_inputs_fall_back(self):
        self.assertEqual(memory_dir_for_project(None), memory._FALLBACK_MEM_DIR)
        self.assertEqual(memory_dir_for_user(None), memory._FALLBACK_MEM_DIR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
