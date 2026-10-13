"""
流终止判定：区分「模型说完了」与「流被掐断了」
（cd backend && python3 -m unittest tests.test_stream_truncation -v）
================================================================================

动机（真实缺口）：读取循环只在收到显式终止标记（OpenAI 的 `data: [DONE]`、
Anthropic 的 `message_stop`）时 break。若对端【干净关闭连接】而不发终止标记，
`for raw in resp:` 会正常结束——与"模型正常说完"在代码上无从区分，半截回答
被当成完整回答 append 进 history 并永久落库，用户与模型都无从察觉。此前
`finish_reason` 从未被检查，撞 max_tokens（"length"）同样静默。

本文件锁死三件事：
  1. _classify_stream_end 的判定表（宁可漏判也不误判：假阳性会把正常回答
     判成中断，比漏判更伤体验）；
  2. 两个适配器在"无终止标记"时的处置：纯文本 → 打 _truncated 标记保留内容；
     已攒出工具调用 → 抛 RuntimeError（绝不拿半截参数去执行）；
  3. 正常路径（有 [DONE] / message_stop）永不误报——含"服务商只给
     finish_reason、不发 [DONE]"的兼容情形。
"""

import io
import json
import unittest

import llm_client
from llm_client import (AnthropicMessagesClient, OpenAIChatClient,
                        _classify_stream_end)


class ClassifyStreamEndTest(unittest.TestCase):
    """判定表：完整 / length 截断 / unknown 中断。"""

    def test_terminator_means_complete(self):
        """见到 [DONE] / message_stop：完整，哪怕没有 finish_reason。"""
        self.assertIsNone(_classify_stream_end(
            saw_terminator=True, finish_reasons=[], cancelled=False))

    def test_cancelled_means_complete(self):
        """用户主动停止：带着部分内容正常收尾，不算截断。"""
        self.assertIsNone(_classify_stream_end(
            saw_terminator=False, finish_reasons=[], cancelled=True))

    def test_no_terminator_no_finish_reason_is_unknown(self):
        """两者都没有 → 疑似连接被掐断。"""
        self.assertEqual(_classify_stream_end(
            saw_terminator=False, finish_reasons=[], cancelled=False), "unknown")

    def test_finish_reason_without_done_is_complete(self):
        """部分服务商不发 [DONE] 但给 finish_reason=stop——不能误判成中断。"""
        self.assertIsNone(_classify_stream_end(
            saw_terminator=False, finish_reasons=["stop"], cancelled=False))

    def test_tool_calls_finish_reason_is_complete(self):
        """finish_reason=tool_calls（正常请求调用工具）也是完整结束。"""
        self.assertIsNone(_classify_stream_end(
            saw_terminator=False, finish_reasons=["tool_calls"], cancelled=False))

    def test_length_is_truncation(self):
        """撞 max_tokens → length 截断（内容可用但不完整）。"""
        self.assertEqual(_classify_stream_end(
            saw_terminator=False, finish_reasons=["length"], cancelled=False), "length")

    def test_length_wins_even_with_done(self):
        """给了 [DONE] 但 finish_reason=length：仍按截断如实标注。"""
        self.assertEqual(_classify_stream_end(
            saw_terminator=True, finish_reasons=["length"], cancelled=False), "length")


class _FakeStreamResp:
    """假 SSE 响应：逐行迭代 + 上下文管理器（与 test_retry.FakeResp 同形）。"""

    def __init__(self, lines):
        self._lines = [ln.encode("utf-8") for ln in lines]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def openai_lines(chunks: list[dict], done: bool) -> list[str]:
    """把 chunk 列表编成 SSE 行；done=True 时补上 `data: [DONE]`。"""
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    if done:
        lines.append("data: [DONE]")
    return lines


def chunk(content=None, finish_reason=None, tool_calls=None) -> dict:
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def drain(gen):
    """把流跑到结束，返回 (message, 异常)。"""
    msg = None
    try:
        for kind, payload in gen:
            if kind == "message":
                msg = payload
    except Exception as e:  # noqa: BLE001 —— 测试要拿到异常本身做断言
        return None, e
    return msg, None


class OpenAITruncationTest(unittest.TestCase):

    def _client(self):
        c = OpenAIChatClient(api_key="k", base_url="http://x", model="m")
        return c

    def _stream(self, lines):
        c = self._client()
        resp = _FakeStreamResp(lines)
        with mock_urlopen(resp):
            return drain(c.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    def test_normal_stream_has_no_truncation_mark(self):
        """正常结束（有 [DONE]）：message 不带 _truncated。"""
        msg, err = self._stream(openai_lines([chunk("你好")], done=True))
        self.assertIsNone(err)
        self.assertEqual(msg["content"], "你好")
        self.assertNotIn("_truncated", msg)

    def test_missing_done_with_text_marks_truncated(self):
        """无终止标记 + 纯文本：保留内容，但打上 _truncated=unknown。"""
        msg, err = self._stream(openai_lines([chunk("半截回")], done=False))
        self.assertIsNone(err, "纯文本截断不该抛异常——内容仍有用")
        self.assertEqual(msg["content"], "半截回")
        self.assertEqual(msg["_truncated"], "unknown")

    def test_missing_done_with_tool_call_raises(self):
        """无终止标记 + 已攒出工具调用：抛错，绝不执行半截参数。"""
        tc = [{"index": 0, "id": "call_1", "type": "function",
               "function": {"name": "run_bash", "arguments": '{"command": "rm -rf /'}}]
        msg, err = self._stream(openai_lines([chunk(tool_calls=tc)], done=False))
        self.assertIsNone(msg)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("工具调用", str(err))

    def test_finish_reason_length_marks_truncated(self):
        """finish_reason=length：正文截断如实标注。"""
        msg, err = self._stream(openai_lines(
            [chunk("被截断的回答"), chunk(finish_reason="length")], done=True))
        self.assertIsNone(err)
        self.assertEqual(msg["_truncated"], "length")

    def test_length_with_tool_call_raises(self):
        """length + 工具调用：参数没输完，抛错而非执行。"""
        tc = [{"index": 0, "id": "c1", "type": "function",
               "function": {"name": "write_file", "arguments": '{"path": "a'}}]
        msg, err = self._stream(openai_lines(
            [chunk(tool_calls=tc), chunk(finish_reason="length")], done=True))
        self.assertIsNone(msg)
        self.assertIsInstance(err, RuntimeError)

    def test_finish_reason_stop_without_done_is_not_truncated(self):
        """服务商不发 [DONE] 但给 finish_reason=stop：不误报（兼容性关键）。"""
        msg, err = self._stream(openai_lines([chunk("完整回答", finish_reason="stop")], done=False))
        self.assertIsNone(err)
        self.assertNotIn("_truncated", msg)

    def test_cancelled_stream_is_not_truncated(self):
        """用户点停止导致断流：带部分内容正常收尾，不打截断标记。"""
        import threading
        ev = threading.Event()
        ev.set()
        c = self._client()
        resp = _FakeStreamResp(openai_lines([chunk("部分")], done=False))
        with mock_urlopen(resp):
            msg, err = drain(c.chat_stream(messages=[{"role": "user", "content": "hi"}],
                                           cancel=ev))
        self.assertIsNone(err)
        self.assertEqual(msg["content"], "部分")
        self.assertNotIn("_truncated", msg)


class AnthropicTruncationTest(unittest.TestCase):
    """Anthropic 用 message_stop 作终止标记、stop_reason=max_tokens 表示截断。"""

    def _stream(self, lines, cancel=None):
        c = AnthropicMessagesClient(api_key="k", base_url="http://x", model="m")
        resp = _FakeStreamResp(lines)
        with mock_urlopen(resp):
            return drain(c.chat_stream(messages=[{"role": "user", "content": "hi"}],
                                       cancel=cancel))

    def test_stop_event_means_complete(self):
        lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}',
            'data: {"type":"message_stop"}',
        ]
        msg, err = self._stream(lines)
        self.assertIsNone(err)
        self.assertEqual(msg["content"], "你好")
        self.assertNotIn("_truncated", msg)

    def test_no_stop_with_text_marks_truncated(self):
        lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"半截"}}',
        ]
        msg, err = self._stream(lines)
        self.assertIsNone(err)
        self.assertEqual(msg["_truncated"], "unknown")

    def test_no_stop_with_tool_use_raises(self):
        lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"run_bash"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"rm"}}',
        ]
        msg, err = self._stream(lines)
        self.assertIsNone(msg)
        self.assertIsInstance(err, RuntimeError)

    def test_max_tokens_stop_reason_marks_truncated(self):
        lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"截断"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":5}}',
            'data: {"type":"message_stop"}',
        ]
        msg, err = self._stream(lines)
        self.assertIsNone(err)
        self.assertEqual(msg["_truncated"], "length")


class TruncationNoticeInAnswerTest(unittest.TestCase):
    """截断标记要如实出现在最终回答里，并且随历史留给下一轮的模型。

    此前半截回答被当成完整回答静默落库：用户看到的是一个"正常结束"的
    回答，模型下一轮也不知道自己上次被切断了。
    """

    def _agent(self, message):
        import shutil
        import tempfile
        from agent import Agent

        class OneShotLLM:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "message", message

        ws = tempfile.mkdtemp(prefix="trunc_notice_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        return Agent(llm=OneShotLLM(), verbose=False, workspace=ws)

    def _answer(self, message):
        events = list(self._agent(message).run("hi"))
        done = next(p for k, p in events if k == "done")
        return done["answer"]

    def test_unknown_truncation_notice(self):
        answer = self._answer({"role": "assistant", "content": "半截回答",
                               "_truncated": "unknown"})
        self.assertIn("半截回答", answer)
        self.assertIn("被中断", answer)
        self.assertIn("可能不完整", answer)

    def test_length_truncation_notice(self):
        answer = self._answer({"role": "assistant", "content": "被截断",
                               "_truncated": "length"})
        self.assertIn("max_tokens", answer)

    def test_complete_answer_has_no_notice(self):
        answer = self._answer({"role": "assistant", "content": "完整回答"})
        self.assertEqual(answer, "完整回答")
        self.assertNotIn("⚠️", answer)


# ---------------------------------------------------------------------------
# mock 工具：把 llm_client 内部的 urlopen 换成假响应
# ---------------------------------------------------------------------------

class mock_urlopen:
    """把 llm_client._http_post_json 的 urlopen 换成返回固定假响应。"""

    def __init__(self, resp):
        self.resp = resp
        self._patch = None

    def __enter__(self):
        from unittest import mock
        self._patch = mock.patch.object(llm_client.urllib.request, "urlopen",
                                        return_value=self.resp)
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
