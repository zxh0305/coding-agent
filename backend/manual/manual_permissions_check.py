"""
权限闸门真机手测（mock LLM，不发真实网络请求）
================================================

python3 backend/manual/manual_permissions_check.py

与 manual_memory_check.py 同一套搭建：临时 SQLite 库 + 临时工作区 + 真实
app.py HTTP 服务 + 脚本内 mock LLM。区别在剧本：这回 mock 模型按「帮用户
清理临时目录」的剧本调用真实工具链——run_bash 的执行是【真的】（subprocess
真跑），高危确认也真走 SSE 弹卡 → POST 回令的完整链路。

逐项验收（对应任务书手测要求）：
  1. rm -rf 弹卡：让 agent 跑 `rm -rf /tmp/...`，事件流出现 permission_request，
     页面端点三选可用（这里直接模拟前端 POST）；
  2. 拒绝后模型收到拒绝原因并改道：deny 回令后回填 error（权限拒绝 + 原因 +
     hint），目标目录仍然存活；模型下一轮收到结果后换用「逐文件 rm」方案；
  3. 本会话内允许：第二次弹卡选 allow_session 后命令真执行、目录真被删除，
     且后续同类命令不再弹卡（会话内记住）；
  4. allow 的操作全程无感：普通命令（mkdir、echo 等）零 permission_request 直达。

安全注记：测试目标目录位于 /tmp 下由本脚本创建、内容可控；rm -rf 只会作用到
它。脚本结束时清理。
"""

import json
import logging
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent  # 脚本在 manual/ 子目录，退两级到 backend/
sys.path.insert(0, str(BACKEND_DIR))

import db          # noqa: E402
import tempfile    # noqa: E402

# ---------------------------------------------------------------------------
# mock LLM：按「清理临时目录」剧本应答
# ---------------------------------------------------------------------------


class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.roles_log = []       # 每次主对话请求的角色序列（排障用）
        self.answered_first = set()  # 已发过"首轮 tool_calls"的用户文本（剧本只在首轮发）

    def record(self, roles):
        with self.lock:
            self.roles_log.append(roles)

    def first_round_done(self, text):
        with self.lock:
            hit = text in self.answered_first
            self.answered_first.add(text)
        return hit


STATE = MockState()
# 目标目录在脚本 main() 里创建；mock 剧本与主流程共享这个路径
TARGET = None


class MockLLMHandler(BaseHTTPRequestHandler):
    """剧本模型：
    - 第一轮：收到「清理临时目录」→ 发 tool_calls: mkdir -p + rm -rf <TARGET>；
    - 之后每轮（带 tool 结果）：若最后一条工具结果是权限拒绝 → 改道
      【逐文件删除】（cd TARGET && rm 1.txt 2.txt/3.txt——rm 不带 -r，
      不会命中递归规则，但为了让剧本可控我们直接检查结果再决定）；
      若结果显示已删 → 最终总结。
    """

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []

        # ---- 轮末提取（非流式）：一律 NOTHING_TO_SAVE，避免干扰 ----
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        if "提取器" in system:
            self._send_json("NOTHING_TO_SAVE")
            return

        # ---- 主对话（流式）----
        STATE.record([m.get("role") for m in messages if isinstance(m, dict)])
        # 用户原文（剧本路由键）：过滤掉防失控注入的合成提醒（内容以"（系统提示"开头）
        user_texts = [str((m.get("content") or "")) for m in messages
                      if isinstance(m, dict) and m.get("role") == "user"
                      and "系统提示" not in str(m.get("content") or "")]
        # 新回合首轮的判定：消息列表最后一条是 user（回合继续时最后一条必然是
        # tool 结果）——不能用"有没有 assistant"判断，同一会话历史里永远有
        first_round_of_turn = messages[-1].get("role") == "user"
        if first_round_of_turn:
            if "sub3" in (user_texts[-1] if user_texts else ""):
                # 场景 B2 第一轮：同类递归删除（会话记忆应直接放行，不弹卡）
                self._send_stream([], tool_calls=[
                    {"name": "run_bash", "arguments": {"command": f"rm -rf {TARGET}/sub3"}},
                ])
            elif "再删一次" in (user_texts[-1] if user_texts else ""):
                # 场景 B 第一轮：直接发递归删除
                self._send_stream([], tool_calls=[
                    {"name": "run_bash", "arguments": {"command": f"rm -rf {TARGET}/sub2"}},
                ])
            else:
                # 场景 A 第一轮：一个 allow 命令 + 一个 rm -rf（弹卡）
                self._send_stream([], tool_calls=[
                    {"name": "run_bash", "arguments": {"command": f"mkdir -p {TARGET}/work && echo prepared"}},
                    {"name": "run_bash", "arguments": {"command": f"rm -rf {TARGET}/sub"}},
                ])
            return
        # 续答轮：只按【最后一条】工具结果路由。拒绝会留在历史里，若按"出现过"
        # 判断，改道完成后每轮都会再次改道，直到把 max_rounds 烧光
        tool_results = [m for m in messages if m.get("role") == "tool"]
        last = tool_results[-1].get("content") or ""
        if "diverted" in last:
            self._send_stream(["已改用逐文件方式清理完成。"])
            return
        if "权限拒绝" in last:
            # 改道方案：删掉目录里的单个文件（rm 不带 -r），echo 标记改道已完成
            self._send_stream([], tool_calls=[
                {"name": "run_bash",
                 "arguments": {"command": f"rm {TARGET}/sub/keep.txt && echo diverted"}},
            ])
            return
        self._send_stream(["临时目录已清理完成。"])

    # ---- 应答工具（与 manual_memory_check 相同的 OpenAI 兼容外形）----

    def _send_json(self, content):
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
# 驱动（HTTP + SSE 采集）
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

    def wait_event(self, collector, etype, timeout=15, base=0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if collector.count(etype) > base:
                return collector.find(etype)
            time.sleep(0.05)
        raise AssertionError(f"超时未等到 SSE 事件 {etype}")

    def answer(self, sid, fragment, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.messages(sid):
                if m.get("role") == "assistant" and fragment in (m.get("content") or ""):
                    return m
            time.sleep(0.1)
        raise AssertionError(f"超时未等到回答（{fragment}）")

    def tool_results(self, sid):
        """时间线接口不回工具消息；这里只用于文本回答。工具事件走 SSE。"""
        return [m for m in self.messages(sid)]


class SseCollector:
    """常驻 SSE 采集器：记录完整事件（含 payload），供弹卡与回填断言。"""

    def __init__(self, port, token, sid):
        self.events = []
        self.lock = threading.Lock()
        conn = HTTPConnection("127.0.0.1", port, timeout=60)
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
                            self.events.append((time.time(), pending_seq, event))
                        pending_seq = None
            except OSError:
                pass

        threading.Thread(target=pump, daemon=True).start()

    def find(self, etype):
        with self.lock:
            hits = [e for e in self.events if e[2].get("type") == etype]
        return hits[-1] if hits else None

    def count(self, etype):
        with self.lock:
            return sum(1 for e in self.events if e[2].get("type") == etype)

    def payloads(self, etype):
        with self.lock:
            return [e[2] for e in self.events if e[2].get("type") == etype]


def wait_until(fn, timeout, desc):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"超时：{desc}")


def resolve_permission(drv, sid, pid, decision):
    """模拟前端确认卡片的 POST 回令。返回 (ok, error)。"""
    try:
        r = drv.http("POST", f"/api/sessions/{sid}/permission/{pid}",
                     {"decision": decision})
        return bool(r.get("ok")), ""
    except urllib.error.HTTPError as e:  # 404 = 确认已失效/不存在
        return False, f"HTTP {e.code}"


def main():
    logging.basicConfig(level=logging.WARNING)  # INFO 太吵（每条工具日志一行）
    global TARGET
    tmp = Path(tempfile.mkdtemp(prefix="perm_e2e_"))
    ws = tmp / "workspace"
    ws.mkdir()
    TARGET = tmp / "target"      # rm -rf 的目标：脚本自建的 /tmp 临时目录，内容可控

    db.DB_PATH = tmp / "e2e.db"
    db.init_db()

    mock_server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    mock_port = mock_server.server_address[1]
    threading.Thread(target=mock_server.serve_forever, daemon=True).start()

    db.upsert_provider("mock", "MockLLM", f"http://127.0.0.1:{mock_port}",
                       "mock-key", True, "openai", context_window=200000)
    db.upsert_model("mock", "mock-model", 200000, True, False)
    db.set_setting("active_model", {"provider_id": "mock", "model": "mock-model"})
    user = db.create_user("perm_tester", "pass1234")
    token = db.create_token(user["id"])
    db.set_setting(f"default_workspace:{user['id']}", str(ws))

    import app as app_mod
    app_server = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
    app_port = app_server.server_address[1]
    threading.Thread(target=app_server.serve_forever, daemon=True).start()

    drv = Driver(app_port, token)
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def permission_request_with(collector, etype_base=0, timeout=15):
        """等下一条 permission_request，返回其 payload（并打印卡片要素）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with collector.lock:
                hits = [e[2] for e in collector.events if e[2].get("type") == "permission_request"]
            if len(hits) > etype_base:
                return hits[etype_base]
            time.sleep(0.05)
        raise AssertionError("超时未等到 permission_request")

    # ================= 场景 A：rm -rf 弹卡 + 拒绝 + 改道 =================
    (TARGET / "sub").mkdir(parents=True, exist_ok=True)
    (TARGET / "sub" / "keep.txt").write_text("活口", encoding="utf-8")

    r = drv.submit(None, f"请清理临时目录 {TARGET}（先建好标记再删掉子目录）")
    sid = r["session_id"]
    collector = SseCollector(app_port, token, sid)

    # A1：第一轮两条命令——mkdir（allow）+ rm -rf（ask 弹卡）
    req = permission_request_with(collector)
    check("A1 rm -rf 弹出确认卡（事件带 tool/input/reason）",
          req.get("tool") == "run_bash" and req.get("input", {}).get("command") == f"rm -rf {TARGET}/sub"
          and bool(req.get("reason")) and bool(req.get("id")),
          f"payload={req}")
    # A2：暂停点严格在调度之前——卡片未答时【整轮一个工具都没跑】
    # （mkdir 虽是 allow 也要等恢复后整轮重新分组调度，这正是设计语义）
    time.sleep(0.6)
    check("A2 ask 暂停整轮（卡未答前 allow 的 mkdir 也未执行）",
          not (TARGET / "work").exists()
          and not [p for p in collector.payloads("tool_result")],
          f"work exists={(TARGET / 'work').exists()}")

    # A3：拒绝 rm -rf
    pid = req["id"]
    ok, err = resolve_permission(drv, sid, pid, "deny")
    check("A3 deny 回令成功", ok, err)

    # A4：恢复后——allow 的 mkdir 无感直达执行（它的结果不需要任何确认）
    mkdir_ran = wait_until(lambda: any(
        "prepared" in json.dumps(p.get("result") or "", ensure_ascii=False)
        for p in collector.payloads("tool_result")), 15, "mkdir 的执行结果")
    check("A4 恢复后 allow 命令无感执行并回填", mkdir_ran)

    # A5：拒绝以工具结果回填（ok:false + 原因 + hint）
    def denied_result():
        for p in collector.payloads("tool_result"):
            if p.get("name") == "run_bash":
                try:
                    data = json.loads(p["result"])
                except (json.JSONDecodeError, KeyError):
                    continue
                if data.get("ok") is False and "权限拒绝" in str(data.get("error")):
                    return data
        return None
    denial = wait_until(denied_result, 15, "带原因的拒绝回填")
    check("A5 拒绝回填带原因（ok:false + 权限拒绝 + 原因 + hint）",
          denial is not None and "递归" in denial["error"]
          and denial.get("hint") == "可换用其它方案或请用户调整权限",
          f"result={denial}")
    # A6：拒绝即未执行——rm -rf 若真跑了会把整个 sub/ 目录删掉；目录级存活
    # 不受"模型下一轮改道只删单个文件"影响（keep.txt 的生死是 A7 的事）
    check("A6 拒绝后目标目录级存活（rm -rf 确实没执行）",
          (TARGET / "sub").exists(),
          f"sub exists={(TARGET / 'sub').exists()}")

    # A7：模型收到拒绝原因后改道（逐文件 rm keep.txt），最终总结到达
    drv.answer(sid, "清理完成", timeout=20)
    check("A7 模型改道成功（逐文件删除完成）",
          not (TARGET / "sub" / "keep.txt").exists()
          and (TARGET / "work").exists(),
          f"keep exists={(TARGET / 'sub' / 'keep.txt').exists()}, work={(TARGET / 'work').exists()}")

    # ================= 场景 B：本会话内允许 + 真删除 + 不再弹卡 =================
    base_perm = collector.count("permission_request")
    base_turn = collector.count("turn_end")
    (TARGET / "sub2").mkdir(parents=True, exist_ok=True)
    (TARGET / "sub2" / "gone.txt").write_text("将逝", encoding="utf-8")
    drv.submit(sid, f"再删一次 {TARGET}/sub2 整个目录")
    req2 = permission_request_with(collector, etype_base=base_perm)
    ok, err = resolve_permission(drv, sid, req2["id"], "allow_session")
    check("B1 allow_session 回令成功", ok, err)
    # 真删除：sub2 目录真的没了；回合正常收尾
    wait_until(lambda: collector.count("turn_end") > base_turn, 15, "回合收尾")
    deleted = wait_until(lambda: not (TARGET / "sub2").exists(), 5, "sub2 被删除")
    check("B2 本会话允许后命令真执行（目录真被删除）", deleted)
    time.sleep(0.8)   # 缓冲：若还会弹卡，事件此刻应已到达
    check("B3 会话内记住：本轮无第二次弹卡",
          collector.count("permission_request") == base_perm + 1,
          f"perm_count={collector.count('permission_request')}")

    # ================= 场景 B2：新回合同类命令 → 会话记忆放行，全程无感 =================
    base_perm = collector.count("permission_request")
    base_turn = collector.count("turn_end")
    (TARGET / "sub3").mkdir(parents=True, exist_ok=True)
    drv.submit(sid, f"把 {TARGET}/sub3 也删了（说三遍，确认已记住）")
    wait_until(lambda: collector.count("turn_end") > base_turn, 15, "B2 回合收尾")
    time.sleep(0.8)
    check("B2 跨回合同类命令不再弹卡（会话记忆生效）且真执行",
          collector.count("permission_request") == base_perm
          and not (TARGET / "sub3").exists(),
          f"perm={collector.count('permission_request')} base={base_perm}")

    # ================= 场景 C：重复消费防护 =================
    # 同一个已处理的 pid 再 POST 一次，必须 ok=false（确认不可重复消费）
    ok2, err2 = resolve_permission(drv, sid, req["id"], "allow")
    check("C1 已处理的确认请求不可重复消费", not ok2, f"err={err2}")

    # ---- 收尾 ----
    print(f"\n工作区: {ws}\n临时库: {db.DB_PATH}\n目标目录: {TARGET}")
    failed = [n for n, ok_, _ in results if not ok_]
    print(f"\n共 {len(results)} 项，失败 {len(failed)} 项")
    shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
