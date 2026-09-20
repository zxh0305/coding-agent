"""
LLM 客户端层
=============

Agent 只会通过这里定义的 `chat(messages, tools)` 与大模型对话，
不关心底层是哪家的 API —— 只要对方兼容 OpenAI 的 chat/completions 格式即可
（智谱 GLM、DeepSeek、Moonshot/Kimi、OpenAI 本家……都兼容）。

配置从环境变量读取，支持项目根目录下的 .env 文件：
  LLM_API_KEY    必填，你的 API key
  LLM_BASE_URL   选填，API 地址（默认智谱 GLM）
  LLM_MODEL      选填，模型名（默认 glm-4-flash）
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("llm")

# 默认指向智谱 GLM；换其他服务商时在 .env 里改 LLM_BASE_URL 和 LLM_MODEL 即可
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-4-flash"


def load_env_file(path: str = ".env") -> None:
    """极简 .env 加载器：逐行读取 KEY=VALUE 写入环境变量。

    规则：忽略空行/#注释；已在 shell 里 export 过的同名变量优先，不被覆盖。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def save_env_values(updates: dict, path: str = ".env") -> None:
    """把 updates 中的键值写进 .env：已有该键则整行替换，没有则追加，注释和其他行原样保留。

    Web 界面的「模型配置」靠它持久化，命令行版下次启动也能读到同一份配置。
    """
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    for key, value in updates.items():
        value = str(value).strip().replace("\n", " ")  # 配置值必须单行，防呆
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _http_post_json(url: str, headers: dict, payload: dict, timeout: int):
    """两种协议共用的 JSON POST：发送并返回响应对象，网络/HTTP 错误统一转成带原因的 RuntimeError。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        # 把服务商返回的错误原文带出来，方便排查（key 无效/额度不足等）
        detail = e.read().decode("utf-8", errors="replace")[:500]
        log.error("API 返回 HTTP %s: %s", e.code, detail)
        raise RuntimeError(f"API 返回错误 HTTP {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
            # python.org 安装版 Python 的经典坑：不读 macOS 钥匙串，自带信任库为空，
            # OpenSSL 会把链尾的正常根证书也误报成 "self-signed certificate in chain"。
            log.error("TLS 证书校验失败: %s", e.reason)
            raise RuntimeError(
                f"TLS 证书校验失败（{e.reason}）\n"
                "  常见原因：python.org 安装的 Python 缺少 CA 根证书包，并非网络被劫持。\n"
                "  修复二选一：\n"
                "    a. 在 .env 里加一行：SSL_CERT_FILE=/etc/ssl/cert.pem（仅本项目生效）\n"
                "    b. 运行 /Applications/Python 3.12/Install Certificates.command（全局生效，需管理员权限）"
            ) from e
        log.error("连接失败: %s", e.reason)
        raise RuntimeError(f"无法连接 API（{url}）：{e.reason}") from e
    except TimeoutError as e:
        # socket 级超时：连接 / 发请求 / 等响应头，任一步 60s 没动静。
        # 注意必须是 RuntimeError——app.py 只接 RuntimeError，让 TimeoutError
        # 漏过去的话，前端只会看到连接无声断掉，没有任何提示。
        log.error("请求超时: %s", e)
        raise RuntimeError(f"请求超时（{timeout}s 内服务器没有响应）: {e}") from e


def _arm_cancel_watchdog(resp, cancel) -> None:
    """停止被触发时，由看护线程把这条 LLM 连接强断掉。

    最麻烦的卡死场景：网关迟迟不回首个 token，却按秒发着 SSE 心跳
    （空行/注释行）。读线程阻塞在 readline 上，socket 超时被心跳不断
    重置，任何"事件之间检查标志"的方案都等不到执行的时机；唯一可靠
    的办法是外部把响应关掉，阻塞中的读立刻抛错，流才能收尾。
    """
    if cancel is None:
        return

    def _watch():
        cancel.wait()
        try:
            resp.close()
        except Exception:
            pass

    threading.Thread(target=_watch, daemon=True, name="llm-cancel-watchdog").start()


class OpenAIChatClient:
    """任意 OpenAI 兼容接口的客户端（纯标准库实现，零依赖）。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 60):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._stream_usage = True  # 服务商不支持 stream_options 时自动降级为 False
        # GLM 的 base_url 以 / 结尾（…/v4/），OpenAI 的不带（…/v1），统一兜一下
        self.api_url = base_url.rstrip("/") + "/chat/completions"

    def _post(self, payload: dict):
        """构造请求并发送，返回响应对象；网络/HTTP 错误统一转成带原因的 RuntimeError。"""
        return _http_post_json(
            self.api_url,
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            payload,
            self.timeout,
        )

    def chat(self, messages: list, tools: list | None = None) -> dict:
        """非流式：发送对话历史 + 工具清单，一次性返回 LLM 的下一条 message。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "tool_choice": "auto",  # 让模型自己决定：直接回答 or 调用工具
        }
        if tools:
            payload["tools"] = tools
        start = time.time()
        with self._post(payload) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        message = data["choices"][0]["message"]
        log.debug("API 往返 %.2fs", time.time() - start)
        return message

    def chat_stream(self, messages: list, tools: list | None = None, cancel=None):
        """流式：逐段产出 ("delta", 文字片段)，最后产出 ("message", 完整消息 dict)。

        两个关键细节：
        1. 服务商返回的是 SSE 格式（text/event-stream）：一行一条 `data: {...}`，
           以 `data: [DONE]` 结束，每条里的 choices[0].delta 只是增量内容；
        2. 工具调用是【分片】到达的：delta.tool_calls[i].function.arguments 每次
           只有一小段 JSON 字符串，必须按 index 累积拼接——这是流式
           Function Calling 最容易踩的坑。

        cancel：threading.Event。被置位时看护线程强断连接，流带着已收到的
        部分内容正常收尾（Agent 用它实现"停止生成"）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "tool_choice": "auto",
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        # 请求 token 用量统计（最后一个 chunk 会带 usage）。部分兼容服务不认
        # stream_options 参数，首次报错就自动降级为不请求。
        resp = None
        want_usage = self._stream_usage
        while True:
            if want_usage:
                payload["stream_options"] = {"include_usage": True}
            try:
                resp = self._post(payload)
                break
            except RuntimeError as e:
                if want_usage and "stream_options" in str(e):
                    self._stream_usage = False
                    want_usage = False
                    log.info("服务商不支持 stream_options，降级为不返回 token 用量")
                    continue
                raise
        _arm_cancel_watchdog(resp, cancel)

        content_parts: list[str] = []
        calls: dict[int, dict] = {}  # tool_calls 的 index -> 累积中的调用
        usage: dict | None = None
        start = time.time()
        with resp:
            try:
                for raw in resp:  # 逐行读 SSE
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue  # 空行 / 注释 / 心跳
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):  # 用量统计 chunk（choices 为空，必须先于 choices 判断）
                        usage = {k: chunk["usage"].get(k, 0) for k in
                                 ("prompt_tokens", "completion_tokens", "total_tokens")}
                        for extra in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                            # DeepSeek 等会返回缓存命中统计 → 前端可显示"缓存命中率"
                            if extra in chunk["usage"]:
                                usage[extra] = chunk["usage"][extra]
                        yield "usage", usage
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        # 思考模型的推理过程先于正文流出。不透传的话，整个思考阶段
                        # 界面毫无动静（socket 读被心跳/思考流喂着不会触发超时），
                        # 用户只能看着光标闪、误以为卡死。推理内容只做实时展示，不进历史。
                        yield "reasoning_delta", reasoning
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        yield "delta", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        index = tc.get("index", 0)
                        acc = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] += fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
            except (OSError, ValueError, AttributeError):
                if cancel is None or not cancel.is_set():
                    # 不是主动停止：网络断了或读取超时，转成 app.py 认识的错误
                    raise RuntimeError("LLM 连接中断（网络断开或读取超时）") from None
                # cancel 置位导致的断连：当作正常结束，带着已收到的部分收尾

        message: dict = {"role": "assistant", "content": "".join(content_parts) or None}
        if calls:
            message["tool_calls"] = [
                {
                    "id": acc["id"] or f"call_{index}",
                    "type": "function",
                    "function": {"name": acc["name"], "arguments": acc["arguments"]},
                }
                for index, acc in sorted(calls.items())
            ]
        log.debug("API 流式往返 %.2fs", time.time() - start)
        yield "message", message


class AnthropicMessagesClient:
    """Anthropic Messages 协议（/v1/messages）适配器。

    对外暴露与 OpenAIChatClient 完全相同的接口——chat/chat_stream 的出入参都是
    OpenAI 消息格式——内部做双向协议转换。这是"协议适配层"模式：
    agent.py 和前端完全不用关心底层是哪家协议。

    与 OpenAI 格式的主要差异：
      * 鉴权用 x-api-key 头（不是 Authorization Bearer，这里两个都发以兼容各网关）；
      * system 提示是顶层字段（不在 messages 数组里）；
      * 工具参数叫 input_schema（不是 parameters），且是对象不是 JSON 字符串；
      * 助手的工具调用和工具结果都是 content 块（tool_use / tool_result），
        工具结果挂在 user 消息上，且相邻同角色消息必须合并；
      * 请求必须带 max_tokens；
      * 流式事件是 message_start / content_block_delta / message_delta / message_stop。
    """

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 60):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        base = base_url.rstrip("/")
        # base 以 /v1 结尾（如 …/code/v1）就直接拼 /messages，否则补全 /v1/messages
        self.api_url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")

    # ---- OpenAI 消息 → Anthropic 请求体 ----

    def _to_anthropic(self, messages: list, tools: list | None) -> dict:
        system_parts, convo = [], []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_parts.append(m.get("content") or "")
            elif role == "user":
                # content 可能是纯文本，也可能是多部分数组（文本 + 图片，视觉输入）
                raw = m.get("content")
                if isinstance(raw, str):
                    convo.append({"role": "user", "content": [{"type": "text", "text": raw}]})
                else:
                    blocks = []
                    for part in raw or []:
                        ptype = part.get("type")
                        if ptype == "text":
                            blocks.append({"type": "text", "text": part.get("text", "")})
                        elif ptype == "image_url":
                            url = (part.get("image_url") or {}).get("url", "")
                            if url.startswith("data:"):  # data:image/png;base64,xxx → base64 块
                                header, _, b64 = url.partition(",")
                                media_type = header[len("data:"):].split(";")[0] or "image/png"
                                blocks.append({"type": "image",
                                               "source": {"type": "base64", "media_type": media_type, "data": b64}})
                    convo.append({"role": "user", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        input_obj = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        input_obj = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{len(blocks)}",
                                   "name": fn.get("name", ""), "input": input_obj})
                convo.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "",
                         "content": m.get("content") or ""}
                # Anthropic 里工具结果是 user 消息；连续多条 tool 合并进同一条 user
                prev = convo[-1] if convo else None
                if prev and prev["role"] == "user" and any(b.get("type") == "tool_result" for b in prev["content"]):
                    prev["content"].append(block)
                else:
                    convo.append({"role": "user", "content": [block]})
        # Anthropic 要求 user/assistant 严格交替：相邻同角色合并
        merged = []
        for c in convo:
            if merged and merged[-1]["role"] == c["role"]:
                merged[-1]["content"].extend(c["content"])
            else:
                merged.append(c)

        body = {"model": self.model, "max_tokens": 16384, "messages": merged}
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            body["tools"] = [
                {"name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                for t in tools
            ]
            body["tool_choice"] = {"type": "auto"}
        return body

    def _post(self, payload: dict):
        return _http_post_json(
            self.api_url,
            {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,                    # Anthropic 官方鉴权头
                "Authorization": f"Bearer {self.api_key}",    # 部分网关认 Bearer，两个都发
                "anthropic-version": "2023-06-01",
            },
            payload,
            self.timeout,
        )

    @staticmethod
    def _from_anthropic(data: dict) -> tuple[dict, dict]:
        """Anthropic 响应 → OpenAI 消息格式 + 用量统计。"""
        text_parts, tool_calls = [], []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": block.get("name", ""),
                                 "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)},
                })
        message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        u = data.get("usage") or {}
        usage = {"prompt_tokens": u.get("input_tokens", 0),
                 "completion_tokens": u.get("output_tokens", 0),
                 "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}
        return message, usage

    def chat(self, messages: list, tools: list | None = None) -> dict:
        """非流式：一次拿到完整 message（OpenAI 格式）。"""
        body = self._to_anthropic(messages, tools)
        with self._post(body) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("type") == "error":
            raise RuntimeError(f"Anthropic API 错误：{(data.get('error') or {}).get('message', data)}")
        message, _ = self._from_anthropic(data)
        return message

    def chat_stream(self, messages: list, tools: list | None = None, cancel=None):
        """流式：产出 ("delta", 文字) / ("usage", 用量)，最后 ("message", 完整消息)。

        cancel：threading.Event，置位时看护线程强断连接（见 _arm_cancel_watchdog）。
        """
        body = self._to_anthropic(messages, tools)
        body["stream"] = True

        blocks: dict[int, dict] = {}  # content block 下标 -> 累积中的内容
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        resp = self._post(body)
        _arm_cancel_watchdog(resp, cancel)
        try:
            with resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[len("data:"):].strip())
                    except json.JSONDecodeError:
                        continue
                    t = data.get("type")
                    if t == "message_start":
                        u = (data.get("message") or {}).get("usage") or {}
                        usage["prompt_tokens"] = u.get("input_tokens", 0)
                    elif t == "content_block_start":
                        block = data.get("content_block") or {}
                        blocks[data.get("index", 0)] = {"type": block.get("type", "text"), "text": "",
                                                        "tool_id": block.get("id", ""),
                                                        "tool_name": block.get("name", ""), "json": ""}
                    elif t == "content_block_delta":
                        d = data.get("delta") or {}
                        blk = blocks.setdefault(data.get("index", 0),
                                                {"type": "text", "text": "", "tool_id": "", "tool_name": "", "json": ""})
                        if d.get("type") == "text_delta":
                            blk["type"] = "text"
                            blk["text"] += d.get("text", "")
                            yield "delta", d.get("text", "")
                        elif d.get("type") == "thinking_delta":
                            # 思考块的 delta 只透传给前端实时显示；不并入 text，
                            # 最终 message（进历史的内容）不带思考过程
                            blk["type"] = "thinking"
                            yield "reasoning_delta", d.get("thinking", "")
                        elif d.get("type") == "input_json_delta":  # 工具参数也是分片到达的
                            blk["type"] = "tool_use"
                            blk["json"] += d.get("partial_json", "")
                    elif t == "message_delta":
                        usage["completion_tokens"] = (data.get("usage") or {}).get("output_tokens", 0)
                    elif t == "error":
                        raise RuntimeError(f"Anthropic API 错误：{(data.get('error') or {}).get('message', data)}")
                    elif t == "message_stop":
                        break
        except (OSError, ValueError, AttributeError):
            if cancel is None or not cancel.is_set():
                raise RuntimeError("LLM 连接中断（网络断开或读取超时）") from None
            # cancel 置位导致的断连：当作正常结束，带着已收到的部分收尾

        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        yield "usage", usage

        message: dict = {"role": "assistant", "content": ""}
        tool_calls = []
        for index in sorted(blocks):
            b = blocks[index]
            if b["type"] == "text" and b["text"]:
                message["content"] += b["text"]
            elif b["type"] == "tool_use":
                tool_calls.append({"id": b["tool_id"] or f"toolu_{index}", "type": "function",
                                   "function": {"name": b["tool_name"], "arguments": b["json"] or "{}"}})
        message["content"] = message["content"] or None
        if tool_calls:
            message["tool_calls"] = tool_calls
        yield "message", message


def create_client(api_format: str, api_key: str, base_url: str, model: str, timeout: int = 60):
    """按供应商的 API 格式选择协议适配器。新协议在这里加一个分支即可。"""
    if api_format == "anthropic":
        return AnthropicMessagesClient(api_key=api_key, base_url=base_url, model=model, timeout=timeout)
    return OpenAIChatClient(api_key=api_key, base_url=base_url, model=model, timeout=timeout)


def create_llm_client(env_path: str = ".env") -> OpenAIChatClient:
    """读取配置并创建客户端；未配置 API key 时直接报错退出。"""
    load_env_file(env_path)

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "\n[配置缺失] 未找到 LLM_API_KEY，无法调用大模型。请按以下步骤配置：\n"
            "  1. cp .env.example .env\n"
            "  2. 编辑 .env，填入你的 LLM_API_KEY（可选：LLM_BASE_URL / LLM_MODEL）\n"
            "  3. 重新运行 python3 main.py\n"
        )

    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL).strip()
    return OpenAIChatClient(api_key=api_key, base_url=base_url, model=model)
