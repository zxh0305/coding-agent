"""
记忆系统真机手测（mock LLM，不发真实网络请求）
================================================

python3 backend/manual_memory_check.py

起一套真实环境做端到端验收：临时 SQLite 库 + 临时工作区 + 真实 app.py
HTTP 服务（ThreadingHTTPServer）+ 本脚本内起的 mock LLM 服务（纯标准库）。
mock LLM 按剧本回答：主对话走流式 tool_calls（模拟模型自己 write_file 记忆），
轮末提取走非流式 JSON（模拟"一发 JSON，宿主落盘"路径），压缩总结走流式纯文本。

逐项验收（对应任务书）：
  1. 「记住我偏好深色主题」→ .agent-memory/ 出现 md + 索引行；新会话（同
     workspace）system 消息含该索引行，agent 能答出偏好；
  2. 同一事实说两遍 → 只有一个文件（第二次是更新不是新建）；
  3. 会话触发 compact 后，system 里的记忆索引原样保留；
  4. 提取线程抛异常不影响 worker 与下一回合；连续两轮只触发一次提取（单飞）；
     提取期间前端时间线无任何新事件（SSE 静默验证）；
  5. 附带验证提取链路的宿主落盘：提取模型只回 JSON，记忆文件与索引行由
     宿主执行器写入。
"""

import json
import logging
import sys
import threading
import time
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db        # noqa: E402  （先插 sys.path 再 import，与 app.py 同一运行方式）
import tempfile

# ---------------------------------------------------------------------------
# mock LLM 服务
# ---------------------------------------------------------------------------

MOCK_MEMORY_MD = ("---\nname: dark-theme\ndescription: 用户偏好深色主题\n"
                  "metadata:\n  type: user\n---\n"
                  "用户明确表示偏好深色主题界面（2026-09-21 记录）。\n")
MEMORY_LINE = "- [dark-theme](dark-theme.md) — 用户偏好深色主题"


class MockState:
    """mock 服务的共享状态（锁保护）：请求流水 + 可切换的故障开关。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []            # 每次请求: {kind, system, last_user, t}
        self.extraction_calls = 0     # 到达 mock 的提取请求数（单飞验证用）
        self.fail_extraction = False  # 置 True：提取请求直接回 HTTP 500
        self.extraction_delay = 0.0   # 提取请求的人为延迟（单飞竞争窗口）

    def record(self, **kw):
        with self.lock:
            self.requests.append(kw)

    def snapshot(self):
        with self.lock:
            return list(self.requests), self.extraction_calls


STATE = MockState()


def index_lines_from_system(system: str) -> list[str]:
    """从注入的 system 里抠出索引行（模拟模型"看了索引再更新"的行为）。"""
    if "用户记忆索引（跨会话持久）" not in system:
        return []
    tail = system.split("用户记忆索引（跨会话持久）", 1)[1]
    lines = []
    for ln in tail.splitlines():
        ln = ln.strip()
        if ln.startswith("- [") and "(.md)" not in ln:
            lines.append(ln)
        elif ln.startswith("（"):      # （暂无记忆）/ 截断警告 跳过
            continue
        elif lines:
            break
    return lines


class MockLLMHandler(BaseHTTPRequestHandler):
    """按剧本应答的假 LLM。主对话=流式；提取/其他=非流式。"""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        users = [m for m in messages if m.get("role") == "user"]
        last_user = str((users[-1].get("content") if users else "") or "")

        # ---- 轮末提取（非流式，temperature=0）----
        if "提取器" in system:
            if STATE.fail_extraction:  # 故障注入：提取请求直接 500（测试隔离性）
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with STATE.lock:           # 计数在延迟 sleep 之前加：等待方立刻可见
                STATE.extraction_calls += 1
            STATE.record(kind="extract", last_user=last_user, t=time.time())
            texts = last_user
            if "Python" in texts:
                payload = {"memories": [{
                    "action": "write", "file": "python-script.md",
                    "frontmatter": {"name": "python-script",
                                    "description": "用户常用 Python 写脚本",
                                    "metadata": {"type": "user"}},
                    "body": "用户表示常用 Python 编写脚本。"}]}
                content = json.dumps(payload, ensure_ascii=False)
            else:
                content = "NOTHING_TO_SAVE"
            self._send_json(content)
            return

        # ---- 压缩总结（单条 user 消息，含"对话压缩器"）----
        if len(messages) == 1 and "对话压缩器" in str(messages[0].get("content") or ""):
            STATE.record(kind="compact", t=time.time())
            self._send_stream([f"（摘要）此前对话围绕记忆系统验收展开，用户偏好深色主题。"])
            return

        # ---- 主对话（流式）----
        STATE.record(kind="main", system=system, last_user=last_user, t=time.time())
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if has_tool_result:
            # 看自己上一条 assistant 的 tool_calls 写了什么，决定最终答复：
            # 写过 MEMORY.md（建新记忆）→「已记住」；只更新正文 →「已更新」
            wrote_index = any(
                "MEMORY.md" in str((call.get("function") or {}).get("arguments") or "")
                for m in messages for call in (m.get("tool_calls") or []))
            answer = "已记住：你偏好深色主题。" if wrote_index else "已更新记忆。"
            self._send_stream([answer])
            return

        has_index = MEMORY_LINE in system
        if "记住" in last_user and "深色" in last_user:
            if has_index:
                self._send_stream(["这条我已经记过了，无需重复。"])
            else:
                index = "\n".join(index_lines_from_system(system) + [MEMORY_LINE]) + "\n"
                self._send_stream([], tool_calls=[
                    {"name": "write_file", "arguments": {
                        "path": ".agent-memory/dark-theme.md", "content": MOCK_MEMORY_MD}},
                    {"name": "write_file", "arguments": {
                        "path": ".agent-memory/MEMORY.md", "content": index}},
                ])
        elif "界面偏好" in last_user or "偏好是什么" in last_user:
            self._send_stream(["你的界面偏好是深色主题。" if has_index else "我还不知道你的偏好。"])
        elif "再记一遍" in last_user:
            # 索引里已有 → 只更新正文文件，不再动 MEMORY.md（update 而非新建）
            updated = MOCK_MEMORY_MD.replace("明确表示", "再次确认自己")
            self._send_stream([], tool_calls=[
                {"name": "write_file", "arguments": {
                    "path": ".agent-memory/dark-theme.md", "content": updated}}])
        elif "还在吗" in last_user:
            self._send_stream(["在的，随时待命。"])
        else:
            self._send_stream(["收到。"])

    # ---- 应答工具 ----

    def _send_json(self, content):
        if STATE.extraction_delay:
            time.sleep(STATE.extraction_delay)
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, texts, tool_calls=None):
        def sse(obj):
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if tool_calls:
            payload = {"choices": [{"delta": {"role": "assistant", "tool_calls": [
                {"index": i, "id": f"c{i+1}", "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for i, tc in enumerate(tool_calls)]}}]}
            self.wfile.write(sse(payload))
        for t in texts:
            self.wfile.write(sse({"choices": [{"delta": {"content": t}}]}))
        self.wfile.write(sse({"choices": [{"delta": {}}],
                              "usage": {"prompt_tokens": 500, "completion_tokens": 50,
                                        "total_tokens": 550}}))
        self.wfile.write(b"data: [DONE]\n\n")


# ---------------------------------------------------------------------------
# 驱动：真实 app 服务 + HTTP 客户端 + SSE 采集
# ---------------------------------------------------------------------------

class Driver:

    def __init__(self, app_port: int, token: str):
        self.app_port = app_port
        self.token = token

    def http(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.app_port}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def submit(self, sid, message):
        path = "/api/sessions" if sid is None else f"/api/sessions/{sid}/messages"
        return self.http("POST", path, {"message": message})

    def messages(self, sid):
        return self.http("GET", f"/api/sessions/{sid}/messages")["messages"]

    def wait_answer(self, sid, fragment, timeout=15, desc=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.messages(sid):
                if m.get("role") == "assistant" and fragment in (m.get("content") or ""):
                    return m
            time.sleep(0.1)
        raise AssertionError(f"超时未等到回答（{desc or fragment}）")

    def wait_event(self, collector, etype, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hit = collector.find(etype)
            if hit:
                return hit
            time.sleep(0.05)
        raise AssertionError(f"超时未等到 SSE 事件 {etype}")


class SseCollector:
    """常驻 SSE 连接的采集器：记录 (时刻, seq, type)，供静默验证。"""

    def __init__(self, port, token, sid):
        self.records = []                      # [(t, seq, type)]
        self.lock = threading.Lock()
        conn = HTTPConnection("127.0.0.1", port, timeout=25)
        conn.request("GET", f"/api/sessions/{sid}/events?token={token}")
        resp = conn.getresponse()
        assert resp.status == 200, resp.status

        def pump():
            pending_seq = None
            try:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("id:"):
                        pending_seq = int(line[3:].strip())
                    elif line.startswith("data:"):
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        with self.lock:
                            self.records.append((time.time(), pending_seq, event.get("type")))
                        pending_seq = None
            except OSError:
                pass

        threading.Thread(target=pump, daemon=True, name="sse-collector").start()

    def find(self, etype):
        with self.lock:
            hits = [(t, seq) for t, seq, typ in self.records if typ == etype]
        return hits[-1] if hits else None

    def count(self, etype):
        with self.lock:
            return sum(1 for _, _, typ in self.records if typ == etype)

    def events_after(self, t):
        with self.lock:
            return [(seq, typ) for ts, seq, typ in self.records if ts > t]

    def max_seq(self):
        with self.lock:
            seqs = [seq for _, seq, _ in self.records if seq is not None]
        return max(seqs) if seqs else 0


def wait_until(fn, timeout, desc):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"超时：{desc}")


def wait_turn_end(collector, base, timeout=15):
    """等一个【新的】turn_end。不要用回答片段判断回合结束：片段在消息落盘
    时就能被轮询到（早于 turn_end 发布、更早于提取线程启动），会引入竞态。"""
    wait_until(lambda: collector.count("turn_end") > base, timeout, "新的 turn_end")


def main():
    # 手测也要能看见后台线程的动静：提取线程全程静默（不发事件），唯一可观测
    # 面就是日志。这里把 INFO 打到 stderr，便于失败时定位（平时输出可 grep 掉）。
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    tmp = Path(tempfile.mkdtemp(prefix="memory_e2e_"))
    ws = tmp / "workspace"
    ws.mkdir()
    db.DB_PATH = tmp / "e2e.db"           # 与单测同一套路：重定向到临时库
    db.init_db()

    # mock LLM 服务（ephemeral 端口）
    mock_server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    mock_port = mock_server.server_address[1]
    threading.Thread(target=mock_server.serve_forever, daemon=True).start()

    # 供应商/模型/用户，全部走真实 db 接口
    db.upsert_provider("mock", "MockLLM", f"http://127.0.0.1:{mock_port}",
                       "mock-key", True, "openai", context_window=300)
    db.upsert_model("mock", "mock-model", 300, True, False)   # 小窗口：逼出 compact
    db.set_setting("active_model", {"provider_id": "mock", "model": "mock-model"})
    user = db.create_user("mem_tester", "pass1234")
    token = db.create_token(user["id"])
    db.set_setting(f"default_workspace:{user['id']}", str(ws))

    # 真实 app 服务
    import app as app_mod
    app_server = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
    app_port = app_server.server_address[1]
    threading.Thread(target=app_server.serve_forever, daemon=True).start()

    drv = Driver(app_port, token)
    mem_dir = ws / ".agent-memory"
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def memory_files():
        return sorted(p.name for p in mem_dir.glob("*.md")) if mem_dir.is_dir() else []

    # ---- 验收 1a：说一遍 → 落盘 + 索引行 ----
    r = drv.submit(None, "记住我偏好深色主题")
    sid1 = r["session_id"]
    drv.wait_answer(sid1, "已记住", desc="回合1完成")
    wait_until(lambda: "dark-theme.md" in memory_files(), 5, "记忆文件出现")
    index_text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    check("验收1a 记忆文件与索引行落盘",
          "dark-theme.md" in memory_files() and MEMORY_LINE in index_text,
          f"files={memory_files()}")

    # ---- 验收 1b：新会话（同 workspace）注入索引行并能答出偏好 ----
    r = drv.submit(None, "我的界面偏好是什么？")
    sid2 = r["session_id"]
    drv.wait_answer(sid2, "深色主题", desc="新会话答出偏好")
    reqs, _ = STATE.snapshot()
    turn = [q for q in reqs if q["kind"] == "main" and "界面偏好" in q["last_user"]][-1]
    check("验收1b 新会话 system 含索引行", MEMORY_LINE in turn["system"])

    # ---- 验收 2：同一事实说两遍 → 更新而非新建 ----
    drv.submit(sid2, "再记一遍：我偏好深色主题")
    drv.wait_answer(sid2, "已更新", desc="更新完成")
    wait_until(lambda: "再次确认" in (mem_dir / "dark-theme.md").read_text(encoding="utf-8"),
               5, "正文被更新")
    check("验收2a 只有一个记忆文件", memory_files() == ["MEMORY.md", "dark-theme.md"],
          f"files={memory_files()}")
    index_text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    check("验收2b 索引行不重复", index_text.count("](dark-theme.md)") == 1)
    # 换新会话再说一遍：注入的索引让模型知道已记过，不再写盘
    r = drv.submit(None, "记住我偏好深色主题")
    sid3 = r["session_id"]
    drv.wait_answer(sid3, "记过了", desc="跨会话去重")
    check("验收2c 跨会话也去重", memory_files() == ["MEMORY.md", "dark-theme.md"],
          f"files={memory_files()}")

    # ---- 验收 3：compact 后记忆索引原样保留 ----
    # 窗口 300：keep_tail=6+min_segment=4，消息数得凑过切点下限。填充轮的
    # 回答都是"收到"，等片段会瞬间返回（匹配到上一轮），改等时间线的用户
    # 消息计数——全部落盘才算数。
    def user_count(sid):
        return len([m for m in drv.messages(sid) if m.get("role") == "user"])

    base_users = user_count(sid3)
    for _ in range(6):
        drv.submit(sid3, "继续")
    wait_until(lambda: user_count(sid3) >= base_users + 6, 20, "填充轮全部落盘")
    compacts = [m for m in drv.messages(sid3) if m.get("role") == "compact"]
    check("验收3a 会话触发 compact", len(compacts) >= 1, f"compact 边界 {len(compacts)} 条")
    reqs, _ = STATE.snapshot()
    pre = [q["system"] for q in reqs if q["kind"] == "main"
           and "记住" in q["last_user"] and "深色" in q["last_user"]][-1]
    drv.submit(sid3, "我的界面偏好是什么？")
    drv.wait_answer(sid3, "深色主题", desc="压缩后仍答出偏好")
    reqs, _ = STATE.snapshot()
    post = [q["system"] for q in reqs if q["kind"] == "main" and "界面偏好" in q["last_user"]][-1]
    check("验收3b compact 后索引原样保留",
          MEMORY_LINE in pre and MEMORY_LINE in post)

    # ---- 验收 4c（先做）：提取链路宿主落盘 + SSE 静默 ----
    r = drv.submit(None, "另外我喜欢用 Python 写脚本")
    sid4 = r["session_id"]
    collector = SseCollector(app_port, token, sid4)
    drv.wait_event(collector, "turn_start", timeout=10)
    drv.wait_answer(sid4, "收到", desc="提取载体回合")
    t_end, _ = drv.wait_event(collector, "turn_end")  # (时刻, seq)——时刻做静默窗口起点
    wait_until(lambda: "python-script.md" in memory_files(), 10, "提取写入 python-script.md")
    time.sleep(0.8)  # 留出"若有泄漏事件早就到了"的观察窗
    index_text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    check("验收4c-1 提取模型只回 JSON、宿主落盘（文件+索引行）",
          "python-script.md" in memory_files() and "python-script.md" in index_text,
          f"files={memory_files()}")
    leaked = collector.events_after(t_end)
    check("验收4c-2 提取期间时间线静默（turn_end 后无任何 SSE 事件）",
          leaked == [], f"泄漏事件={leaked}")

    # ---- 验收 4a：提取线程抛异常不影响 worker 与下一回合 ----
    base = collector.count("turn_end")
    STATE.fail_extraction = True
    drv.submit(sid4, "顺便说下我讨厌蓝色")
    wait_turn_end(collector, base)
    drv.submit(sid4, "还在吗")
    wait_turn_end(collector, base + 1)
    time.sleep(0.6)               # 失败的提取（毫秒级 500）必然已收尾、锁已释放
    STATE.fail_extraction = False
    check("验收4a 提取失败不影响 worker 与下一回合", True)

    # ---- 验收 4b：连续两轮只触发一次提取（单飞）----
    # 前置已保证 sid4 没有在途提取。第一轮的提取带着 2.5s 延迟占住单飞锁，
    # 第二轮紧接着提交：它的提取必须被跳过（到达 mock 的提取总数只 +1）。
    _, before = STATE.snapshot()
    STATE.extraction_delay = 2.5
    drv.submit(sid4, "告诉你一个新偏好：我喜欢简洁排版")
    wait_until(lambda: STATE.snapshot()[1] > before, 10, "第一次提取到达 mock")
    base = collector.count("turn_end")
    drv.submit(sid4, "再说一句：我喜欢简单措辞")   # 趁第一次提取还在跑，立刻提交下一轮
    wait_turn_end(collector, base)
    time.sleep(4.0)                                # 等第一次提取收尾 + 第二次的尝试窗口
    STATE.extraction_delay = 0.0
    _, after = STATE.snapshot()
    check("验收4b 连续两轮只触发一次提取（单飞）", after - before == 1,
          f"提取请求数 {before} → {after}")

    # ---- 收尾说明 ----
    print(f"\n工作区: {ws}\n临时库: {db.DB_PATH}")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，失败 {len(failed)} 项")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
