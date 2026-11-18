"""崩溃尾语义配平：让模型知道「那次工具到底跑没跑」。

背景
----
进程在派发工具之后挂掉（或被杀、断电），库里会留下这样的尾巴：

    assistant: {tool_calls: [call_1, call_2]}      ← 有请求
    tool:      <call_1 的结果>                      ← 只结算了一个
                                                  ← call_2 没有结果

这种"有请求、无结果"的历史，下一轮请求服务端直接 400。现状是靠人记住这条
约束（agent.py 的多处注释都在强调），而恢复路径（services/session_boot.py）
事实上**没有任何配平**，等于把问题留给了下一条请求。

更重要的是：即使补一条空结果把 400 糊过去，模型仍然不知道发生了什么，可能
做出两种错误决定——把"可能已经执行了的写操作"再执行一遍（重复副作用），或者
以为工具还没开始而白等。所以补的结果必须**带语义**，分两档：

    TOOL_NOT_STARTED      确定没开始：进程在派发之前就没了。可以安全重试。
    TOOL_OUTCOME_UNKNOWN  不确定：可能已经开始执行了。不要盲目重跑有副作用的
                          操作，先检查现场（读文件 / 查 git status / 看日志）。

怎么判断是哪一档
----------------
恢复时没有运行时上下文，只能靠持久化痕迹推断：
  · 若库里有该调用的「进行中」记录 → UNKNOWN
  · 否则 → UNKNOWN（保守默认）

为什么默认保守：把"其实没开始"误判成 UNKNOWN，代价只是模型多花一步去确认；
把"其实已经改了文件"误判成 NOT_STARTED，代价是重复副作用、覆盖用户改动。
两者代价不对称，所以默认选 UNKNOWN。
"""

from __future__ import annotations

TOOL_NOT_STARTED = "tool_not_started"
TOOL_OUTCOME_UNKNOWN = "tool_outcome_unknown"

_TEMPLATES = {
    TOOL_NOT_STARTED: (
        "[未执行] 上一次运行在本工具开始前中断，{name} 没有执行，也没有产生任何副作用。"
        "如需继续，可以安全地重新调用。"
    ),
    TOOL_OUTCOME_UNKNOWN: (
        "[结果未知] 上一次运行在本工具执行期间中断，{name} 是否已经执行、执行到什么程度"
        "都不确定。不要盲目重跑：先检查现场（例如读相关文件、查看 git status 或日志），"
        "确认后再决定重试、补做还是跳过。特别注意：重复执行写文件/提交/发请求这类"
        "有副作用的操作可能造成数据损坏。"
    ),
}


def synthetic_result(name: str, kind: str = TOOL_OUTCOME_UNKNOWN) -> str:
    """生成给模型看的合成工具结果文本。"""
    template = _TEMPLATES.get(kind, _TEMPLATES[TOOL_OUTCOME_UNKNOWN])
    return template.format(name=name or "该工具")


def balance_tail(history: list[dict], *, default_kind: str = TOOL_OUTCOME_UNKNOWN) -> int:
    """为历史尾部未结算的工具调用补合成结果，返回补了几条。

    只处理「最后一个 assistant 消息带 tool_calls，且其后缺少对应 tool 结果」的情况：
      · 崩溃/被杀 → 尾巴必然落在会话末尾，处理尾部即可；
      · 中间轮次缺结果只可能来自历史损坏，那种情况交给更上游的一致性检查，
        这里不做猜测性修补（猜错会改坏正常历史）。

    幂等：已有结果的调用不会重复补，可以安全地在每次恢复时调用。
    """
    if not history:
        return 0

    # 从后往前找最后一条带 tool_calls 的 assistant
    idx = None
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            idx = i
            break
    if idx is None:
        return 0

    calls = history[idx].get("tool_calls") or []
    call_ids = [c.get("id") for c in calls if c.get("id")]
    if not call_ids:
        return 0

    # 收集该消息之后已有的工具结果（可能只有一部分）
    settled = set()
    for msg in history[idx + 1:]:
        if msg.get("role") in ("tool",) and msg.get("tool_call_id"):
            settled.add(msg["tool_call_id"])
        elif msg.get("role") == "user":
            # 已经进入下一轮用户输入：说明这一轮是被正常推进过的，
            # 残留的缺失不属于"崩溃尾"，不在这里修补
            return 0

    missing = [c for c in call_ids if c not in settled]
    if not missing:
        return 0

    added = 0
    for call in calls:
        cid = call.get("id")
        if not cid or cid in settled:
            continue
        name = (call.get("function") or {}).get("name") or call.get("name") or ""
        history.append({
            "role": "tool",
            "tool_call_id": cid,
            "content": synthetic_result(name, default_kind),
            # 标记为合成结果：前端/对账逻辑可据此区分"真实结果"与"补的"
            "_synthetic": True,
            "_crash_balanced": True,
        })
        added += 1
    return added
