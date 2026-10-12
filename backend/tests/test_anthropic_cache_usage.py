"""
Anthropic 适配器缓存字段解析的回归测试
==================================================================
运行：cd backend && python3 -m unittest tests.test_anthropic_cache_usage -v

背景：探测脚本直连 maiya 生产能拿到 cache_read_input_tokens=1024，但应用落库
的 cached_tokens 恒 0——因为 AnthropicMessagesClient.chat_stream 解析 usage 时
只读 input_tokens/output_tokens，cache_read/creation 被直接丢弃。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_client import AnthropicMessagesClient


class FakeResp:
    """模拟 urllib 响应：按行吐 SSE。"""

    def __init__(self, lines):
        self._lines = [l.encode() for l in lines]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass

    def read(self):
        return b""


def run_stream(client, lines):
    """喂 SSE 行，收集 (kind, payload)。"""
    client._post = lambda body, cancel=None: FakeResp(lines)
    out = []
    for kind, payload in client.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        out.append((kind, payload))
    return out


class TestAnthropicCacheUsage(unittest.TestCase):
    def _client(self):
        return AnthropicMessagesClient(api_key="k", base_url="https://x/v1", model="m")

    def test_message_start_cache_fields(self):
        """message_start 带缓存字段 → usage 应带 prompt_cache_hit/miss_tokens"""
        lines = [
            'data: {"type":"message_start","message":{"usage":{'
            '"input_tokens":2048,"cache_read_input_tokens":1024,'
            '"cache_creation_input_tokens":512}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":3}}',
            'data: {"type":"message_stop"}',
        ]
        events = run_stream(self._client(), lines)
        usage = [p for k, p in events if k == "usage"][0]
        self.assertEqual(usage["prompt_cache_hit_tokens"], 1024)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 512)
        self.assertEqual(usage["prompt_tokens"], 2048)
        self.assertEqual(usage["completion_tokens"], 3)

    def test_message_delta_cache_fields(self):
        """缓存字段延到 message_delta 才给（部分网关行为）→ 同样能解析到"""
        lines = [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":2048}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":3,'
            '"input_tokens":2048,"cache_read_input_tokens":1536}}',
            'data: {"type":"message_stop"}',
        ]
        events = run_stream(self._client(), lines)
        usage = [p for k, p in events if k == "usage"][0]
        self.assertEqual(usage["prompt_cache_hit_tokens"], 1536)
        self.assertEqual(usage["prompt_tokens"], 2048)

    def test_no_cache_fields_absent_keys(self):
        """供应商不返回缓存字段 → usage 不带这两个键（前端显示「未返回」而非 0）"""
        lines = [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":100}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":2}}',
            'data: {"type":"message_stop"}',
        ]
        events = run_stream(self._client(), lines)
        usage = [p for k, p in events if k == "usage"][0]
        self.assertNotIn("prompt_cache_hit_tokens", usage)
        self.assertNotIn("prompt_cache_miss_tokens", usage)


if __name__ == "__main__":
    unittest.main()
