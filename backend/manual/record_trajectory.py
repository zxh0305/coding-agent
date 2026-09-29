"""
从真实会话导出回放剧本草稿（manual/record_trajectory.py）
==========================================================

用法：
    cd backend
    python3 manual/record_trajectory.py <session_id> [输出文件.json]

读取 data/agent_data.db 里某会话的消息历史，把【第一轮任务】（首条 user
消息 → 其后的工具循环 → 最终回答）展开成回放剧本草稿，格式与
tests/fixtures/*.json 一致。

展开规则（与 agent 循环的请求节奏一一对应）：
- 首条 user 消息 = 剧本的 user 字段；其后每条 assistant 消息就是逐次的
  LLM 响应（工具循环每轮一个）；
- role=tool / _synthetic / role=compact 的消息不进剧本：tool 结果回放时由
  agent 真实重跑产生，后两者不参与请求节奏；error 消息是收尾追加的，跳过；
- tool_calls 的 arguments 解析不出合法 JSON 时原样放进 arguments_raw——
  真实发生过的格式漂移正是回放要覆盖的路径；
- 会话有多轮用户消息时只导出第一轮（多轮剧本需要 fixtures 格式扩展支持，
  真需要时人工拼接），截断处会在 _note 里说明。

导出的是【草稿】：workspace_files（开场文件快照）与 expect 的 file_contains
等断言必须人工补全——工作区当时的样子库里没有，得由人按任务背景重建。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 退两级到 backend/

import db  # noqa: E402


def record(session_id: str) -> dict:
    msgs = db.get_messages(session_id)

    # 找首条 user 消息；其后到第二条 user（若有）之间是第一轮任务的全部消息
    starts = [i for i, m in enumerate(msgs)
              if m.get("role") == "user" and not m.get("_synthetic")]
    if not starts:
        raise SystemExit(f"会话 {session_id} 里没有用户消息，无从导出")
    first, second = starts[0], (starts[1] if len(starts) > 1 else None)
    user_msg = msgs[first]
    body = msgs[first + 1: second] if second else msgs[first + 1:]

    user_text = user_msg.get("content")
    if not isinstance(user_text, str):  # 多部分（带图）消息：文本部分拼出来
        user_text = " ".join(p.get("text") or "" for p in user_text or []
                             if isinstance(p, dict) and p.get("type") == "text")

    turns, pending_calls = [], None

    def flush_calls():
        nonlocal pending_calls
        if pending_calls:
            turns.append({"tool_calls": pending_calls})
            pending_calls = None

    for m in body:
        if m.get("_synthetic") or m.get("_artifact"):
            continue
        if m.get("role") == "assistant" and m.get("error"):
            continue
        if m.get("role") == "assistant":
            content = m.get("content")
            calls = []
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    calls.append({"name": fn.get("name"), "arguments": json.loads(raw)})
                except json.JSONDecodeError:
                    calls.append({"name": fn.get("name"), "arguments_raw": raw})
            if calls:
                flush_calls()
                pending_calls = calls
            elif content:
                flush_calls()
                turns.append({"content": content})
    flush_calls()

    # 止于 tool_calls 的残轮（会话没收到最终回答）对回放无意义：丢弃
    notes = []
    if second:
        notes.append(f"会话共 {len(starts)} 轮用户消息，本剧本只含第一轮。")
    if turns and "tool_calls" in turns[-1]:
        turns.pop()
        notes.append("会话止于工具调用（无最终回答），残轮已丢弃。")

    return {
        "name": f"session_{session_id[:8]}",
        "description": "（草稿）由 record_trajectory.py 导出；补全 description/"
                       "workspace_files/expect 后放入 tests/fixtures/。",
        "workspace_files": {},
        "user": user_text or "[多部分消息，人工补全]",
        "turns": turns,
        "expect": {
            "tool_sequence": [c["name"] for t in turns if "tool_calls" in t
                              for c in t["tool_calls"]],
        },
        "_note": " ".join(notes) or "单轮任务，已完整导出。",
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python3 manual/record_trajectory.py <session_id> [输出.json]")
    script = record(sys.argv[1])
    out = json.dumps(script, ensure_ascii=False, indent=2)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(out + "\n", encoding="utf-8")
        print(f"已写入 {sys.argv[2]}")
    else:
        print(out)
