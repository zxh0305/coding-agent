"""
持久记忆的单元测试（纯标准库 unittest，不碰网络与真实 LLM）
============================================================

cd backend && python3 -m unittest tests.test_memory -v

覆盖五块：
1. 文件名校验（非法字符 / ../ / 绝对路径 / 非 .md 全拒绝）与 frontmatter
   解析（三字段缺一不可）；
2. 索引行生成/更新/删除，150 行 / 20000 字符注入截断告警；
3. 宿主执行器 apply_extraction：write 落盘 + 索引同步、delete 清索引、
   非法操作全有或全无放弃、body 8000 字符截断、.tmp + os.replace 原子覆盖；
4. 单飞锁：上一次未完成时第二次调用直接跳过（不排队不重入）；
5. 注入拼接：system 段包含契约与索引；索引损坏/缺失降级为空，不抛异常；
   Agent 级验证记忆只进 system、绝不进消息历史（compact 兼容的前提）。
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import memory
from agent import Agent
from memory import (BODY_MAX_CHARS, INDEX_MAX_CHARS, INDEX_MAX_LINES, MEMORY_CONTRACT,
                    apply_extraction, build_extraction_messages, memory_dir,
                    parse_extraction_reply, parse_frontmatter, recent_user_texts,
                    remove_index_line, render_index_line, render_memory_file,
                    scan_inventory, system_memory_block, truncate_index,
                    upsert_index_line, valid_memory_filename)


def make_mem_dir(case: unittest.TestCase) -> Path:
    """每个用例一个独立临时记忆目录（惰性创建语义：初始不落盘）。"""
    d = Path(tempfile.mkdtemp(prefix="memory_test_")) / ".agent-memory"
    case.addCleanup(shutil.rmtree, d.parent, ignore_errors=True)
    return d


def write_op(fname="dark-theme.md", description="用户偏好深色主题", mtype="user",
             body="用户明确表示偏好深色主题界面。", **overrides):
    """构造一条合法 write 操作，overrides 逐键覆盖（value=None 表示删除该键）。"""
    op = {"action": "write", "file": fname,
          "frontmatter": {"name": fname[:-3], "description": description,
                          "metadata": {"type": mtype}},
          "body": body}
    for k, v in overrides.items():
        if v is None:
            op.pop(k, None)
        else:
            op[k] = v
    return op


def seed_memory(mem_dir: Path, fname: str, description: str, mtype="user",
                body="正文") -> None:
    """直接落一条记忆 + 索引行（绕过执行器，做删除/注入类测试的底料）。"""
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / fname).write_text(
        render_memory_file(fname[:-3], description, mtype, body), encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        render_index_line(fname, description) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 一、文件名与 frontmatter
# ---------------------------------------------------------------------------

class TestFilename(unittest.TestCase):

    def test_valid_names(self):
        for name in ("a.md", "0abc.md", "dark-theme-pref.md", "user-py-style.md"):
            self.assertTrue(valid_memory_filename(name), name)

    def test_invalid_names(self):
        bad = ["", ".md", "A.md", "-a.md", "a_b.md", "a b.md", "a.md.txt",
               "x.txt", "../x.md", "../../etc/passwd.md", "/tmp/x.md",
               "a/b.md", "a.md/", "中文.md", "a.md ", None, 123]
        for name in bad:
            self.assertFalse(valid_memory_filename(name), repr(name))


class TestFrontmatter(unittest.TestCase):

    def test_render_parse_roundtrip(self):
        text = render_memory_file("dark-theme", "用户偏好深色主题", "user", "正文一段话。")
        meta, body = parse_frontmatter(text)
        self.assertEqual(meta, {"name": "dark-theme", "description": "用户偏好深色主题",
                                "type": "user"})
        self.assertEqual(body, "正文一段话。")

    def test_description_newline_flattened(self):
        """description 要进索引行，渲染时必须压成单行（否则一行一条被破坏）。"""
        text = render_memory_file("x", "第一行\n第二行", "user", "b")
        meta, _ = parse_frontmatter(text)
        self.assertEqual(meta["description"], "第一行 第二行")

    def test_missing_any_field_rejected(self):
        base = "---\nname: n\ndescription: d\nmetadata:\n  type: user\n---\nbody"
        variants = [
            base.replace("name: n\n", ""),          # 缺 name
            base.replace("description: d\n", ""),   # 缺 description
            base.replace("  type: user\n", ""),     # 缺 metadata.type
            base.replace("---\n", "", 1),           # 没有起始围栏
            "只有正文，没有 frontmatter",
        ]
        for text in variants:
            meta, _ = parse_frontmatter(text)
            self.assertIsNone(meta, f"应拒绝：{text!r}")

    def test_body_preserved_when_meta_broken(self):
        meta, body = parse_frontmatter("---\nname: n\n---\n保留的正文")
        self.assertIsNone(meta)
        self.assertEqual(body, "保留的正文")


# ---------------------------------------------------------------------------
# 二、索引行与截断
# ---------------------------------------------------------------------------

class TestIndex(unittest.TestCase):

    def test_render_line_format(self):
        self.assertEqual(render_index_line("dark-theme.md", "用户偏好深色主题"),
                         "- [dark-theme](dark-theme.md) — 用户偏好深色主题")

    def test_upsert_appends_and_replaces(self):
        idx = render_index_line("a.md", "旧描述") + "\n"
        idx = upsert_index_line(idx, "b.md", "另一条")
        self.assertEqual(idx.count("\n"), 2)
        idx = upsert_index_line(idx, "a.md", "新描述")
        self.assertIn("新描述", idx)
        self.assertNotIn("旧描述", idx)
        self.assertEqual(idx.count("](a.md)"), 1)  # 按链接去重，不产生重复行

    def test_remove_line(self):
        idx = render_index_line("a.md", "甲") + "\n" + render_index_line("b.md", "乙") + "\n"
        idx = remove_index_line(idx, "a.md")
        self.assertNotIn("a.md", idx)
        self.assertIn("b.md", idx)
        self.assertEqual(remove_index_line(idx, "b.md"), "")

    def test_truncate_by_lines_with_warning(self):
        idx = "".join(render_index_line(f"m{i}.md", f"第{i}条") + "\n"
                      for i in range(INDEX_MAX_LINES + 50))
        out = truncate_index(idx)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), INDEX_MAX_LINES + 1)  # 150 条 + 1 行警告
        self.assertIn("警告", lines[-1])
        self.assertIn("仅加载了", lines[-1])

    def test_truncate_by_chars_with_warning(self):
        idx = "".join(render_index_line(f"m{i}.md", "长" * 500) + "\n"
                      for i in range(50))  # 每行 ~520 字符 × 50 ≫ 20000
        out = truncate_index(idx)
        self.assertLess(len(out), len(idx))              # 确实截短了
        self.assertLess(len(out), INDEX_MAX_CHARS + 200)  # 正文不超限（警告行除外）
        self.assertIn("警告", out)
        self.assertIn("仅加载了", out)

    def test_small_index_untouched(self):
        idx = render_index_line("a.md", "描述") + "\n"
        self.assertEqual(truncate_index(idx), idx)
        self.assertEqual(truncate_index(""), "")

    def test_inventory_scan(self):
        mem_dir = make_mem_dir(self)
        seed_memory(mem_dir, "a.md", "甲")
        seed_memory(mem_dir, "b.md", "乙", mtype="project")
        (mem_dir / "MEMORY.md").write_text("- 索引不算记忆\n", encoding="utf-8")
        (mem_dir / "broken.md").write_text("没有 frontmatter", encoding="utf-8")
        items = scan_inventory(mem_dir)
        self.assertEqual(sorted(i["file"] for i in items), ["a.md", "b.md", "broken.md"])
        # 索引文件不算记忆；frontmatter 损坏的文件仍在清单里（type/description
        # 留空）——让提取模型看得见它，才有机会修复或删除
        broken = next(i for i in items if i["file"] == "broken.md")
        self.assertEqual(broken["type"], "")
        self.assertEqual(broken["description"], "")
        b = next(i for i in items if i["file"] == "b.md")
        self.assertEqual(b["type"], "project")
        self.assertEqual(scan_inventory(mem_dir.parent / "不存在"), [])


# ---------------------------------------------------------------------------
# 三、宿主执行器 apply_extraction
# ---------------------------------------------------------------------------

class TestApplyExtraction(unittest.TestCase):

    def test_write_creates_file_and_index(self):
        mem_dir = make_mem_dir(self)
        result = apply_extraction(mem_dir, {"memories": [write_op()]})
        self.assertEqual(result["written"], ["dark-theme.md"])
        self.assertIsNone(result["abandoned"])
        meta, body = parse_frontmatter((mem_dir / "dark-theme.md").read_text(encoding="utf-8"))
        self.assertEqual(meta["name"], "dark-theme")
        self.assertEqual(meta["type"], "user")
        self.assertIn("深色", body)
        index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn(render_index_line("dark-theme.md", "用户偏好深色主题"), index)

    def test_write_second_time_updates_not_duplicates(self):
        """同一文件写两次：正文覆盖、索引仍只有一行（update 语义）。"""
        mem_dir = make_mem_dir(self)
        apply_extraction(mem_dir, {"memories": [write_op(body="v1")]})
        apply_extraction(mem_dir, {"memories": [write_op(body="v2", description="新钩子")]})
        self.assertEqual(len(list(mem_dir.glob("*.md"))) - 1, 1)  # 除 MEMORY.md 外 1 个
        self.assertIn("v2", (mem_dir / "dark-theme.md").read_text(encoding="utf-8"))
        index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("](dark-theme.md)"), 1)
        self.assertIn("新钩子", index)

    def test_body_truncated_to_8000(self):
        mem_dir = make_mem_dir(self)
        apply_extraction(mem_dir, {"memories": [write_op(body="长" * (BODY_MAX_CHARS + 100))]})
        _, body = parse_frontmatter((mem_dir / "dark-theme.md").read_text(encoding="utf-8"))
        self.assertEqual(len(body), BODY_MAX_CHARS)

    def test_atomic_write_via_tmp_and_replace(self):
        """原子覆盖：必须先写同目录 .tmp，再 os.replace 到正式名（不留 .tmp）。"""
        mem_dir = make_mem_dir(self)
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((str(src), str(dst)))
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=spy):
            apply_extraction(mem_dir, {"memories": [write_op()]})
        self.assertEqual(len(seen), 2)  # 记忆文件 + 索引各一次
        for src, dst in seen:
            self.assertTrue(src.endswith(".tmp"))
            self.assertEqual(Path(src).parent, Path(dst).parent)  # 同目录 → 同文件系统
        self.assertEqual(list(mem_dir.glob("*.tmp")), [])      # 不残留临时文件

    def test_rejects_escape_and_absolute_and_illegal_names(self):
        mem_dir = make_mem_dir(self)
        for fname in ("../evil.md", "/tmp/evil.md", "sub/dir.md", "OK.md", "x.txt"):
            result = apply_extraction(mem_dir, {"memories": [write_op(fname=fname)]})
            self.assertIsNotNone(result["abandoned"], fname)
            self.assertEqual(result["written"], [])
            self.assertFalse(mem_dir.exists(), "放弃时磁盘一个字节都不该动")

    def test_rejects_illegal_ops(self):
        mem_dir = make_mem_dir(self)
        cases = [
            write_op(action="upsert"),                       # 非法 action
            write_op(mtype="secret"),                        # 非法 type
            write_op(description=""),                        # 空 description
            write_op(frontmatter=None),                      # 缺 frontmatter
            write_op(body=None),                             # 缺 body
            {"memories": "不是列表"},                          # 顶层形状错误（单测走 payload）
        ]
        for op in cases:
            payload = op if op.get("memories") else {"memories": [op]}
            result = apply_extraction(mem_dir, payload)
            self.assertIsNotNone(result["abandoned"], op)
            self.assertFalse(mem_dir.exists())

    def test_all_or_nothing_on_mixed_batch(self):
        """合法与非法混合：整批放弃（半套记忆比没有更糟）。"""
        mem_dir = make_mem_dir(self)
        result = apply_extraction(mem_dir, {"memories": [write_op(), write_op(fname="../x.md")]})
        self.assertIsNotNone(result["abandoned"])
        self.assertFalse(mem_dir.exists())

    def test_delete_removes_file_and_index_line(self):
        mem_dir = make_mem_dir(self)
        seed_memory(mem_dir, "old.md", "过时的记忆")
        result = apply_extraction(mem_dir, {"memories": [{"action": "delete", "file": "old.md"}]})
        self.assertEqual(result["deleted"], ["old.md"])
        self.assertFalse((mem_dir / "old.md").exists())
        index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        self.assertNotIn("old.md", index)

    def test_delete_missing_file_is_idempotent(self):
        mem_dir = make_mem_dir(self)
        result = apply_extraction(mem_dir, {"memories": [{"action": "delete", "file": "ghost.md"}]})
        self.assertEqual(result["deleted"], [])
        self.assertIsNone(result["abandoned"])

    def test_delete_still_respects_whitelist(self):
        mem_dir = make_mem_dir(self)
        result = apply_extraction(mem_dir, {"memories": [{"action": "delete", "file": "../x.md"}]})
        self.assertIsNotNone(result["abandoned"])


# ---------------------------------------------------------------------------
# 四、提取回复解析与输入构造
# ---------------------------------------------------------------------------

class TestExtractionParsing(unittest.TestCase):

    def test_nothing_to_save(self):
        self.assertEqual(parse_extraction_reply("NOTHING_TO_SAVE"), [])
        self.assertEqual(parse_extraction_reply("好的，NOTHING_TO_SAVE"), [])

    def test_plain_and_fenced_json(self):
        ops = [write_op()]
        for text in (json.dumps({"memories": ops}, ensure_ascii=False),
                     f"```json\n{json.dumps({'memories': ops}, ensure_ascii=False)}\n```",
                     f"提取结果如下：\n{json.dumps({'memories': ops}, ensure_ascii=False)}\n以上。"):
            self.assertEqual(parse_extraction_reply(text), ops)

    def test_unparseable_returns_none(self):
        for text in ("", "   ", "我觉得没啥可记的", "{broken", "[1,2,3]",
                     json.dumps({"other": 1})):
            self.assertIsNone(parse_extraction_reply(text), text)

    def test_build_messages_contains_inventory_and_texts(self):
        msgs = build_extraction_messages([{"file": "a.md", "type": "user", "description": "甲"}],
                                         ["第一句", "第二句"])
        self.assertEqual(msgs[0]["role"], "system")
        joined = msgs[1]["content"]
        self.assertIn("a.md", joined)
        self.assertIn("甲", joined)
        self.assertIn("第一句", joined)
        self.assertIn("NOTHING_TO_SAVE", msgs[0]["content"])  # 输出契约写进 system


class TestRecentUserTexts(unittest.TestCase):

    def test_filters_non_user_and_short_messages(self):
        history = [
            {"role": "user", "content": "记住我偏好深色主题"},      # 9 词，保留
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "继续"},                    # 2 词，滤掉
            {"role": "user", "content": "好的"},                    # 2 词，滤掉
            {"role": "tool", "tool_call_id": "x", "content": "..."},
            {"role": "user", "content": "另外我喜欢用 Python 写脚本"},  # 保留
        ]
        self.assertEqual(recent_user_texts(history),
                         ["记住我偏好深色主题", "另外我喜欢用 Python 写脚本"])

    def test_window_is_last_n_messages(self):
        history = [{"role": "user", "content": f"第{i}条较长的发言内容"} for i in range(30)]
        texts = recent_user_texts(history)
        self.assertEqual(len(texts), 20)
        self.assertEqual(texts[0], "第10条较长的发言内容")
        self.assertEqual(texts[-1], "第29条较长的发言内容")

    def test_multipart_content_extracts_text_parts(self):
        history = [{"role": "user", "content": [
            {"type": "text", "text": "带图的正式提问"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]}]
        self.assertEqual(recent_user_texts(history), ["带图的正式提问"])

    def test_skips_synthetic_messages(self):
        """_synthetic 合成消息（收尾指令/循环提醒，运行时构造）不是用户说的话，
        提取输入必须跳过——否则"不要再调用任何工具"会被提炼成记忆。"""
        history = [
            {"role": "user", "content": "记住我偏好深色主题"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "_synthetic": True,
             "content": "本轮工具调用轮数已达上限（16）。不要再调用任何工具——请直接输出总结。"},
        ]
        self.assertEqual(recent_user_texts(history), ["记住我偏好深色主题"])


# ---------------------------------------------------------------------------
# 五、单飞锁与提取流程
# ---------------------------------------------------------------------------

class FakeChat:
    """假 LLM：可阻塞、可延迟、可指定回复内容，记录每次调用。"""

    def __init__(self, reply="NOTHING_TO_SAVE", gate: threading.Event | None = None,
                 delay: float = 0.0, fail: bool = False):
        self.reply = reply
        self.gate = gate
        self.delay = delay
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, messages, temperature=None):
        self.calls.append({"messages": messages, "temperature": temperature})
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("模拟提取调用失败")
        return {"role": "assistant", "content": self.reply}


class TestSingleFlight(unittest.TestCase):

    def setUp(self):
        self.mem_dir = make_mem_dir(self)

    def test_second_call_skipped_while_first_running(self):
        """上一次未完成：第二次调用直接返回 skipped，不排队、不并发执行。"""
        gate = threading.Event()
        chat = FakeChat(reply=json.dumps({"memories": [write_op()]}, ensure_ascii=False),
                        gate=gate)
        done = []

        def first():
            done.append(memory.extract_memories("s1", self.mem_dir, chat, ["第一次发言"]))

        t = threading.Thread(target=first)
        t.start()
        while not chat.calls:      # 等第一次真正进入 LLM 调用（锁已被持有）
            time.sleep(0.01)
        second = memory.extract_memories("s1", self.mem_dir, chat, ["第二次发言"])
        self.assertEqual(second, {"skipped": True})
        self.assertEqual(len(chat.calls), 1)  # 第二次没发 LLM 请求
        gate.set()
        t.join(timeout=5)
        self.assertEqual(done[0]["written"], ["dark-theme.md"])

    def test_runs_again_after_previous_finished(self):
        chat = FakeChat()
        memory.extract_memories("s1", self.mem_dir, chat, ["第一次"])
        result = memory.extract_memories("s1", self.mem_dir, chat, ["第二次"])
        self.assertNotIn("skipped", result)
        self.assertEqual(len(chat.calls), 2)

    def test_locks_are_per_session(self):
        chat = FakeChat()
        gate = threading.Event()
        blocker = FakeChat(gate=gate)
        t = threading.Thread(target=lambda: memory.extract_memories("sA", self.mem_dir, blocker, ["x"]))
        t.start()
        while not blocker.calls:
            time.sleep(0.01)
        # 会话 B 不受 A 的锁影响；会话 A 完成后锁被释放
        self.assertNotIn("skipped", memory.extract_memories("sB", self.mem_dir, chat, ["y"]))
        gate.set()
        t.join(timeout=5)

    def test_empty_texts_skips_llm_call(self):
        chat = FakeChat()
        result = memory.extract_memories("s1", self.mem_dir, chat, [])
        self.assertEqual(result, {"skipped": False, "written": [], "deleted": []})
        self.assertEqual(chat.calls, [])

    def test_failure_is_silent_and_never_raises(self):
        chat = FakeChat(fail=True)
        result = memory.extract_memories("s1", self.mem_dir, chat, ["正常发言"])
        self.assertEqual(result, {"error": True})  # 不抛异常即通过

    def test_unparseable_reply_abandons(self):
        chat = FakeChat(reply="我觉得没什么好记的")
        result = memory.extract_memories("s1", self.mem_dir, chat, ["正常发言"])
        self.assertIsNotNone(result.get("abandoned"))


# ---------------------------------------------------------------------------
# 六、注入拼接（memory 层 + Agent 层）
# ---------------------------------------------------------------------------

class TestSystemBlock(unittest.TestCase):

    def test_contains_contract_and_index(self):
        mem_dir = make_mem_dir(self)
        seed_memory(mem_dir, "dark-theme.md", "用户偏好深色主题")
        block = system_memory_block(mem_dir)
        self.assertIn(MEMORY_CONTRACT, block)
        self.assertIn("用户记忆索引（跨会话持久）", block)
        self.assertIn("dark-theme.md", block)
        self.assertIn("用户偏好深色主题", block)

    def test_empty_dir_degrades_and_stays_lazy(self):
        mem_dir = make_mem_dir(self)
        block = system_memory_block(mem_dir)
        self.assertIn(MEMORY_CONTRACT, block)
        self.assertIn("（暂无记忆）", block)
        self.assertFalse(mem_dir.exists(), "纯读路径不得创建目录（惰性创建）")

    def test_corrupt_index_degrades_to_empty(self):
        """索引文件损坏（被换成目录/乱码）：视为空，绝不抛异常。"""
        mem_dir = make_mem_dir(self)
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").mkdir()  # 目录：read_text 必抛 IsADirectoryError
        block = system_memory_block(mem_dir)
        self.assertIn(MEMORY_CONTRACT, block)
        self.assertIn("（暂无记忆）", block)
        (mem_dir / "MEMORY.md").rmdir()
        (mem_dir / "MEMORY.md").write_bytes(b"\xff\xfe\xff")  # 乱码：解码失败
        self.assertIn("（暂无记忆）", system_memory_block(mem_dir))

    def test_index_truncated_on_injection_only(self):
        """磁盘保留全量，注入时才截断（磁盘是真相，截断是展示策略）。"""
        mem_dir = make_mem_dir(self)
        full = "".join(render_index_line(f"m{i}.md", f"第{i}条") + "\n"
                       for i in range(INDEX_MAX_LINES + 10))
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text(full, encoding="utf-8")
        block = system_memory_block(mem_dir)
        self.assertEqual(block.count("- ["), INDEX_MAX_LINES)  # 只注入 150 条
        self.assertIn("警告", block)
        self.assertEqual((mem_dir / "MEMORY.md").read_text(encoding="utf-8"), full)


class CaptureLLM:
    """记录请求、返回固定回答的假客户端（Agent 注入测试用）。"""

    def __init__(self):
        self.calls = []

    def chat_stream(self, messages, tools=None, cancel=None):
        self.calls.append(messages)
        yield "message", {"role": "assistant", "content": "收到"}


class TestAgentInjection(unittest.TestCase):

    def make_agent(self, ws: Path) -> tuple[Agent, CaptureLLM]:
        llm = CaptureLLM()
        agent = Agent(llm=llm, verbose=False, workspace=str(ws))
        return agent, llm

    def test_system_contains_memory_and_history_never_does(self):
        """验收不变式的 Agent 级验证：契约与索引进 system；消息历史里绝无记忆段。"""
        ws = Path(tempfile.mkdtemp(prefix="memory_agent_ws_"))
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        mem_dir = memory_dir(ws)
        seed_memory(mem_dir, "dark-theme.md", "用户偏好深色主题")
        agent, llm = self.make_agent(ws)
        events = list(agent.run("随便聊聊"))
        self.assertEqual(events[-1][0], "done")
        system = llm.calls[0][0]["content"]
        self.assertIn(MEMORY_CONTRACT, system)
        self.assertIn("dark-theme.md", system)
        for m in agent.history:  # 压缩只改历史视图，历史里混进记忆段就会被摘要吞掉
            self.assertNotIn(MEMORY_CONTRACT, str(m.get("content")))
        # agent 的基础 system_prompt 原样保留在开头
        self.assertTrue(system.startswith(agent.system_prompt))

    def test_fresh_read_each_run(self):
        """索引每次组装从磁盘现读：回合间刚写入的记忆，下一轮立即可见。"""
        ws = Path(tempfile.mkdtemp(prefix="memory_agent_ws2_"))
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent, llm = self.make_agent(ws)
        list(agent.run("第一轮"))
        first_system = llm.calls[0][0]["content"]
        self.assertIn("（暂无记忆）", first_system)
        seed_memory(memory_dir(ws), "new-fact.md", "第二轮前刚写入的记忆")
        list(agent.run("第二轮"))
        second_system = llm.calls[1][0]["content"]
        self.assertNotIn("（暂无记忆）", second_system)
        self.assertIn("new-fact.md", second_system)


if __name__ == "__main__":
    unittest.main(verbosity=2)
