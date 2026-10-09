"""首轮问答 → 会话标题（后台线程）
================================

原 app.py 顶层的标题生成逻辑，逐字搬移。依赖装配层的会话总线（_event_bus）
与统一日志（log）——为避免与 app.py 形成循环 import，装配完成后由
app.install_service_hooks() 注入。

注意：注入的 `_event_bus` / `log` 是**模块级名字**，通过 .bind() 绑定后成为模块
属性，发布事件时取到的是注入后的对象（不要在 import 期把它们当默认值固化）。
"""

import re
import threading

import db

# 由 app.install_service_hooks() 注入：会话总线工厂与统一日志。
_event_bus = None
log = None


def _clean_title(raw: str) -> str:
    """把模型输出洗成一行可用标题；不合格返回 ""（调用方保留占位标题）。"""
    line = (raw or "").strip().splitlines()
    text = next((ln.strip() for ln in line if ln.strip()), "")
    # 去掉模型常见的包装：引号、书名号、Markdown 记号、"标题："前缀
    text = text.strip("「」『』\"'“”《》\\*# ")
    text = re.sub(r"^(标题|任务名|会话名)[:：]\s*", "", text).strip()
    if not text or len(text) > 24:
        return ""
    return text


TITLE_PROMPT = (
    "你在为一个 AI 编程助手生成会话标题。根据下面这段对话，用中文给这个任务起一个"
    "标题：4~14 个字，动宾或名词短语，概括【用户到底想做什么】，不要复述原句、"
    "不要引号、不要标点结尾、不要任何解释，只输出标题本身。\n\n"
    "用户：{user}\n\n"
    "助手：{answer}\n"
)


def _generate_session_title(sid: str, chat, plain: str, answer: str) -> None:
    """在后台线程里按首轮问答生成标题；任何失败都静默保留占位标题。

    三道闸门都必须在【真正写库前】现查一次（而不是启动线程时查完就算）：
    * title_manual=1 —— 用户在等待总结期间亲手改了名，绝不能覆盖；
    * title 已是总结结果 —— 同一会话只总结一次（重放/重试不重复烧请求）；
    * 线程优先级之外的失败（模型报错、输出非法）→ 直接放弃，保留 placeholder。
    """
    try:
        prompt = TITLE_PROMPT.format(user=plain[:1500], answer=(answer or "（无回答）")[:1500])
        msg = chat([{"role": "user", "content": prompt}], temperature=0)
        title = _clean_title(msg.get("content") if isinstance(msg, dict) else "")
        if not title:
            return
        if db.session_title_manual(sid):
            return  # 用户手动命名过：这个名字归用户，AI 不碰
        if db.session_title_contains(sid, title):
            return  # 已是这个名字（重复触发/重放）：不重复写、不重复广播
        db.set_session_title(sid, title)
        # 广播让所有开着这个会话的页面就地更新左侧列表那一行；前端失败与否
        # 都不影响库里的标题（下次拉列表自然一致）。
        _event_bus(sid).publish({"type": "session_title", "session_id": sid, "title": title})
    except Exception:
        log.exception("[会话 %s] 标题总结失败（忽略，保留原占位标题）", sid)


def _spawn_title_generation(sid: str, chat, plain: str, answer: str) -> None:
    """首轮结束后的标题总结启动点（与 _spawn_memory_extraction 同址同纪律）。

    传的是 chat 方法而非 agent 实例：线程绝不共享 agent.history（下一回合马上
    会改它）。启动失败只进日志，绝不影响回合收尾。
    """
    try:
        threading.Thread(target=_generate_session_title, daemon=True,
                         args=(sid, chat, plain, answer),
                         name=f"title-gen-{sid}").start()
    except Exception:
        log.exception("[会话 %s] 标题总结线程启动失败（忽略）", sid)
