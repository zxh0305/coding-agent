"""
附件工具（Attachment tools）
============================

让 agent 能读取用户上传的附件内容。与 code_tools 分开是因为职责不同：
code_tools 操作的是【用户工作区文件】（用户的项目代码，受工作区边界约束），
本模块操作的是【会话附件】（用户上传给这次对话的文件，按会话隔离存在
data/attachments/<sid>/，不进用户工作区）。

为什么要有这个工具：附件改成"引用式"存储后，消息里只带文件名与大小，
正文不在上下文里——模型必须主动调用本工具去读，才能看到文件内容。
这也正是它能支持 5MB 的原因：读多少、读哪段由模型决定，上下文只装它
真正需要的那部分。

与工具层的约定一致：
  * 工具层不持有全局状态——session_id 通过 ToolContext 注入（见 tools.py）；
  * 失败走统一信封 {ok:false, error, hint?}，让模型能读原因改道；
  * ctx 形参不进 schema（模型看不到），由 execute_tool 在执行时注入。
"""

import json

import db


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str, hint: str = "") -> str:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


def list_attachments(ctx=None) -> str:
    """列出当前会话的全部附件（名字 / 大小 / 上传时间）。"""
    sid = getattr(ctx, "session_id", None) if ctx is not None else None
    if not sid:
        return _err("当前会话未知，无法确定附件归属（系统内部问题）",
                    "请重试；若持续失败请联系服务部署者")
    items = db.list_attachments(sid)
    if not items:
        return _ok({"attachments": [], "result": "当前会话没有附件。"})
    lines = [f"- {it['name']}（{it['bytes']} 字节）" for it in items]
    return _ok({"attachments": items,
                "result": "当前会话的附件：\n" + "\n".join(lines)})


def read_attachment(name: str, offset: int = 0, limit: int = 2000, ctx=None) -> str:
    """读取当前会话中一份附件的内容，按行分页。

    name 是附件文件名（见消息里的附件清单或 list_attachments）；offset 是
    起始行号（0 起），limit 是最多读取的行数。文件很长时返回 truncated=true，
    模型可继续用更大的 offset 读下一段——不要一次拉取整个大文件。
    """
    sid = getattr(ctx, "session_id", None) if ctx is not None else None
    if not sid:
        return _err("当前会话未知，无法确定附件归属（系统内部问题）",
                    "请重试；若持续失败请联系服务部署者")
    try:
        info = db.read_attachment_text(sid, name, offset=offset, limit=limit)
    except ValueError as e:
        return _err(f"附件读取被拒：{e}", "文件名应为纯文件名，不含路径分隔符；可先用 list_attachments 查看")
    except FileNotFoundError:
        return _err(f"附件不存在：{name}", "先用 list_attachments 查看本会话有哪些附件，核对文件名")
    except OSError as e:
        return _err(f"附件读取失败：{e}", "稍后重试")
    head = (f"《{info['name']}》第 {info['offset'] + 1}~{info['offset'] + info['lines']} 行"
            f"（共 {info['total_lines']} 行）")
    tail = ("\n…[还有后续内容，用更大的 offset 继续读取]"
            if info["truncated"] else "")
    return _ok({**info, "result": f"{head}\n\n{info['content']}{tail}"})


ATTACH_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_attachments",
            "description": "列出当前会话中用户上传的全部附件（文件名 / 大小）。"
                           "什么时候用：不确定有哪些附件、或读取失败要核对文件名时。"
                           "无参数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_attachment",
            "description": "读取当前会话中一份附件的内容（按行分页）。"
                           "什么时候用：用户上传了文件、消息里提示「已存入附件区」时，"
                           "必须调用本工具才能看到文件内容——附件正文不在你的上下文里。"
                           "大文件请分页读：先读前 2000 行，需要时再用更大的 offset 继续，"
                           "不要试图一次拉完。"
                           "示例：{\\\"name\\\": \\\"build.log\\\", \\\"offset\\\": 0, \\\"limit\\\": 2000}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "附件文件名（纯文件名，不含路径），见消息里的附件清单"},
                    "offset": {"type": "integer",
                               "description": "起始行号（0 起），默认 0"},
                    "limit": {"type": "integer",
                              "description": "最多读取的行数，默认 2000"},
                },
                "required": ["name"],
            },
        },
    },
]

ATTACH_TOOL_REGISTRY = {
    "list_attachments": list_attachments,
    "read_attachment": read_attachment,
}

# 两个工具都是只读：只打开附件文件读，不改任何状态，可与其它只读工具并行。
ATTACH_TOOL_READ_ONLY = {
    "list_attachments": True,
    "read_attachment": True,
}
