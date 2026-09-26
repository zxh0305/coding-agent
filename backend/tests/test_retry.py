"""
瞬态错误退避重试 + tools/tool_choice 配对约束的单元测试
（cd backend && python3 -m unittest tests.test_retry -v）
================================================================================

1. _http_post_json 的错误转译：HTTPError → ApiHTTPError（带 status / Retry-After /
   body 摘要），URLError / 超时 → ApiConnectionError；两者都是 RuntimeError 子类
   （worker 的 except RuntimeError 兼容面不变）。TLS 证书错误刻意保持普通
   RuntimeError——配置错误不会因等待自愈，绝不重试；
2. post_json_with_retry：429/5xx/连接失败退避重试（节奏优先 Retry-After、封顶
   30s、无头默认 2s/4s），401/400 立即抛零重试，等待期间 cancel 置位立即放弃；
3. 两个 client 的 _post 接 cancel 并透传给重试层——chat_stream 把 Agent 的停止
   开关传进来，_arm_cancel_watchdog 之前的空窗（重试等待期）也能响应停止；
4. tools=None 的请求（收尾轮 / 记忆提取 / 看图）绝不带 tools/tool_choice 字段
   ——OpenAI 协议只发 tool_choice 不发 tools 直接 400。
全部 mock 在 urlopen / _http_post_json / post_json_with_retry 层，不碰网络。
"""

import email.message
import io
import json
import threading
import unittest
import urllib.error
from unittest import mock

import llm_client
from llm_client import (AnthropicMessagesClient, ApiConnectionError, ApiHTTPError,
                        OpenAIChatClient, post_json_with_retry)


def http_error(status: int, body: str = "boom", retry_after: str | None = None) -> urllib.error.HTTPError:
    """构造真实的 urllib HTTPError（带响应体与可选 Retry-After 头）。"""
    fp = io.BytesIO(body.encode("utf-8"))
    headers = None
    if retry_after is not None:
        headers = email.message.Message()
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x", status, "err", headers, fp)


class FakeResp:
    """假响应：chat() 走 read()，chat_stream() 走逐行迭代 + 上下文管理器。"""

    def __init__(self, lines=None, body=None):
        self._lines = [ln.encode("utf-8") for ln in (lines or [])]
        self._body = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


OPENAI_TEXT_SSE = [
    'data: {"choices":[{"delta":{"content":"hi"}}]}',
    "data: [DONE]",
]

ANTHROPIC_TEXT_SSE = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
    'data: {"type":"message_stop"}',
]


class RetryTestBase(unittest.TestCase):
    """公共脚手架：替换 _sleep 为记录函数（不真睡），restore 交给 addCleanup。"""

    def setUp(self):
        self.sleeps = []
        self._orig_sleep = llm_client._sleep
        llm_client._sleep = self.sleeps.append
        self.addCleanup(lambda: setattr(llm_client, "_sleep", self._orig_sleep))

    def patch_post(self, responses, calls):
        """替换 _http_post_json 为按剧本吐结果/抛异常的假函数。"""

        def fake(url, headers, payload, timeout):
            calls.append(payload)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return mock.patch("llm_client._http_post_json", side_effect=fake)


# ---------------------------------------------------------------------------
# 一、_http_post_json 的错误转译
# ---------------------------------------------------------------------------

class TestErrorTranslation(unittest.TestCase):

    def run_post(self, side_effect):
        with mock.patch("urllib.request.urlopen", side_effect=side_effect):
            llm_client._http_post_json("http://x", {}, {}, 5)

    def test_http_error_carries_status_retry_after_and_body(self):
        with self.assertRaises(ApiHTTPError) as cm:
            self.run_post(http_error(429, "rate limited", retry_after="7"))
        e = cm.exception
        self.assertIsInstance(e, RuntimeError)  # worker 的兼容面不变
        self.assertEqual(e.status, 429)
        self.assertEqual(e.retry_after, 7)
        self.assertIn("rate limited", e.body)

    def test_http_error_without_retry_after(self):
        with self.assertRaises(ApiHTTPError) as cm:
            self.run_post(http_error(500, "boom"))
        self.assertIsNone(cm.exception.retry_after)

    def test_connection_error_and_timeout_are_retryable_types(self):
        with self.assertRaises(ApiConnectionError):
            self.run_post(urllib.error.URLError("connection refused"))
        with self.assertRaises(ApiConnectionError):
            self.run_post(TimeoutError("timed out"))

    def test_certificate_failure_stays_plain_runtime_error(self):
        """证书配置错误不重试：必须是普通 RuntimeError，不是 ApiConnectionError。"""
        with self.assertRaises(RuntimeError) as cm:
            self.run_post(urllib.error.URLError("CERTIFICATE_VERIFY_FAILED: self-signed"))
        self.assertNotIsInstance(cm.exception, ApiConnectionError)
        self.assertIn("SSL_CERT_FILE", str(cm.exception))  # 修复指引仍在


# ---------------------------------------------------------------------------
# 二、post_json_with_retry 的重试策略
# ---------------------------------------------------------------------------

class TestPostJsonWithRetry(RetryTestBase):

    def test_retries_429_twice_then_succeeds(self):
        calls, responses = [], [ApiHTTPError(429, "限流"), ApiHTTPError(429, "限流"), "resp"]
        with self.patch_post(responses, calls):
            self.assertEqual(post_json_with_retry("u", {}, {}, 5), "resp")
        self.assertEqual(len(calls), 3)                      # 恰好重试 2 次
        self.assertAlmostEqual(sum(self.sleeps), 6.0, places=6)  # 固定退避 2s + 4s

    def test_retry_after_header_wins_over_backoff(self):
        calls, responses = [], [ApiHTTPError(429, "限流", retry_after=7), "resp"]
        with self.patch_post(responses, calls):
            post_json_with_retry("u", {}, {}, 5)
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(sum(self.sleeps), 7.0, places=6)  # 等待秒数取 Retry-After

    def test_retry_after_capped_at_30s(self):
        calls, responses = [], [ApiHTTPError(429, "限流", retry_after=600), "resp"]
        with self.patch_post(responses, calls):
            post_json_with_retry("u", {}, {}, 5)
        self.assertAlmostEqual(sum(self.sleeps), 30.0, places=6)  # 600s 被封顶到 30s

    def test_5xx_and_connection_errors_are_retryable(self):
        calls, responses = [], [ApiHTTPError(503, "网关"), ApiConnectionError("断"), "resp"]
        with self.patch_post(responses, calls):
            self.assertEqual(post_json_with_retry("u", {}, {}, 5), "resp")
        self.assertEqual(len(calls), 3)

    def test_401_raises_immediately_with_zero_retries(self):
        calls, responses = [], [ApiHTTPError(401, "key 无效"), "resp"]
        with self.patch_post(responses, calls):
            with self.assertRaises(ApiHTTPError):
                post_json_with_retry("u", {}, {}, 5)
        self.assertEqual(len(calls), 1)   # 一次都没重试
        self.assertEqual(self.sleeps, [])  # 也没等过

    def test_400_raises_immediately(self):
        calls, responses = [], [ApiHTTPError(400, "参数错"), "resp"]
        with self.patch_post(responses, calls):
            with self.assertRaises(ApiHTTPError):
                post_json_with_retry("u", {}, {}, 5)
        self.assertEqual(len(calls), 1)

    def test_cancel_during_wait_stops_retrying(self):
        """等待期间停止开关置位：重抛最后一个错误、不再重试（取舍见其 docstring）。"""
        cancel = threading.Event()

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            cancel.set()  # 第一次等待分片时用户点了「停止」

        calls, responses = [], [ApiHTTPError(429, "限流"), "resp"]
        with self.patch_post(responses, calls), \
                mock.patch("llm_client._sleep", side_effect=fake_sleep):
            with self.assertRaises(ApiHTTPError):
                post_json_with_retry("u", {}, {}, 5, cancel=cancel)
        self.assertEqual(len(calls), 1)  # 没有第二次尝试

    def test_attempts_exhausted_raises_last_error(self):
        calls, responses = [], [ApiHTTPError(500, "挂") for _ in range(3)]
        with self.patch_post(responses, calls):
            with self.assertRaises(ApiHTTPError):
                post_json_with_retry("u", {}, {}, 5)
        self.assertEqual(len(calls), 3)  # 默认 attempts=3


# ---------------------------------------------------------------------------
# 三、client → 重试层的 cancel 透传
# ---------------------------------------------------------------------------

class CaptureRetry:
    """替换 post_json_with_retry：记录 (payload, cancel)，按剧本返回响应。"""

    def __init__(self, response):
        self.response = response
        self.records = []

    def __call__(self, url, headers, payload, timeout, cancel=None, attempts=3, on_retry=None):
        self.records.append({"payload": payload, "cancel": cancel})
        return self.response


class TestClientWiring(unittest.TestCase):

    def test_openai_chat_passes_no_cancel_and_streams_pass_event(self):
        client = OpenAIChatClient(api_key="k", base_url="https://x/v4/", model="m")
        cap = CaptureRetry(FakeResp(body={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}))
        with mock.patch("llm_client.post_json_with_retry", cap):
            client.chat([{"role": "user", "content": "hi"}])  # 非流式（记忆提取走这里）
            self.assertIsNone(cap.records[-1]["cancel"])
            evt = threading.Event()
            list(client.chat_stream([{"role": "user", "content": "hi"}], cancel=evt))
            self.assertIs(cap.records[-1]["cancel"], evt)  # 停止开关传进重试层

    def test_anthropic_chat_stream_passes_cancel(self):
        client = AnthropicMessagesClient(api_key="k", base_url="https://x/v1", model="m")
        cap = CaptureRetry(FakeResp(lines=ANTHROPIC_TEXT_SSE))
        with mock.patch("llm_client.post_json_with_retry", cap):
            evt = threading.Event()
            list(client.chat_stream([{"role": "user", "content": "hi"}], cancel=evt))
            self.assertIs(cap.records[-1]["cancel"], evt)


# ---------------------------------------------------------------------------
# 四、tools=None 的请求体不带 tools / tool_choice
# ---------------------------------------------------------------------------

class TestToolChoicePairing(unittest.TestCase):

    def test_openai_chat_without_tools_has_no_tool_fields(self):
        client = OpenAIChatClient(api_key="k", base_url="https://x/v4/", model="m")
        cap = CaptureRetry(FakeResp(body={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}))
        with mock.patch("llm_client.post_json_with_retry", cap):
            client.chat([{"role": "user", "content": "总结一下"}])
            payload = cap.records[-1]["payload"]
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            client.chat([{"role": "user", "content": "hi"}], tools=[{"x": 1}])
            payload = cap.records[-1]["payload"]
            self.assertEqual(payload["tools"], [{"x": 1}])
            self.assertEqual(payload["tool_choice"], "auto")

    def test_openai_chat_stream_without_tools_has_no_tool_fields(self):
        """收尾轮的关键前提：tools=None 的流式请求体若带 tool_choice 会被 400。"""
        client = OpenAIChatClient(api_key="k", base_url="https://x/v4/", model="m")
        cap = CaptureRetry(FakeResp(lines=OPENAI_TEXT_SSE))
        with mock.patch("llm_client.post_json_with_retry", cap):
            list(client.chat_stream([{"role": "user", "content": "直接总结"}], tools=None))
            payload = cap.records[-1]["payload"]
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            list(client.chat_stream([{"role": "user", "content": "hi"}], tools=[{"x": 1}]))
            payload = cap.records[-1]["payload"]
            self.assertEqual(payload["tool_choice"], "auto")

    def test_anthropic_body_without_tools_has_no_tool_choice(self):
        client = AnthropicMessagesClient(api_key="k", base_url="https://x/v1", model="m")
        body = client._to_anthropic([{"role": "user", "content": "直接总结"}], tools=None)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
