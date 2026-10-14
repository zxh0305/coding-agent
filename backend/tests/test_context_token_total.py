"""上下文口径回归：prompt_tokens_total 必须含缓存命中，且两家语义不混
================================================================================
运行：cd backend && python3 -m unittest tests.test_context_token_total -v

背景（真实踩过的坑）
--------------------------------------------------------------------------------
同一个字段名在两家的含义是**相反的**：

  Anthropic  input_tokens            = 全部输入（命中缓存的也含在内）
             cache_read_input_tokens = 其中命中缓存的部分（子集）
  DeepSeek   prompt_tokens           = **仅未命中**的部分
             prompt_cache_hit_tokens = 另外还有这么多命中

旧代码统一按 Anthropic 语义理解 prompt_tokens，于是"缓存命中越多、算出来的
上下文越小"。实测真实会话：prompt_tokens=102 而命中 8448 → 真实上下文 8550，
被当成 102。三处受害共用这个被低估的数：前端上下文徽章、自动压缩触发线、
context_stats 的每字符校准系数——一起偏小，表现为"占用看着很小、压缩迟迟
不触发，直到窗口满了直接崩"。

【本文件守什么】
1. 两家各自的 prompt_tokens_total 取值（DeepSeek 加 hit、Anthropic 不加）；
2. **判别信息来自解析处而非字典内容**——这是最容易写错的地方：usage 走到
   agent 层时两家字段名已统一，仅看字典无法区分。若实现改成"有 hit 就加"，
   Anthropic 的 8550+8448=16998 会让上下文虚高近一倍，压缩被提前误触发。
   test_helper_takes_semantics_from_argument_not_dict_shape 专门锁这条。
3. 计费口径 prompt_tokens **不被改动**（它必须保持"未命中"，用量页按它
   算钱、按 cached_tokens 算命中率）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_client import (AnthropicMessagesClient, OpenAIChatClient,
                        input_token_total)
from tests.test_anthropic_cache_usage import FakeResp


# --------------------------------------------------------------------------
# 纯函数：语义判别
# --------------------------------------------------------------------------

class TestInputTokenTotal(unittest.TestCase):

    def test_deepseek_semantics_adds_hit(self):
        """DeepSeek：prompt_tokens 仅未命中 → 总数 = prompt + hit。

        用真实会话的数字：102 + 8448 = 8550（旧代码报 102）。
        """
        self.assertEqual(
            input_token_total({"prompt_tokens": 102, "prompt_cache_hit_tokens": 8448},
                              prompt_excludes_cache=True),
            8550)

    def test_anthropic_semantics_does_not_add_hit(self):
        """Anthropic：input_tokens 已含命中 → 原样返回，绝不能再加 hit。

        同样数字（输入 8550、命中 8448）在 Anthropic 语义下必须仍是 8550；
        若误加成 16998，上下文虚高近一倍 → 压缩被提前误触发。
        """
        self.assertEqual(
            input_token_total({"prompt_tokens": 8550, "prompt_cache_hit_tokens": 8448},
                              prompt_excludes_cache=False),
            8550)

    def test_no_cache_fields_unaffected(self):
        """没报缓存时两家一致：原样返回。"""
        for exc in (True, False):
            self.assertEqual(
                input_token_total({"prompt_tokens": 1200}, prompt_excludes_cache=exc),
                1200)

    def test_zero_hit(self):
        """命中 0：加不加都一样（首次请求的常见形态）。"""
        self.assertEqual(
            input_token_total({"prompt_tokens": 8471, "prompt_cache_hit_tokens": 0},
                              prompt_excludes_cache=True),
            8471)

    def test_empty_usage_returns_zero(self):
        """空 usage → 0（宁小勿大：调用方走 max，不会把上下文误报成 0）。"""
        self.assertEqual(input_token_total({}, prompt_excludes_cache=True), 0)
        self.assertEqual(input_token_total({}, prompt_excludes_cache=False), 0)

    def test_helper_takes_semantics_from_argument_not_dict_shape(self):
        """【核心】同一个字典，两种语义给出两个不同答案。

        这条防止实现退化成"看字典里有没有 prompt_cache_hit_tokens 就决定加不加"
        ——那样 usage 统一字段名后必然把 Anthropic 算错。判别信息只能来自
        解析处（明确知道在读哪家响应），本断言把这个契约钉死。
        """
        same = {"prompt_tokens": 8550, "prompt_cache_hit_tokens": 8448}
        as_openai = input_token_total(dict(same), prompt_excludes_cache=True)
        as_anthropic = input_token_total(dict(same), prompt_excludes_cache=False)
        self.assertEqual((as_openai, as_anthropic), (16998, 8550))
        self.assertNotEqual(as_openai, as_anthropic,
                            "两种语义必须给出不同结果——否则判别信息丢了")


# --------------------------------------------------------------------------
# 端到端：真解析器喂真 SSE 行
# --------------------------------------------------------------------------

def run_anthropic_stream(lines):
    client = AnthropicMessagesClient(api_key="k", base_url="https://x/v1", model="m")
    client._post = lambda body, cancel=None: FakeResp(lines)
    out = []
    for kind, payload in client.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        out.append((kind, payload))
    return out


class TestAnthropicStreamTotal(unittest.TestCase):

    def _usage(self, lines):
        return [p for k, p in run_anthropic_stream(lines) if k == "usage"][0]

    def test_official_anthropic_input_already_includes_cache(self):
        """官方 Anthropic 语义：input=8550 命中 8448（命中≤输入，是子集）
        → total 必须还是 8550，绝不能加成 16998。"""
        usage = self._usage([
            'data: {"type":"message_start","message":{"usage":{'
            '"input_tokens":8550,"cache_read_input_tokens":8448}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":3}}',
            'data: {"type":"message_stop"}',
        ])
        self.assertEqual(usage["prompt_tokens_total"], 8550)
        # 计费口径未被改动：仍是原始 input_tokens
        self.assertEqual(usage["prompt_tokens"], 8550)
        self.assertEqual(usage["prompt_cache_hit_tokens"], 8448)

    def test_gateway_whose_input_excludes_cache(self):
        """【实测踩坑】api_format=anthropic 的网关却报 DeepSeek 语义。

        maiya生产 返回 input_tokens=102 + cache_read=8448——命中量比输入总量
        还大，在"子集"语义下算术不可能，故 input 必为【仅未命中】，
        真实上下文 = 102 + 8448 = 8550。

        这条同时锁住判别方式：不能按 api_format 猜（这个网关就是 anthropic），
        只能按 hit > input 这条算术不可能性来认。
        """
        usage = self._usage([
            'data: {"type":"message_start","message":{"usage":{'
            '"input_tokens":102,"cache_read_input_tokens":8448}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":189}}',
            'data: {"type":"message_stop"}',
        ])
        self.assertEqual(usage["prompt_tokens_total"], 8550)
        self.assertEqual(usage["prompt_tokens"], 102)   # 计费口径不动

    def test_missing_message_start_still_gets_total(self):
        """兼容网关不给 message_start 时，message_delta 补的值也要判对语义。"""
        usage = self._usage([
            'data: {"type":"message_start","message":{"usage":{}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":3,'
            '"input_tokens":102,"cache_read_input_tokens":8448}}',
            'data: {"type":"message_stop"}',
        ])
        self.assertEqual(usage["prompt_tokens_total"], 8550)

    def test_total_present_even_without_cache(self):
        """不报缓存时也要给 total（下游按它算上下文，缺键会退回旧口径）。"""
        usage = self._usage([
            'data: {"type":"message_start","message":{"usage":{"input_tokens":1234}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":2}}',
            'data: {"type":"message_stop"}',
        ])
        self.assertEqual(usage["prompt_tokens_total"], 1234)

    def test_does_not_mutate_billing_prompt_tokens(self):
        """【计费口径不动】prompt_tokens 保持"未命中"，用量页算钱/命中率靠它。

        曾经的诱惑是直接把 prompt_tokens 改成总量——那会让账单虚高、
        命中率算错（hit/(hit+miss) 的分母被污染）。本断言锁死不改。
        """
        usage = self._usage([
            'data: {"type":"message_start","message":{"usage":{'
            '"input_tokens":2048,"cache_read_input_tokens":1024,'
            '"cache_creation_input_tokens":512}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":3}}',
            'data: {"type":"message_stop"}',
        ])
        self.assertEqual(usage["prompt_tokens"], 2048)        # 未改：仍是服务商给的
        self.assertEqual(usage["prompt_tokens_total"], 2048)  # Anthropic 语义下相等
        self.assertEqual(usage["prompt_cache_miss_tokens"], 512)


class TestOpenAIStreamTotal(unittest.TestCase):
    """OpenAI 兼容流（DeepSeek 走这条）：total 必须把命中量加回来。"""

    def _usage(self, usage_obj):
        import json as _json
        client = OpenAIChatClient(api_key="k", base_url="https://x/v1", model="m")
        lines = [
            'data: ' + _json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
            'data: ' + _json.dumps({"choices": [], "usage": usage_obj}),
            'data: [DONE]',
        ]
        client._post = lambda body, cancel=None: FakeResp(lines)
        return [p for k, p in client.chat_stream(messages=[{"role": "user", "content": "hi"}])
                if k == "usage"][0]

    def test_deepseek_hit_is_added_back(self):
        """复现真实会话：prompt 102 + 命中 8448 → total 8550（旧代码 102）。"""
        usage = self._usage({
            "prompt_tokens": 102, "completion_tokens": 189, "total_tokens": 291,
            "prompt_cache_hit_tokens": 8448, "prompt_cache_miss_tokens": 0})
        self.assertEqual(usage["prompt_tokens_total"], 8550)
        self.assertEqual(usage["prompt_tokens"], 102)   # 计费口径不动

    def test_openai_nested_cached_tokens_style(self):
        """OpenAI 原生风格（命中嵌在 prompt_tokens_details.cached_tokens）。"""
        usage = self._usage({
            "prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
            "prompt_tokens_details": {"cached_tokens": 900}})
        self.assertEqual(usage["prompt_tokens_total"], 1000)

    def test_first_turn_no_cache(self):
        """首轮无命中：total == prompt（加 0 不影响）。"""
        usage = self._usage({
            "prompt_tokens": 8471, "completion_tokens": 74, "total_tokens": 8545,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0})
        self.assertEqual(usage["prompt_tokens_total"], 8471)


class TestAgentContextTokens(unittest.TestCase):
    """端到端到 agent 层：context_tokens（徽章 + 压缩触发线）必须用总输入。

    这一层最容易漏——单测 llm_client 全绿，但 agent 若仍读 payload["prompt_tokens"]
    或忘了把新键累计进 usage_total，线上照旧低估（现实测正是如此：
    解析器已正确产出 8506，落库的 context_tokens 却还是 58）。
    """

    def _run(self, usage):
        from agent import Agent

        class FakeLLM:
            def chat_stream(self, messages=None, tools=None, cancel=None):
                yield "delta", "hi"
                yield "usage", dict(usage)
                yield "message", {"role": "assistant", "content": "hi"}

        agent = Agent(llm=FakeLLM())
        out = {}
        for kind, payload in agent.run("hi"):
            if kind == "usage":
                out["usage"] = payload
            elif kind == "done":
                out["done"] = payload
        return out

    def test_context_tokens_uses_total_not_prompt(self):
        """真实会话数字：prompt=58 命中 8448 → context_tokens 必须是 8506。

        旧行为是 58，比真值小两个数量级——徽章显示"上下文几乎空着"，
        压缩触发线也据此判断，于是永远不触发。
        """
        out = self._run({"prompt_tokens": 58, "completion_tokens": 10,
                         "total_tokens": 68, "prompt_cache_hit_tokens": 8448,
                         "prompt_cache_miss_tokens": 0, "prompt_tokens_total": 8506})
        self.assertEqual(out["usage"]["context_tokens"], 8506)

    def test_done_usage_carries_total_for_persistence(self):
        """done.usage 落进 _stats.usage：必须带上新键，否则重启后
        /api/context 的内存兜底仍拿不到真值（只认 stats_json.context_tokens）。"""
        out = self._run({"prompt_tokens": 58, "completion_tokens": 10,
                         "total_tokens": 68, "prompt_cache_hit_tokens": 8448,
                         "prompt_cache_miss_tokens": 0, "prompt_tokens_total": 8506})
        self.assertIn("prompt_tokens_total", out["done"]["usage"])
        self.assertEqual(out["done"]["usage"]["prompt_tokens_total"], 8506)

    def test_falls_back_to_prompt_when_total_absent(self):
        """老服务端/异常包不给 total → 退回 prompt_tokens，不倒退成 0。"""
        out = self._run({"prompt_tokens": 4375, "completion_tokens": 42,
                         "total_tokens": 4417})
        self.assertEqual(out["usage"]["context_tokens"], 4375)

    def test_billing_prompt_tokens_untouched(self):
        """累计的 prompt_tokens 保持"未命中"口径（用量页算钱用）。"""
        out = self._run({"prompt_tokens": 58, "completion_tokens": 10,
                         "total_tokens": 68, "prompt_cache_hit_tokens": 8448,
                         "prompt_cache_miss_tokens": 0, "prompt_tokens_total": 8506})
        self.assertEqual(out["done"]["usage"]["prompt_tokens"], 58)


if __name__ == "__main__":
    unittest.main()
