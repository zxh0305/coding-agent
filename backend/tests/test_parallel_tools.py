"""
工具并行执行的单元测试（cd backend && python3 -m unittest tests.test_parallel_tools -v）
================================================================================

覆盖三件事：
1. 元数据：注册表里每个工具都有 read_only 标记，未知工具按写处理（安全侧）；
2. 分组调度：连续只读工具真的并发（时间区间重叠）、写工具等前一组全部完成、
   写与写串行、读写相间时不凑组、并发上限受 max_workers 约束；
3. 顺序与隔离：结果回填顺序 = 模型请求顺序（与完成先后无关，靠 tool_call_id
   配对校验）；组内一个工具出错只污染自己的结果，同组其它工具照常返回。

慢工具/爆炸工具临时注册进真实 TOOL_REGISTRY（测完恢复），不碰网络。
端到端验收场景各有专项：两条 read_file 同轮 → 日志出现"并行执行"；
grep 抛异常 → 同轮 read_file 结果原样回填。
"""

import json
import shutil
import tempfile
import threading
import time
import unittest

import agent as agent_mod
from agent import Agent, PARALLEL_TOOL_WORKERS
from tools import TOOL_REGISTRY, TOOL_READ_ONLY, is_read_only


# ---------- 消息与调用构造 ----------

def call(cid, name, **args):
    """构造一条 OpenAI 格式的 tool_call。"""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class ScriptedLLM:
    """按剧本逐轮吐消息的假 LLM：第一轮返回 tool_calls，之后返回纯文本收尾。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None, cancel=None):
        yield "message", self.script.pop(0)


def make_probe(name, delay, journal, result=None, raise_exc=None):
    """造一个可观测的假工具：睡 delay 秒、把 (名字, 起止时间, 线程) 记进 journal。"""

    def fn(**kwargs):
        t0 = time.monotonic()
        time.sleep(delay)
        journal.append({"name": name, "start": t0, "end": time.monotonic(),
                        "thread": threading.current_thread().name})
        if raise_exc is not None:
            raise raise_exc
        return json.dumps(result or {"ok": name}, ensure_ascii=False)

    fn.__name__ = name
    return fn


def span_of(journal, name):
    """journal 里指定名字的执行区间（每个名字只用一次的测试里就是唯一一条）。"""
    return next(e for e in journal if e["name"] == name)


def max_concurrency(journal):
    """按起止时间扫描出的峰值并发数（end 排在 start 前，贴边不算重叠）。"""
    points = []
    for e in journal:
        points.append((e["start"], 1))
        points.append((e["end"], -1))
    cur = peak = 0
    for _, delta in sorted(points):
        cur += delta
        peak = max(peak, cur)
    return peak


class ParallelTestBase(unittest.TestCase):
    def make_agent(self, **kw) -> Agent:
        # 独立临时工作区：Agent.__init__ 会 prepare_workspace，别让它碰项目目录
        ws = tempfile.mkdtemp(prefix="parallel_test_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        return Agent(llm=ScriptedLLM([]), verbose=False, workspace=ws, **kw)

    def register_tool(self, name, fn, read_only=True):
        """把测试工具临时注册进真实注册表（测完恢复原状）。"""
        orig_fn, orig_ro = TOOL_REGISTRY.get(name), TOOL_READ_ONLY.get(name)
        TOOL_REGISTRY[name] = fn
        TOOL_READ_ONLY[name] = read_only

        def restore():
            if orig_fn is None:
                TOOL_REGISTRY.pop(name, None)
            else:
                TOOL_REGISTRY[name] = orig_fn
            if orig_ro is None:
                TOOL_READ_ONLY.pop(name, None)
            else:
                TOOL_READ_ONLY[name] = orig_ro

        self.addCleanup(restore)


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------

class TestReadOnlyMetadata(unittest.TestCase):

    def test_every_registered_tool_is_marked(self):
        """注册表与元数据表必须同步：新工具忘了标记会静默退化为串行（安全但白瞎），测试把它亮出来。"""
        self.assertEqual(set(TOOL_REGISTRY), set(TOOL_READ_ONLY))

    def test_flags_match_design(self):
        """五只读：read_file / list_dir / grep / calculator / current_time；其余全为写。"""
        for name in ("read_file", "list_dir", "grep", "calculator", "current_time"):
            self.assertTrue(is_read_only(name), f"{name} 应为只读")
        for name in ("write_file", "apply_patch", "run_bash", "get_weather", "analyze_image"):
            self.assertFalse(is_read_only(name), f"{name} 应按写处理")

    def test_unknown_tool_treated_as_write(self):
        """未知工具没有元数据 → 按写串行，调度永远站在安全侧。"""
        self.assertFalse(is_read_only("no_such_tool"))

    def test_worker_cap_is_four(self):
        """max_workers 按约定为 4（组内并发上限，见 _run_tool_group）。"""
        self.assertEqual(PARALLEL_TOOL_WORKERS, 4)


# ---------------------------------------------------------------------------
# 分组调度：并发关系与屏障
# ---------------------------------------------------------------------------

class TestScheduling(ParallelTestBase):

    def run_calls(self, calls):
        agent = self.make_agent()
        events = list(agent._execute_tool_calls(calls))
        return agent, events

    def test_consecutive_readonly_tools_run_concurrently(self):
        """两个连续只读工具：执行区间必须重叠（串行则是首尾相接），且跑在不同池线程上。"""
        journal = []
        for n in ("probe_a", "probe_b"):
            self.register_tool(n, make_probe(n, 0.25, journal), read_only=True)
        self.run_calls([call("1", "probe_a"), call("2", "probe_b")])
        a, b = span_of(journal, "probe_a"), span_of(journal, "probe_b")
        self.assertLess(b["start"], a["end"])   # b 开工时 a 还没结束 → 真并发
        self.assertLess(a["start"], b["end"])
        self.assertNotEqual(a["thread"], b["thread"])

    def test_write_waits_for_whole_read_group(self):
        """读组在前、写在后：写必须等读组【全部】完成（组间屏障，不能读到"尚未发生的写"之外的乱序）。"""
        journal = []
        self.register_tool("probe_read", make_probe("probe_read", 0.25, journal), True)
        self.register_tool("probe_write", make_probe("probe_write", 0.0, journal), False)
        self.run_calls([call("1", "probe_read"), call("2", "probe_write")])
        r, w = span_of(journal, "probe_read"), span_of(journal, "probe_write")
        self.assertGreaterEqual(w["start"], r["end"])

    def test_read_after_write_waits_for_write(self):
        """写在前、读在后：读必须等写落地（要读到写的结果）。"""
        journal = []
        self.register_tool("probe_write", make_probe("probe_write", 0.15, journal), False)
        self.register_tool("probe_read", make_probe("probe_read", 0.0, journal), True)
        self.run_calls([call("1", "probe_write"), call("2", "probe_read")])
        w, r = span_of(journal, "probe_write"), span_of(journal, "probe_read")
        self.assertGreaterEqual(r["start"], w["end"])

    def test_write_write_serial(self):
        """写与写之间串行：第二个写开工前第一个写必须已结束。"""
        journal = []
        for n in ("probe_w1", "probe_w2"):
            self.register_tool(n, make_probe(n, 0.15, journal), read_only=False)
        self.run_calls([call("1", "probe_w1"), call("2", "probe_w2")])
        w1, w2 = span_of(journal, "probe_w1"), span_of(journal, "probe_w2")
        self.assertGreaterEqual(w2["start"], w1["end"])

    def test_interleaved_reads_are_not_grouped(self):
        """读写相间：两个 read 不相邻 → 各自单独执行，不出现"并行执行"日志。"""
        journal = []
        self.register_tool("probe_read", make_probe("probe_read", 0.0, journal), True)
        self.register_tool("probe_write", make_probe("probe_write", 0.0, journal), False)
        with self.assertLogs("agent", level="INFO") as cm:
            self.run_calls([call("1", "probe_read"), call("2", "probe_write"),
                            call("3", "probe_read")])
        self.assertFalse(any("并行执行" in line for line in cm.output),
                         f"不相邻的只读工具不该凑组: {cm.output}")

    def test_concurrency_capped_by_workers(self):
        """6 个只读工具同组：峰值并发 ≤ max_workers（4），且确实并发了（≥2）。"""
        journal = []
        for i in range(6):
            self.register_tool(f"probe_{i}", make_probe(f"probe_{i}", 0.15, journal), True)
        agent = self.make_agent()
        t0 = time.monotonic()
        list(agent._execute_tool_calls([call(str(i), f"probe_{i}") for i in range(6)]))
        wall = time.monotonic() - t0
        peak = max_concurrency(journal)
        self.assertLessEqual(peak, PARALLEL_TOOL_WORKERS)
        self.assertGreaterEqual(peak, 2)
        # 6 个 0.15s 的任务 ÷ 4 个线程 = 至少两波：总耗时下限可作确定性断言
        self.assertGreaterEqual(wall, 2 * 0.15 - 0.02)


# ---------------------------------------------------------------------------
# 顺序保证：回填顺序 = 请求顺序
# ---------------------------------------------------------------------------

class TestOrdering(ParallelTestBase):

    def test_results_filled_in_request_order_despite_completion_order(self):
        """后提交的先完成：回填仍必须是请求顺序（fast 先跑完也不能排到前面）。"""
        journal = []
        self.register_tool("probe_slow", make_probe("probe_slow", 0.3, journal, result={"who": "slow"}), True)
        self.register_tool("probe_fast", make_probe("probe_fast", 0.0, journal, result={"who": "fast"}), True)
        agent = self.make_agent()
        events = list(agent._execute_tool_calls(
            [call("id_slow", "probe_slow"), call("id_fast", "probe_fast")]))
        # 前置确认：完成顺序确实是 fast 在前，否则这个测试没测到点子上
        fast, slow = span_of(journal, "probe_fast"), span_of(journal, "probe_slow")
        self.assertLess(fast["end"], slow["end"])
        # 事件与历史都按请求顺序回填，tool_call_id 一一配对
        self.assertEqual([e[1]["name"] for e in events], ["probe_slow", "probe_fast"])
        self.assertIn("slow", events[0][1]["result"])
        self.assertIn("fast", events[1][1]["result"])
        history_tools = [m for m in agent.history if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in history_tools], ["id_slow", "id_fast"])

    def test_end_to_end_two_read_files_logged_as_parallel(self):
        """验收场景 1：模型一轮返回 2 个 read_file → 日志出现"并行执行"，事件流与历史配对正确。"""
        agent = self.make_agent()
        ws = agent.ctx.workspace
        (ws / "a.txt").write_text("内容甲", encoding="utf-8")
        (ws / "b.txt").write_text("内容乙", encoding="utf-8")
        agent.llm = ScriptedLLM([
            tool_call_message(call("c1", "read_file", path="a.txt"),
                              call("c2", "read_file", path="b.txt")),
            {"role": "assistant", "content": "两个文件都读完了"},
        ])
        with self.assertLogs("agent", level="INFO") as cm:
            events = list(agent.run("读一下这两个文件"))
        self.assertEqual([k for k, _ in events],
                         ["round", "tool_call", "tool_call", "tool_result", "tool_result",
                          "round", "done"])
        self.assertIn("内容甲", events[3][1]["result"])
        self.assertIn("内容乙", events[4][1]["result"])
        self.assertTrue(any("并行执行" in line and "read_file" in line for line in cm.output),
                        f"日志中应出现并行执行记录: {cm.output}")
        # 历史收尾：assistant(tool_calls) 后跟两条按请求顺序配对的 tool 消息
        tail = agent.history[-4:]
        self.assertTrue(tail[0].get("tool_calls"))
        self.assertEqual([m.get("tool_call_id") for m in tail[1:3]], ["c1", "c2"])


# ---------------------------------------------------------------------------
# 故障隔离：单个工具出错不殃及同组
# ---------------------------------------------------------------------------

class TestErrorIsolation(ParallelTestBase):

    def test_failing_grep_does_not_affect_read_file(self):
        """验收场景 2：同轮 grep 炸了 → 只有它自己的结果变成 error，read_file 原样回填。"""
        agent = self.make_agent()
        orig_grep = TOOL_REGISTRY["grep"]

        def boom(**kwargs):
            raise RuntimeError("grep 内部炸了")

        TOOL_REGISTRY["grep"] = boom
        self.addCleanup(lambda: TOOL_REGISTRY.__setitem__("grep", orig_grep))
        (agent.ctx.workspace / "a.txt").write_text("平安无事", encoding="utf-8")

        events = list(agent._execute_tool_calls(
            [call("g1", "grep", pattern="x"), call("r1", "read_file", path="a.txt")]))
        self.assertIn("error", json.loads(events[0][1]["result"]))
        self.assertIn("grep 内部炸了", json.loads(events[0][1]["result"])["error"])
        self.assertIn("平安无事", events[1][1]["result"])
        # 错误也是给模型的正式反馈，必须同样进历史（两条 tool 消息都在）
        self.assertEqual(len([m for m in agent.history if m["role"] == "tool"]), 2)

    def test_run_tool_never_raises(self):
        """_run_tool 的"绝不抛"承诺：畸形 call / arguments 非法 / execute_tool 炸，都返回 error 字符串。"""
        agent = self.make_agent()
        self.assertIn("error", json.loads(agent._run_tool({"id": "x"})))  # 连 function 都没有
        bad_args = agent._run_tool({"id": "y", "function": {"name": "read_file",
                                                            "arguments": "不是json"}})
        self.assertIn("error", json.loads(bad_args))
        orig = agent_mod.execute_tool

        def explode(name, arguments, ctx=None):
            raise RuntimeError("执行器炸了")

        agent_mod.execute_tool = explode
        self.addCleanup(lambda: setattr(agent_mod, "execute_tool", orig))
        r3 = agent._run_tool(call("z", "read_file", path="a.txt"))
        self.assertIn("执行器炸了", json.loads(r3)["error"])

    def test_group_survives_run_tool_exception(self):
        """双保险路径：工作线程里 _run_tool 真抛了（理论不该发生），
        收集处也只把它单独转成 error，同组其它结果不受连坐。"""
        agent = self.make_agent()
        real_run_tool = agent._run_tool

        def flaky(c):
            if c["function"]["name"] == "probe_bad":
                raise RuntimeError("线程里炸了")
            return real_run_tool(c)

        agent._run_tool = flaky
        (agent.ctx.workspace / "ok.txt").write_text("好的", encoding="utf-8")
        results = agent._run_tool_group(
            [call("b1", "probe_bad"), call("g1", "read_file", path="ok.txt")])
        self.assertIn("线程里炸了", json.loads(results[0])["error"])
        self.assertIn("好的", results[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
