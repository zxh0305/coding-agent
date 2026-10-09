"""浏览器工具截图 → SSE 推送
==========================

browser_tools.screenshot_pusher 的注入实现：PNG 落盘到会话浏览器目录，并发
SSE 事件让前端右侧「浏览器」弹窗实时显示。逐字搬移。

依赖装配层的会话总线（_event_bus）与统一日志（log），由
app.install_service_hooks() 注入。

说明：这里保留原样的 `Path("data/browser-shots")` 相对路径（纯搬移，不改
行为）；路径口径统一是独立的后续改动项，不夹带在本次结构重排里。
"""

from pathlib import Path

# 由 app.install_service_hooks() 注入。
_event_bus = None
log = None


def _push_browser_screenshot(sid: str, png: bytes, url: str, note: str) -> None:
    """浏览器工具的截图推送（browser_tools.screenshot_pusher 注入点）：
    PNG 落盘到会话浏览器目录 + 发 SSE 事件（前端右侧「浏览器」弹窗实时显示）。
    调用发生在工具线程里，publish 有锁，安全；落盘失败不影响工具调用。"""
    try:
        out = Path("data/browser-shots") / sid
        out.mkdir(parents=True, exist_ok=True)
        n = len(list(out.glob("shot-*.png"))) + 1
        rel = out / f"shot-{n:04d}.png"
        rel.write_bytes(png)
        _event_bus(sid).publish({"type": "browser_shot",
                                 "url": url, "note": note,
                                 "shot": f"/api/sessions/{sid}/browser/shot?n={n}"})
    except Exception:
        log.exception("[会话 %s] 浏览器截图推送失败（忽略）", sid)
