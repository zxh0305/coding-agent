"""
用量聚合 TTL 缓存的单元测试（cd backend && python3 -m unittest tests.test_usage_cache -v）
======================================================================================

压测定位（50 万行 message_usage，2026-10-08）：usage_summary 单次 P50 61ms，
并发 32 时 P50 407ms / P99 638ms——瓶颈是 N 个冷连接同时全表聚合的争用。
方案 A：读走进程级 TTL 缓存（_USAGE_TTL=5s），message_usage 全部写入口主动失效。

覆盖：
1. 命中与失效：TTL 内命中（同一对象）；record_usage 后立即失效（不等 TTL）；
2. 键隔离：days / provider_id / model 各参数组合独立缓存，互不串值；
3. TTL 过期：快进时钟后重新查询（不再命中旧对象）；
4. 失效口全覆盖：delete_session（整删）、cleanup_orphans（孤儿清理）；
5. 实例隔离：init_db / 新导入实例的缓存为空（换库不串值，模拟独立进程）；
6. 已知边界：另一实例写库后，本实例 TTL 内会短暂看到旧值（正式部署单实例，可接受）。
"""

import importlib.util
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db


def fresh_db_module():
    """以新模块对象重新加载 db.py：模块级 _usage_cache 归零，模拟独立服务进程。"""
    spec = importlib.util.spec_from_file_location("db_fresh_under_test", Path(db.__file__))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = db.DB_PATH
    return mod


class UsageCacheBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage_cache_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db._usage_cache.clear()
        self.addCleanup(db._usage_cache.clear)
        db.init_db()
        db.create_session("s1", 1)

    def tearDown(self):
        db.DB_PATH = self._orig

    def seed(self):
        db.record_usage("s1", "turn", {"usage": {
            "prompt_tokens": 100, "completion_tokens": 10}}, "prov", "glm-4")


class TestCacheSemantics(UsageCacheBase):
    def test_hit_within_ttl_same_object(self):
        self.seed()
        a = db.usage_summary()
        b = db.usage_summary()
        self.assertEqual(a, b)
        self.assertIs(a, b)  # 同一对象 = 命中缓存，没打库

    def test_record_usage_invalidates_immediately(self):
        self.seed()
        a = db.usage_summary()
        db.record_usage("s1", "subagent", {"usage": {"prompt_tokens": 500}})
        b = db.usage_summary()
        # turn 行带 provider、subagent 行 provider 为空 → 两个分组，跨组求和验证
        self.assertEqual(sum(r["prompt_tokens"] for r in b), 600)
        self.assertEqual(sum(r["turns"] for r in b), 1)  # turns 只数主回合行
        self.assertIsNot(a, b)

    def test_keys_isolated_by_args(self):
        self.seed()
        a_all = db.usage_summary()
        a_7d = db.usage_summary(days=7)
        self.assertEqual(a_all, a_7d)
        self.assertIsNot(a_all, a_7d)  # 不同键各自缓存
        sr_all = db.usage_session_rows()
        sr_prov = db.usage_session_rows(provider_id="prov")
        self.assertEqual(sr_all, sr_prov)
        self.assertIsNot(sr_all, sr_prov)

    def test_ttl_expiry_requeries(self):
        self.seed()
        a = db.usage_summary()
        self.assertIs(db.usage_summary(), a)
        real = db.time.time()
        with mock.patch.object(db.time, "time", return_value=real + 10):  # 越过 5s TTL
            b = db.usage_summary()
        self.assertIsNot(a, b)
        self.assertEqual(a, b)  # 数据没变，仅缓存过期

    def test_delete_session_invalidates(self):
        self.seed()
        db.usage_summary()
        db.delete_session("s1")
        self.assertEqual(db.usage_summary(), [])

    def test_cleanup_orphans_invalidates(self):
        self.seed()
        a = db.usage_summary()
        with sqlite3.connect(db.DB_PATH) as c:  # 绕过 db 层直接删会话行，制造孤儿
            c.execute("DELETE FROM sessions WHERE id='s1'")
        counts = db.cleanup_orphans()
        self.assertGreaterEqual(counts["message_usage"], 1)
        b = db.usage_summary()
        self.assertEqual(b, [])
        self.assertIsNot(a, b)


class TestInstanceIsolation(UsageCacheBase):
    def test_fresh_module_sees_same_data(self):
        self.seed()
        a = db.usage_summary()
        f = fresh_db_module()
        b = f.usage_summary()
        self.assertEqual(a, b)
        self.assertIsNot(a, b)  # 独立实例、独立缓存

    def test_init_db_resets_cache(self):
        # 测试框架靠换 DB_PATH + init_db 隔离用例：缓存必须随之作废
        self.seed()
        a = db.usage_summary()
        db.init_db()
        self.assertIsNone(db._usage_cache.get(("summary", None)))

    def test_known_edge_other_instance_write_within_ttl(self):
        # 另一实例写入后，本实例 TTL 内短暂看到旧值——正式部署单实例，可接受；
        # 此测试把这个边界钉住：若将来改成多实例，这里就该失败并触发重设计。
        self.seed()
        stale = db.usage_summary()
        f = fresh_db_module()
        f.record_usage("s1", "subagent", {"usage": {"prompt_tokens": 999}})
        total = lambda rows: sum(r["prompt_tokens"] for r in rows)
        self.assertEqual(total(db.usage_summary()), 100)   # TTL 内旧值
        self.assertEqual(total(f.usage_summary()), 1099)   # 写入方自己可见


if __name__ == "__main__":
    unittest.main()
