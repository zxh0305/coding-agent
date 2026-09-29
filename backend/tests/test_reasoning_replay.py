"""
思考模型 reasoning 回传开关（cd backend && python3 -m unittest tests.test_reasoning_replay -v）
================================================================================================

DeepSeek thinking 系列的 OpenAI 兼容协议：带 tools 的请求必须把历史 assistant
消息的 reasoning_content 原样带回，缺失直接 400；不带 tools 时服务端忽略/丢弃。
本项目默认「推理只做实时展示、不进历史」，对支持该协议的供应商须按开关把推理
内容写回消息历史。测试锁定：

  1. 开关关闭（默认）：流式收到的 reasoning_content 不进最终消息（原行为）；
  2. 开关开启：reasoning_content 随 ("message", ...) 事件进历史，下一轮请求
     经 _clean_outgoing（只剥下划线前缀）原样带回；
  3. create_client 透传开关；供应商行（providers.reasoning_replay）落库往返。

流式响应用内联 SSE 行喂给假 _post，不发网络请求。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import db
from llm_client import OpenAIChatClient, create_client

_SSE_LINES = [
    b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x83\xb3"}}]}\n\n',  # 想
    b'data: {"choices":[{"delta":{"content":"\xe7\xad\x94\xe6\xa1\x88"}}]}\n\n',  # 答案
    b'data: [DONE]\n\n',
]


class _FakeResp:
    """最小 SSE 响应桩：可迭代（逐行）+ 上下文管理器 + close。"""

    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _final_message(client):
    """消费 chat_stream，返回最后的 ("message", ...) 消息。"""
    client._post = lambda payload, cancel=None: _FakeResp(_SSE_LINES)
    message = None
    for kind, payload in client.chat_stream([{"role": "user", "content": "hi"}]):
        if kind == "message":
            message = payload
    return message


class TestReplaySwitch(unittest.TestCase):

    def test_default_off_keeps_reasoning_out_of_history(self):
        """默认关闭：推理只做实时展示（reasoning_delta），不进最终消息。"""
        client = OpenAIChatClient(api_key="k", base_url="https://x/v1", model="m")
        self.assertFalse(client.reasoning_replay)
        message = _final_message(client)
        self.assertNotIn("reasoning_content", message)
        self.assertEqual(message["content"], "答案")

    def test_on_attaches_reasoning_for_replay(self):
        """开启：推理内容随最终消息进历史——下一轮请求经 _clean_outgoing 原样带回
        （reasoning_content 不带下划线前缀，不会被剥离）。"""
        client = OpenAIChatClient(api_key="k", base_url="https://x/v1", model="m",
                                  reasoning_replay=True)
        message = _final_message(client)
        self.assertEqual(message["reasoning_content"], "想")
        self.assertEqual(message["content"], "答案")

    def test_create_client_passthrough(self):
        """工厂函数透传开关；Anthropic 协议路径不适用（思考块须签名往返，不接此开关）。"""
        oa = create_client("openai", api_key="k", base_url="https://x/v1", model="m",
                           reasoning_replay=True)
        self.assertTrue(oa.reasoning_replay)
        anthropic = create_client("anthropic", api_key="k", base_url="https://x/v1",
                                  model="m", reasoning_replay=True)
        self.assertFalse(hasattr(anthropic, "reasoning_replay"))


class TestProviderFlagPersistence(unittest.TestCase):
    """providers.reasoning_replay 列（迁移 24）的落库往返。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rr_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig

    def test_roundtrip_and_none_keeps_value(self):
        db.upsert_provider("p1", "DeepSeek", "https://api.deepseek.com/v1", "key",
                           True, reasoning_replay=True)
        self.assertTrue(db.get_provider("p1")["reasoning_replay"])
        db.upsert_provider("p1", "DeepSeek", "https://api.deepseek.com/v1", "key",
                           True, reasoning_replay=False)
        self.assertFalse(db.get_provider("p1")["reasoning_replay"])
        # None = 保持原值（前端编辑不带该字段时不误清）
        db.upsert_provider("p1", "DeepSeek", "https://api.deepseek.com/v1", "key", True)
        self.assertFalse(db.get_provider("p1")["reasoning_replay"])
        # 未传时新建的供应商默认关闭
        db.upsert_provider("p2", "GLM", "https://open.bigmodel.cn/api/paas/v4/", "key", True)
        self.assertFalse(db.get_provider("p2")["reasoning_replay"])

    def test_migration_24_adds_column_to_old_db(self):
        """老库升级：user_version=23 的库启动后自动补上 reasoning_replay 列，数据仍在。"""
        import sqlite3
        # setUp 的 init_db 已按最终形态建表，老库模拟换一个全新库文件
        old_db = Path(self.tmp) / "old.db"
        conn = sqlite3.connect(old_db)
        conn.execute("CREATE TABLE providers(id TEXT PRIMARY KEY, name TEXT, base_url TEXT, "
                     "api_format TEXT DEFAULT 'openai', api_key TEXT DEFAULT '', "
                     "enabled INTEGER DEFAULT 1, context_window INTEGER DEFAULT 128000, created REAL)")
        conn.execute("INSERT INTO providers(id, name, base_url) VALUES('old', '旧供应商', 'https://x/v1')")
        conn.execute("PRAGMA user_version = 23")
        conn.commit()
        conn.close()
        db.DB_PATH = old_db  # tearDown 会恢复原始路径
        db.init_db()  # 应用迁移 24
        self.assertIn("reasoning_replay", db._table_columns(db._conn(), "providers"))
        row = db.get_provider("old")
        self.assertIsNotNone(row)
        self.assertFalse(row["reasoning_replay"])  # 旧行默认关闭


if __name__ == "__main__":
    unittest.main(verbosity=2)
