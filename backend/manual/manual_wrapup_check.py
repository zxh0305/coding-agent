"""
收尾轮 / 瞬态重试的真机手测（假 server，不发真实外网请求）
============================================================

python3 backend/manual/manual_wrapup_check.py

起一套真实环境做端到端验收：临时 SQLite 库 + 临时工作区 + 本脚本内起的
mock LLM 服务（纯标准库 HTTP server，说 OpenAI 兼容的 SSE 协议）+ 真实的
llm_client / agent / db 链路。

逐项验收（对应任务书）：
  A. 收尾轮：max_rounds=3 跑多轮任务——第 3 轮后自动流式输出真实总结（不再
     是「强制停止」文案）；收尾轮的 HTTP 请求体【不含 tools/tool_choice 字段】
     （假 server 检查原始请求体）；带 tools 的请求体两者都有；死循环提醒
     （连续 3 次同参调用）注入后模型确实看到、且不落库；
  B. 重启无残留：worker 式落盘 → restore_window + 指纹重建 → 重存零写入，
     恢复的历史里没有任何 _synthetic 残留、总结还在；
  C. 瞬态重试：假 server 先回两次 429（带 Retry-After）再成功——日志恰好
     两条 warning 重试，Agent 事件流照常 done（前端无感）。
"""

import json
import logging
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent  # 脚本在 manual/ 子目录，退两级到 backend/
sys.path.insert(0, str(BACKEND_DIR))

import db        # noqa: E402  （先插 sys.path 再 import，与 app.py 同一运行方式）
import tempfile  # noqa: E402

from agent import Agent            # noqa: E402
from llm_client import OpenAIChatClient  # noqa: E402

SUMMARY = "收尾总结：已读完 note.txt，确认内容无异常；建议下一步补充校验逻辑。"

_failures = []


def CHECK(name, cond, detail=""):
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" —— {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# ---------------------------------------------------------------------------
# mock LLM 服务（OpenAI 兼容 SSE）
# ---------------------------------------------------------------------------

class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.bodies = []          # 每次 /chat/completions 的原始请求体（dict）
        self.flaky_remaining = 0  # /flaky 路径剩余的 429 次数

    def record(self, body):
        with self.lock:
            self.bodies.append(body)

    def snapshot(self):
        with self.lock:
            return list(self.bodies)


STATE = MockState()


def sse_bytes(chunks) -> bytes:
    """把增量列表拼成 OpenAI SSE 响应体。"""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"index": 0, "delta": c}]},
                                           ensure_ascii=False))
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode("utf-8")


TOOL_CHUNKS = [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "read_file",
                                             "arguments": "{\"path\": \"note.txt\"}"}}]},
               {}]
TEXT_CHUNKS = [{"content": SUMMARY}, {}]


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path.startswith("/flaky"):  # 场景 C：先 429 两次再成功
            with STATE.lock:
                STATE.flaky_remaining -= 1
                remain = STATE.flaky_remaining
            if remain >= 0:
                payload = b'{"error": {"message": "rate limited"}}'
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            return self._sse(TEXT_CHUNKS)

        # 场景 A：/chat/completions。带 tools → 回工具调用；不带（收尾轮）→ 回总结
        STATE.record(body)
        if "tools" in body:
            return self._sse(TOOL_CHUNKS)
        return self._sse(TEXT_CHUNKS)

    def _sse(self, chunks):
        payload = sse_bytes(chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ---------------------------------------------------------------------------
# 场景 A + B：收尾轮与重启无残留
# ---------------------------------------------------------------------------

def scenario_wrapup(base_url):
    print("\n【场景 A/B】max_rounds=3 的多轮任务：收尾轮 + 提醒注入 + 重启无残留")
    tmp = tempfile.mkdtemp(prefix="wrapup_manual_")
    db.DB_PATH = Path(tmp) / "manual.db"
    db.init_db()
    db.create_session("manual1", 1)
    ws = Path(tmp) / "ws"
    ws.mkdir()
    (ws / "note.txt").write_text("方案 V2：第一步读文件，第二步总结。", encoding="utf-8")

    client = OpenAIChatClient(api_key="mock", base_url=base_url, model="mock-model")
    agent = Agent(llm=client, verbose=False, workspace=str(ws), max_rounds=3)

    events = list(agent.run("按 note.txt 里的方案推进"))
    rounds = [p for k, p in events if k == "round"]
    done = events[-1][1]

    CHECK("A1 三轮工具后进入收尾轮（round 4 带 wrap_up 标记）",
          rounds[-1] == {"round": 4, "wrap_up": True})
    CHECK("A2 done 是真实总结而非「强制停止」文案",
          done.get("answer") == SUMMARY and "强制停止" not in done.get("answer", ""))
    CHECK("A3 done 带 stopped_reason=max_rounds 且不是手动停止",
          done.get("stopped_reason") == "max_rounds" and "stopped" not in done)

    bodies = STATE.snapshot()
    CHECK("A4 假 server 共收到 4 次请求（3 工具轮 + 1 收尾轮）", len(bodies) == 4,
          f"实际 {len(bodies)}")
    tool_req, wrap_req = bodies[0], bodies[-1]
    CHECK("A5 带 tools 的请求体同时含 tools 与 tool_choice",
          "tools" in tool_req and tool_req.get("tool_choice") == "auto")
    CHECK("A6 收尾轮请求体不含 tools / tool_choice 字段（OpenAI 协议必 400 的坑）",
          "tools" not in wrap_req and "tool_choice" not in wrap_req)
    wrap_last = wrap_req["messages"][-1]
    CHECK("A7 收尾轮请求末尾是合成指令（user 角色，已达上限）",
          wrap_last["role"] == "user" and "已达上限（3）" in wrap_last["content"])
    CHECK("A8 请求体里没有 _synthetic 等内部字段（发给模型前已剥离）",
          not any(str(k).startswith("_") for m in wrap_req["messages"] for k in m))
    reminder_seen = any("系统提示：你已连续 3 次" in (m.get("content") or "")
                        for m in wrap_req["messages"])
    CHECK("A9 死循环提醒（第 3 轮同参重复触发）出现在收尾轮请求里，模型确实看到",
          reminder_seen)

    # ---- 场景 B：worker 式落盘 → 重启恢复 → 无残留 ----
    print("\n【场景 B】worker 式落盘 + 重启恢复")
    written = db.save_messages("manual1", agent.history, agent.saved)
    db.touch_session("manual1")
    restored = db.restore_window("manual1")
    saved2 = db.fingerprints(restored)
    rewritten = db.save_messages("manual1", restored, saved2)
    CHECK("B1 落盘行数不含合成消息（用户输入 1 + 每轮工具消息 2×3 + 总结 1 = 8 行）",
          len(restored) == 8 and written == 8, f"内存 {len(agent.history)} 条，落盘 {written} 行")
    CHECK("B2 重启后重存零写入（指纹账本重建正确）", rewritten == 0)
    CHECK("B3 恢复的历史里没有任何 _synthetic / 提醒 / 收尾指令残留",
          not any(m.get("_synthetic") or "已达上限" in str(m.get("content"))
                  or "系统提示" in str(m.get("content")) for m in restored))
    CHECK("B4 总结在重启后仍在（最后一行 assistant）",
          restored[-1]["role"] == "assistant" and restored[-1]["content"] == SUMMARY)
    # app.py turn_end 的 user_mid 口径：跳过 _synthetic 后必须还能找到真实用户消息
    user_mid = next((m.get("_mid") for m in reversed(agent.history)
                     if m.get("role") == "user" and not m.get("_synthetic")), None)
    CHECK("B5 turn_end 的 user_mid 不被合成消息带成 None", user_mid is not None)


# ---------------------------------------------------------------------------
# 场景 C：429 限流退避重试
# ---------------------------------------------------------------------------

class RetryLogCatcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def scenario_retry(base_url):
    print("\n【场景 C】假 server 两次 429（Retry-After: 1）后成功")
    tmp = tempfile.mkdtemp(prefix="wrapup_manual_flaky_")
    db.DB_PATH = Path(tmp) / "manual.db"
    db.init_db()
    db.create_session("manual2", 1)
    STATE.flaky_remaining = 2  # 接下来两次请求回 429

    catcher = RetryLogCatcher()
    llm_logger = logging.getLogger("llm")
    llm_logger.addHandler(catcher)
    try:
        client = OpenAIChatClient(api_key="mock", base_url=base_url + "/flaky", model="mock-model")
        agent = Agent(llm=client, verbose=False, workspace=str(tmp), max_rounds=3)
        events = list(agent.run("只回复：好"))
    finally:
        llm_logger.removeHandler(catcher)

    done = events[-1][1]
    warns = [m for m in catcher.records if "重试" in m]
    CHECK("C1 重试恰好 2 次（日志两条 warning）", len(warns) == 2, f"实际 {len(warns)} 条: {warns}")
    CHECK("C2 重试对上层无感：Agent 照常 done 且答案是总结文本",
          done.get("answer") == SUMMARY and not done.get("stopped"))
    CHECK("C3 等待秒数取 Retry-After（两条 warning 都报 1 秒）",
          all("1 秒" in m for m in warns))


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"
    print(f"mock LLM 服务已启动: {base_url}（/chat/completions + /flaky/chat/completions）")
    try:
        scenario_wrapup(base_url)
        scenario_retry(base_url)
    finally:
        server.shutdown()

    print()
    if _failures:
        print(f"❌ {len(_failures)} 项未通过: {_failures}")
        sys.exit(1)
    print("✅ 全部验收项通过")


if __name__ == "__main__":
    main()
