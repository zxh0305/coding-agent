"""
Agent 核心循环 —— 本项目最值得精读的文件
==========================================

抛开各种框架和名词，一个 Agent 的本质就是一个 while 循环：

    ┌─────────────────────────────────────────────────┐
    │ 1. 把【系统提示 + 完整对话历史 + 工具清单】发给 LLM   │
    │ 2. LLM 返回一条 message：                          │
    │      a. 带文字 → 这就是最终回答，结束               │
    │      b. 带 tool_calls → 模型请求调用工具            │
    │ 3. 本地执行工具，把结果以 role=tool 消息追加进历史     │
    │ 4. 回到第 1 步，让 LLM 看着工具结果继续思考           │
    └─────────────────────────────────────────────────┘

两个关键认知（初学者最容易忽略的点）：

  * LLM 本身是【无状态】的。所谓"多轮记忆"，全靠客户端每次把完整
    消息历史重新发一遍。历史里少一条，模型就"忘"了一条。
  * 模型返回的工具调用【不会被自动执行】。它只是输出了一段结构化的
    "请求"，真正执行的是我们本地的代码，执行完还要把结果喂回去。

Function Calling、Tool Use、ReAct……底层都是这个循环的不同包装。
"""

import json
import logging
import threading
import time

from tools import TOOL_SCHEMAS, execute_tool
from ui import colored

log = logging.getLogger("agent")  # 输出目的地由 logger.py 统一配置（写入 agent.log）

DEFAULT_SYSTEM_PROMPT = """\
你是一个在本地工作区里工作的编程助手，所有文件操作都限定在工作区内。
工作守则：
1. 动手前先调查：用 list_dir / grep / read_file 了解代码现状，不要凭空猜测文件内容；
2. 修改已有文件用 apply_patch：search 必须与文件原文逐字符一致（含缩进）且唯一；新建文件用 write_file；
3. 改完要用 run_bash 验证（运行程序或测试），根据输出继续修正，没有验证过不要说"已完成"；
4. 工具或命令失败时，错误信息会原样返回给你——据此调整方案，不要重复同一个失败操作；
5. 最终用简洁中文总结：改了哪些文件、如何验证的。常识性问答直接回答，不必调用工具。
"""


class Agent:
    """一个带工具调用能力的对话 Agent。

    参数：
        llm:          提供 chat(messages, tools) -> dict 的客户端（llm_client.py）
        max_rounds:   单次提问内最多"问 LLM"几轮，防止模型反复调工具停不下来
        verbose:      是否在终端打印每一轮的思考/工具调用过程（学习时强烈建议开着）
    """

    def __init__(self, llm, system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                 max_rounds: int = 16, verbose: bool = True, vision_supported: bool = True):
        # max_rounds=16：coding 任务一轮提问往往要 读代码→改→跑验证→再修 好几个来回
        self.llm = llm
        self.system_prompt = system_prompt
        self.max_rounds = max_rounds
        self.verbose = verbose
        self.vision_supported = vision_supported  # 激活模型能否直接看图（决定是否剥离图片输入）
        self.history: list[dict] = []  # 不含 system 的完整对话历史，跨提问持续累积
        self.trace: list[dict] = []    # 最近一次提问的过程轨迹（轮次/工具调用），供前端展示
        self.current_images: list[dict] = []  # 本轮用户消息附带的图片（供 analyze_image 工具）
        self.cancel_event: threading.Event | None = None  # 本轮生成的停止开关（stop() 置位）

    @staticmethod
    def _clean_outgoing(m: dict) -> dict:
        """发给模型前剥离内部字段（_stats 等下划线前缀），部分服务商会拒绝未知字段。"""
        return {k: v for k, v in m.items() if not k.startswith("_")}

    def _messages_for_model(self) -> list[dict]:
        """发给 LLM 的消息列表。两件事：
        1. 剥离内部字段（_stats 等下划线前缀），部分服务商会拒绝未知字段；
        2. 主模型不支持视觉时，把用户消息里的图片部分替换成文字提示——
           否则不支持视觉的服务商会对图片输入直接报 400。"""
        sanitized = []
        for m in self.history:
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, list) and not self.vision_supported:
                texts, has_image = [], False
                for part in content:
                    if part.get("type") == "image_url":
                        has_image = True
                        continue
                    if part.get("type") == "text" and part.get("text"):
                        texts.append(part["text"])
                note = "\n".join(texts)
                if has_image:
                    note += "\n[用户上传了一张图片；你看不到它的内容，请调用 analyze_image 工具来识别]"
                sanitized.append(self._clean_outgoing({"role": "user", "content": note}))
            else:
                sanitized.append(self._clean_outgoing(m))
        return sanitized

    def context_stats(self, prompt_tokens: int | None = None) -> dict:
        """估算当前上下文的构成（没有本地分词器，用字符占比反推各部分的 token 份额）。

        有服务商返回的真实 prompt_tokens 时，先用 它/总字符数 校准出每字符 token 系数，
        再按各部分字符数分摊 —— 估算值，但量级和占比是可信的。
        """
        sys_chars = len(self.system_prompt)
        tool_chars = len(json.dumps(TOOL_SCHEMAS, ensure_ascii=False))
        buckets = {"user": 0, "assistant": 0, "tool": 0}
        for m in self.history:
            size = len(str(m.get("content") or ""))
            size += len(json.dumps(m.get("tool_calls") or "", ensure_ascii=False))
            if m["role"] in buckets:
                buckets[m["role"]] += size
        total_chars = max(1, sys_chars + tool_chars + sum(buckets.values()))
        ratio = (prompt_tokens / total_chars) if prompt_tokens else 0.4  # 无实测值时的粗略系数
        est = lambda chars: round(chars * ratio)
        return {
            "system": est(sys_chars),
            "tools": est(tool_chars),
            "user": est(buckets["user"]),
            "assistant": est(buckets["assistant"]),
            "tool_results": est(buckets["tool"]),
        }

    # ------------------------------------------------------------------

    def run(self, user_input: str, user_message: dict | None = None):
        """生成器版核心循环：边跑边产出事件，供 Web 流式接口实时推给前端。

        user_message：完整的用户消息（OpenAI 格式，content 可以是带图片/文件的
        数组）。不传则用 user_input 包装成纯文本消息——CLI 走这条路。

        事件序列（kind, payload)：
          ("round",        {"round": n})                  开始第 n 轮
          ("answer_delta", {"delta": "..."})              LLM 正在输出的文字片段
          ("tool_call",    {"name", "arguments"})         模型请求调用工具
          ("tool_result",  {"name", "result"})            工具执行结果
          ("done",         {"answer": "...", "stopped"?}) 最终回答，循环结束；
                                                          用户中途停止时带 stopped=True
        """
        # 停止开关：每次提问配一个新 Event，stop() 置位后循环尽快带部分结果收尾；
        # 结束（或生成器被关闭）时也置位，让 llm_client 里的看护线程退出。
        self.cancel_event = threading.Event()
        try:
            yield from self._run(user_input, user_message)
        finally:
            self.cancel_event.set()

    def stop(self) -> None:
        """请求停止当前这轮生成（Web「停止」按钮 → /api/chat/stop 调到这里）。"""
        if self.cancel_event is not None:
            self.cancel_event.set()

    def _run(self, user_input: str, user_message: dict | None = None):
        """run() 的实际循环体（run 只负责停止开关的生命周期）。"""
        self.trace = []  # 每次提问重新记录过程轨迹
        self.history.append(user_message or {"role": "user", "content": user_input})
        # 提取本轮附带的图片（OpenAI content 数组里的 image_url 部分）。
        # 主模型看不见像素；analyze_image 工具借"视觉模型"看图时用的就是这份数据。
        content = (user_message or {}).get("content")
        if isinstance(content, list):
            self.current_images = [p for p in content if p.get("type") == "image_url"]
        else:
            self.current_images = []
        start = time.time()
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        last_cache = None
        last_ctx = self.context_stats()  # 还没发过请求时给个纯估算

        for round_no in range(1, self.max_rounds + 1):
            if self.cancel_event.is_set():
                # 工具结果刚入完历史就被叫停：历史以上一条 tool 消息结尾，依然合法
                break
            self._log(f"── 第 {round_no} 轮：请求 LLM ──", "gray")
            self.trace.append({"type": "round", "round": round_no})
            yield "round", {"round": round_no}

            # 每轮都重发【系统提示 + 完整历史】—— 这就是 LLM 的全部"记忆"
            # 每轮都重发【系统提示 + 完整历史】—— 这就是 LLM 的全部"记忆"。
            # 主模型不支持视觉时，先把历史里的图片剥离成文字提示（图片数据留在
            # self.current_images，由 analyze_image 工具借视觉模型识别）。
            messages = [{"role": "system", "content": self.system_prompt}, *self._messages_for_model()]
            # 完整 payload 进日志（DEBUG 级）：排错时能看到模型到底"看到"了什么
            log.debug("第 %d 轮请求 payload:\n%s", round_no,
                      json.dumps(messages, ensure_ascii=False, indent=2))

            # 流式拿模型回复：文字片段实时往外 yield，最后拿到完整 message
            assistant_msg = None
            for kind, payload in self.llm.chat_stream(messages=messages, tools=TOOL_SCHEMAS,
                                                      cancel=self.cancel_event):
                if kind == "delta":
                    yield "answer_delta", {"delta": payload}
                elif kind == "usage":  # 本轮 token 用量 → 累计后实时推给前端
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        usage_total[key] += payload.get(key) or 0
                    hit = payload.get("prompt_cache_hit_tokens")
                    miss = payload.get("prompt_cache_miss_tokens")
                    if hit is not None and (hit + (miss or 0)) > 0:
                        last_cache = round(hit / (hit + miss) * 100, 1)
                    last_ctx = self.context_stats(prompt_tokens=payload.get("prompt_tokens"))
                    yield "usage", {**usage_total, "elapsed_s": round(time.time() - start, 1),
                                    "cache_hit_rate": last_cache, "context": last_ctx}
                else:
                    assistant_msg = payload
            log.debug("LLM 原始返回: %s", json.dumps(assistant_msg, ensure_ascii=False))

            if self.cancel_event.is_set():
                # 用户点了停止：已生成的半截文字直接作为回答收尾。
                # 不能把带 tool_calls 的"悬空"assistant 消息留在历史里
                # （下一轮请求会 400），所以这里只追加纯文本回答。
                partial = (assistant_msg or {}).get("content") or ""
                answer = (partial + "\n\n（已手动停止）").strip()
                elapsed = round(time.time() - start, 1)
                self.history.append({"role": "assistant", "content": answer,
                                     "_stats": {"elapsed_s": elapsed, "usage": dict(usage_total),
                                                "cache_hit_rate": last_cache, "stopped": True}})
                log.info("耗时 %.1fs · 用户中途停止", elapsed)
                yield "done", {"answer": answer, "elapsed_s": elapsed, "usage": usage_total,
                               "cache_hit_rate": last_cache, "context": last_ctx, "stopped": True}
                return

            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                # 情况 a：模型直接给出回答，循环结束。
                # 统计信息随消息一起存进历史（_stats 前缀 = 内部字段，
                # 发送给模型前会被剥离，见 _messages_for_model），回放时可见。
                answer = assistant_msg.get("content") or ""
                elapsed = round(time.time() - start, 1)
                record = {"role": "assistant", "content": answer,
                          "_stats": {"elapsed_s": elapsed,
                                     "usage": dict(usage_total),
                                     "cache_hit_rate": last_cache}}
                self.history.append(record)
                log.info("耗时 %.1fs · tokens 输入 %d / 输出 %d",
                         elapsed, usage_total["prompt_tokens"], usage_total["completion_tokens"])
                yield "done", {"answer": answer, "elapsed_s": elapsed, "usage": usage_total,
                               "cache_hit_rate": last_cache, "context": last_ctx}
                return

            # 情况 b：模型请求调用工具
            # 关键：这条"要求调用工具"的 assistant 消息必须原样进历史。
            # 否则下一轮历史里就出现了"没有提问却冒出 tool 结果"的悬空消息，
            # 大多数服务端会直接报 400。
            self.history.append(assistant_msg)
            for call in tool_calls:
                self._log(f"🤖 LLM 请求调用: {call['function']['name']}({call['function']['arguments']})", "cyan")
                arguments = call["function"].get("arguments") or "{}"
                self.trace.append({"type": "tool_call", "name": call["function"]["name"], "arguments": arguments})
                yield "tool_call", {"name": call["function"]["name"], "arguments": arguments}

            for call in tool_calls:
                result = self._run_tool(call)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),  # 与请求里的 id 对应，服务商靠它配对
                    "content": result,
                }
                self.history.append(tool_msg)
                self._log(f"🔧 工具返回: {result}", "yellow")
                name = call["function"]["name"]
                self.trace.append({"type": "tool_result", "name": name, "result": result})
                yield "tool_result", {"name": name, "result": result}

        # 走到循环外只有两种情况：被用户停止，或跑满 max_rounds
        if self.cancel_event.is_set():
            log.info("生成被用户停止（未在流式阶段截住）")
            yield "done", {"answer": "（已手动停止）",
                           "elapsed_s": round(time.time() - start, 1), "usage": usage_total,
                           "cache_hit_rate": last_cache, "context": last_ctx, "stopped": True}
            return
        log.warning("达到最大轮数 %d，强制停止", self.max_rounds)
        yield "done", {"answer": "（已达到最大工具调用轮数，强制停止。可调大 max_rounds，或把问题拆简单些。）",
                       "elapsed_s": round(time.time() - start, 1), "usage": usage_total,
                       "cache_hit_rate": last_cache, "context": last_ctx}

    def chat(self, user_input: str, user_message: dict | None = None) -> str:
        """处理一次用户提问，返回最终文字回答（CLI 用；Web 走 run() 流式）。"""
        answer = ""
        for kind, payload in self.run(user_input, user_message):
            if kind == "done":
                answer = payload["answer"]
        return answer

    def reset(self) -> None:
        """清空对话历史，开始新会话。"""
        self.history.clear()

    # ------------------------------------------------------------------

    def _run_tool(self, call: dict) -> str:
        """解析并执行一次工具调用，任何错误都转成字符串交给 LLM 处理。"""
        name = call["function"]["name"]
        raw_args = call["function"].get("arguments") or "{}"
        try:
            # 注意坑点：arguments 是【JSON 字符串】不是 dict（模型输出的是文本）
            arguments = json.loads(raw_args)
            if not isinstance(arguments, dict):
                arguments = {}
        except json.JSONDecodeError:
            arguments = {}
        result = execute_tool(name, arguments, images=self.current_images)
        if '"error"' in result:
            log.warning("工具 %s 执行出错: %s", name, result)
        return result

    def _log(self, text: str, color: str) -> None:
        log.info(text)  # 同步写进 agent.log（无颜色），终端仍走彩色 print
        if self.verbose:
            print(colored(f"  {text}", color))
