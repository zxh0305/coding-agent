"""backend/crash_tail.py 的单测：崩溃尾配平的边界条件。

这类修补写错比不写更危险——补错位置会改坏正常历史，补错语义会让模型
把已执行的写操作再跑一遍。所以测试重点在「不该动的时候坚决不动」。
"""

import unittest

import crash_tail as ct


def asst(content=None, calls=None):
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = calls
    return m


def call(cid, name="write_file"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


def tool(cid, content="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


class BalanceTailTest(unittest.TestCase):
    def test_empty_history(self):
        self.assertEqual(ct.balance_tail([]), 0)

    def test_no_tool_calls_untouched(self):
        hist = [{"role": "user", "content": "hi"}, asst("答")]
        before = [dict(m) for m in hist]
        self.assertEqual(ct.balance_tail(hist), 0)
        self.assertEqual(hist, before)

    def test_fully_settled_untouched(self):
        hist = [asst(None, [call("c1")]), tool("c1")]
        self.assertEqual(ct.balance_tail(hist), 0)
        self.assertEqual(len(hist), 2)

    def test_crash_tail_gets_synthetic_result(self):
        """典型崩溃：派发了两个工具，只结算了一个。"""
        hist = [{"role": "user", "content": "改文件"},
                asst(None, [call("c1"), call("c2", "run_bash")]),
                tool("c1")]
        self.assertEqual(ct.balance_tail(hist), 1)
        self.assertEqual(len(hist), 4)
        tail = hist[-1]
        self.assertEqual(tail["role"], "tool")
        self.assertEqual(tail["tool_call_id"], "c2")
        self.assertTrue(tail["_synthetic"])
        self.assertTrue(tail["_crash_balanced"])
        # 语义档位：默认保守（结果未知），文案要告诉模型别盲目重跑
        self.assertIn("结果未知", tail["content"])
        self.assertIn("run_bash", tail["content"])
        self.assertIn("不要盲目重跑", tail["content"])

    def test_idempotent(self):
        hist = [asst(None, [call("c1"), call("c2")]), tool("c1")]
        self.assertEqual(len(hist), 2)
        self.assertEqual(ct.balance_tail(hist), 1)
        self.assertEqual(len(hist), 3)               # 补了 c2 一条
        self.assertEqual(ct.balance_tail(hist), 0)   # 第二次不该再补
        self.assertEqual(len(hist), 3)

    def test_none_started_variant_mentions_safe_retry(self):
        hist = [asst(None, [call("c1")])]
        ct.balance_tail(hist, default_kind=ct.TOOL_NOT_STARTED)
        self.assertIn("没有执行", hist[-1]["content"])
        self.assertIn("可以安全地重新调用", hist[-1]["content"])

    def test_turn_already_followed_by_user_is_left_alone(self):
        """后面已经有新的用户输入 → 那一轮是被正常推进过的，不是崩溃尾。"""
        hist = [asst(None, [call("c1")]),
                {"role": "user", "content": "算了，继续"}]
        self.assertEqual(ct.balance_tail(hist), 0)

    def test_handles_legacy_tool_call_shape(self):
        """老数据里 name 可能直接在 call 上而不是 function 里。"""
        hist = [asst(None, [{"id": "c9", "name": "read_file"}])]
        self.assertEqual(ct.balance_tail(hist), 1)
        self.assertIn("read_file", hist[-1]["content"])

    def test_calls_without_id_are_ignored(self):
        """没有 id 的调用无法配对，不能凭空造结果（会造成新的悬空）。"""
        hist = [asst(None, [{"function": {"name": "x"}}])]
        self.assertEqual(ct.balance_tail(hist), 0)

    def test_only_last_assistant_with_calls_is_considered(self):
        """中间轮次的缺失不在这里猜修（猜错会改坏正常历史）。"""
        hist = [asst(None, [call("old")]),          # 中间：缺结果
                {"role": "user", "content": "再来"},
                asst(None, [call("c1")]), tool("c1"),
                asst("好了")]
        self.assertEqual(ct.balance_tail(hist), 0)

    def test_synthetic_text_has_both_variants(self):
        for kind in (ct.TOOL_NOT_STARTED, ct.TOOL_OUTCOME_UNKNOWN):
            text = ct.synthetic_result("apply_patch", kind)
            self.assertIn("apply_patch", text)
            self.assertGreater(len(text), 20)


if __name__ == "__main__":
    unittest.main()
