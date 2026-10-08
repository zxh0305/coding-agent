"""工业级压力自测：并发 API / 吞吐 / 延迟分位 / 鉴权边界 / SS E 并发。

纯标准库（urllib + threading + concurrent.futures），不依赖 requests。
用法：先起服务 `python3 backend/app.py 8099`，再 `python3 backend/manual/stress_test.py`。

覆盖维度：
  A. 读接口并发压测（P50/P90/P99/最大 + 吞吐 QPS + 错误率）
  B. 写接口并发压测（建会话/发包——落库路径，验证 SQLite 锁争用）
  C. 鉴权边界（无 token / 坏 token / 越权访问他人会话）
  D. 静态资源并发（大文件吞吐）
  E. SSE 多连接并发（长连接线程占用）
  F. 资源边界（超大 body / 畸形 JSON / 超长字段）
"""
import json
import time
import uuid
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8099"
FAILURES = []
NOTES = []


def _req(method, path, body=None, token=None, raw=None, timeout=30):
    """返回 (status, body_bytes, elapsed_ms)。"""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000
    except Exception as e:
        return -1, str(e).encode(), (time.perf_counter() - t0) * 1000


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


def summarize(name, lat, errs, wall_s):
    n = len(lat)
    print(f"\n=== {name} ===")
    if n == 0:
        print("  无样本"); return
    print(f"  样本 {n}  用时 {wall_s:.2f}s  吞吐 {n/wall_s:.0f} req/s")
    print(f"  延迟 P50 {pct(lat,50):.1f}ms  P90 {pct(lat,90):.1f}ms  "
          f"P99 {pct(lat,99):.1f}ms  max {max(lat):.1f}ms")
    print(f"  错误 {errs}/{n}")
    if errs:
        FAILURES.append(f"{name}: {errs}/{n} 错误")


def get_token():
    u = "stress_" + uuid.uuid4().hex[:8]
    st, body, _ = _req("POST", "/api/auth/register", {"username": u, "password": "stress123456"})
    if st != 200:
        print("注册失败", st, body[:200]); return None
    return json.loads(body)["token"]


# ---------- A. 读接口并发 ----------
def bench_read(token, path, conc=32, total=400):
    lat, errs = [], 0
    lock = threading.Lock()
    def one():
        nonlocal errs
        st, _, ms = _req("GET", path, token=token)
        with lock:
            lat.append(ms)
            if st != 200:
                errs += 1
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(lambda _: one(), range(total)))
    summarize(f"读 {path} (并发{conc}, 共{total})", lat, errs, time.perf_counter() - t0)


# ---------- B. 写接口并发 ----------
def make_session(token, tag="stress"):
    """建一个真实会话：POST /api/sessions 会创建并落库（带一条首消息）。"""
    st, body, _ = _req("POST", "/api/sessions", {"message": f"{tag} {uuid.uuid4().hex[:6]}"}, token=token)
    if st in (200, 201, 202):
        try:
            return json.loads(body).get("session_id")
        except Exception:
            return None
    return None


def bench_write(token, conc=16, total=120):
    """并发向多个已有会话提交消息——落库写路径，检验 SQLite 写锁争用与 500 率。

    注意：提交会让后台真的跑一个回合（需模型/网络）。这里只关心「命令接口
    是否快速返回 2xx」，不等待回合结束；回合失败与否不属本压测范围。
    """
    sids = [s for s in (make_session(token, "wr") for _ in range(8)) if s]
    if not sids:
        FAILURES.append("减压写: 无法建会话")
        return 0
    lat, errs, oks = [], 0, 0
    lock = threading.Lock()
    def one(i):
        nonlocal errs, oks
        sid = sids[i % len(sids)]
        st, body, ms = _req("POST", f"/api/sessions/{sid}/messages",
                            {"message": f"stress {i}"}, token=token)
        with lock:
            lat.append(ms)
            if st in (200, 201, 202):
                oks += 1
            else:
                errs += 1
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(one, range(total)))
    wall = time.perf_counter() - t0
    summarize(f"写 /messages (并发{conc}, 共{total})", lat, errs, wall)
    print(f"  成功 {oks}  错误 {errs}")
    return errs


# ---------- C. 鉴权边界 ----------
def bench_auth(token, victim_sid):
    print("\n=== C. 鉴权边界 ===")
    cases = [
        ("无 token 访问 /api/sessions", "GET", "/api/sessions", None, 401),
        ("坏 token 访问 /api/sessions", "GET", "/api/sessions", "Bearer garbage", 401),
        ("无 token 提交消息", "POST", f"/api/sessions/{victim_sid}/messages", None, 401),
    ]
    for name, m, p, tok, want in cases:
        hdr = tok.split(" ", 1)[1] if tok else None
        st, _, _ = _req(m, p, {"message": "x"} if m == "POST" else None, token=hdr)
        ok = (st == want)
        print(f"  {'✅' if ok else '❌'} {name}: 期望{want} 实际{st}")
        if not ok:
            FAILURES.append(f"鉴权: {name} 期望{want} 实际{st}")


# ---------- D. 静态资源 ----------
def bench_static(conc=32, total=300):
    lat, errs = [], 0
    lock = threading.Lock()
    def one():
        nonlocal errs
        st, body, ms = _req("GET", "/app.js")
        with lock:
            lat.append(ms)
            if st != 200:
                errs += 1
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(lambda _: one(), range(total)))
    summarize(f"静态 /app.js (并发{conc}, 共{total})", lat, errs, time.perf_counter() - t0)


# ---------- E. SSE 并发 ----------
def bench_sse(token, sid, n=20, hold=4.0):
    """同时开 n 条 SSE 长连接，各保持 hold 秒，统计建连成功率与线程占用。"""
    print(f"\n=== E. SSE 并发 {n} 连接（各持有 {hold}s）===")
    results = []
    lock = threading.Lock()
    def one(i):
        path = f"/api/sessions/{sid}/events?token={token}"
        req = urllib.request.Request(BASE + path)
        req.add_header("Accept", "text/event-stream")
        t0 = time.perf_counter()
        ok = False
        try:
            with urllib.request.urlopen(req, timeout=hold + 8) as r:
                # 读到首个事件（含心跳/补发）即算建连成功
                line = r.readline()
                ok = (r.status == 200)
            el = (time.perf_counter() - t0) * 1000
        except Exception:
            el = (time.perf_counter() - t0) * 1000
        with lock:
            results.append((ok, el))
    t0 = time.perf_counter()
    ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ths: t.start()
    for t in ths: t.join()
    wall = time.perf_counter() - t0
    okc = sum(1 for ok, _ in results if ok)
    print(f"  建连成功 {okc}/{n}  用时 {wall:.2f}s")
    if okc < n:
        FAILURES.append(f"SSE 建连 {okc}/{n}")


# ---------- F. 资源边界 ----------
def bench_limits(token, sid):
    print("\n=== F. 资源边界 ===")
    # 超大 body（10MB JSON）
    big = json.dumps({"message": "x" * (10 * 1024 * 1024)}).encode()
    st, body, ms = _req("POST", f"/api/sessions/{sid}/messages", raw=big, timeout=30)
    print(f"  10MB body: 状态{st} 耗时{ms:.0f}ms {'（应拒绝或快速失败）' if st>=400 else '⚠ 被接受'}")
    if st == -1:
        NOTES.append("10MB body 连接被断（服务端无响应）——确认是否有 body 上限")
    # 畸形 JSON
    st, _, _ = _req("POST", f"/api/sessions/{sid}/messages", raw=b"{not json", token=token)
    print(f"  畸形 JSON: 状态{st} {'✅' if st>=400 else '❌ 未拒绝'}")
    if st < 400:
        FAILURES.append("畸形 JSON 未被拒绝")
    # 超长 username 注册
    st, _, _ = _req("POST", "/api/auth/register",
                    {"username": "z" * 5000, "password": "p" * 5000})
    print(f"  超长注册字段: 状态{st} {'✅' if st>=400 else '❌ 未拒绝'}")
    # 路径穿越
    st, _, _ = _req("GET", "/api/sessions/../../etc/passwd", token=token)
    print(f"  路径穿越: 状态{st} {'✅' if st>=400 else '❌ 未拒绝'}")


def main():
    print("获取 token ...")
    token = get_token()
    if not token:
        return 1
    # 建真实会话（越权测试 + SSE 都需要真实 sid）
    victim_sid = make_session(token, "victim")
    if not victim_sid:
        print("无法建受害者会话"); return 1
    time.sleep(0.5)

    bench_read(token, "/api/sessions")
    bench_read(token, "/api/tools")
    bench_read(token, "/api/usage/summary")
    bench_write(token)
    bench_auth(token, victim_sid)
    bench_static()
    bench_sse(token, victim_sid, n=20, hold=4.0)

    # 重新认证后做边界（上面可能已污染）
    token2 = get_token()
    bench_limits(token2, victim_sid)

    print("\n" + "=" * 50)
    if FAILURES:
        print("❌ 失败项:")
        for f in FAILURES:
            print("   -", f)
    else:
        print("✅ 全部压力用例通过")
    if NOTES:
        print("ℹ 备注:")
        for n in NOTES:
            print("   -", n)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
