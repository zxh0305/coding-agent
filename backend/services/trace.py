"""执行过程轨迹：裁剪、落库、进行中快照
====================================

原 app.py 顶层的轨迹三件事，逐字搬移：
* _persist_trace      —— 收尾时把一轮轨迹裁剪后落库（答案消息 mid 为键）；
* SNAPSHOT_* 常量    —— 进行中快照的节流间隔与裁剪上限；
* _snapshot_trace    —— 从 agent 现场拼"进行中"轨迹（含未固化的思考流）。

依赖 agent.Agent 仅用于类型标注。无状态注入需求（event_bus 的发布发生在
turn.py 里，本模块只管落库与拼装）。
"""

import json

from agent import Agent

import db


def _persist_trace(sid: str, mid: str, trace: list) -> None:
    """把一轮提问的轨迹裁剪后落库。轨迹里的工具结果原样来自执行器（可能几 MB），
    回放场景用不到全文（那在归档/历史消息里），逐条截断控制体积；
    总条数也设上限——失控回合的轨迹不该撑爆库。"""
    entries = []
    for e in trace[:150]:
        if not isinstance(e, dict):
            continue
        e = dict(e)
        r = e.get("result")
        if isinstance(r, str) and len(r) > 1200:
            e["result"] = r[:1200] + f"…[截断，完整输出见执行记录，共 {len(r)} 字符]"
        a = e.get("arguments")
        if isinstance(a, str) and len(a) > 2000:
            e["arguments"] = a[:2000] + "…[截断]"
        t = e.get("text")  # reasoning 条目的思考文本：可很长，回放不必全文
        if isinstance(t, str) and len(t) > 4000:
            e["text"] = t[:4000] + f"…[思考截断，共 {len(t)} 字符]"
        entries.append(e)
    db.set_trace(sid, mid, json.dumps(entries, ensure_ascii=False))


# 进行中回合快照的节流间隔（秒）：太密会高频写库，太疏切回来缺一大段。
SNAPSHOT_INTERVAL = 2.0

# 快照条目上限：直接复用落库轨迹同款裁剪（_persist_trace 的裁剪规则），保证
# 「进行中快照」与「最终轨迹」的数据形状/体量一致，回放渲染零特判。
SNAPSHOT_MAX_ENTRIES = 150

# 单条目文本上限（与 _persist_trace 内的 4000 截断同源）。
SNAPSHOT_MAX_TEXT = 4000


def _snapshot_trace(agent: Agent) -> list:
    """从 agent 现场拼「进行中」轨迹快照：已固化条目 + 当前轮的思考累积。

    agent.trace 里的 reasoning 条目要等每轮流结束才 append；进行中的思考文本
    存在 agent.reasoning_live（round_no → 已吐出的文本）。快照按 round_no 顺序
    把 live 部分接在对应轮次标题之后——与收尾后的最终 trace 同构。
    """
    entries = list(agent.trace)
    if agent.reasoning_live:
        merged = []
        seen_rounds = set()
        for e in entries:
            merged.append(e)
            if e.get("type") == "round":
                r = e.get("round")
                seen_rounds.add(r)
                live = agent.reasoning_live.get(r)
                if live:
                    merged.append({"type": "reasoning", "round": r,
                                   "text": live, "live": True})
        # live 缓冲里有而 trace 里还没有对应轮次标题的（极小的窗口期）：补在末尾
        for r, live in agent.reasoning_live.items():
            if r not in seen_rounds and live:
                merged.append({"type": "reasoning", "round": r, "text": live, "live": True})
        entries = merged
    # 裁剪规则与 _persist_trace 一致：条目数 150、单条文本 4000
    out = []
    for e in entries[:SNAPSHOT_MAX_ENTRIES]:
        e = dict(e)
        t = e.get("text")
        if isinstance(t, str) and len(t) > SNAPSHOT_MAX_TEXT:
            e["text"] = t[:SNAPSHOT_MAX_TEXT] + f"…[思考截断，共 {len(t)} 字符]"
        out.append(e)
    return out
