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

    压缩包不会返回乱码：会返回成员清单，提示用 extract_attachment 解压。
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

    # 归档：返回成员清单 + 下一步指引（不解压、不吐乱码）
    if info.get("kind") == "archive":
        return _ok(_archive_read_result(info, sid))
    # 二进制（非归档）：明确告知不能按文本读
    if info.get("kind") == "binary":
        return _ok({**info, "result":
                    f"《{info['name']}》是二进制文件，无法按文本读取。\n"
                    f"若这是压缩包，请确认扩展名；也可用 list_attachments 核对。"})

    head = (f"《{info['name']}》第 {info['offset'] + 1}~{info['offset'] + info['lines']} 行"
            f"（共 {info['total_lines']} 行）")
    tail = ("\n…[还有后续内容，用更大的 offset 继续读取]"
            if info["truncated"] else "")
    return _ok({**info, "result": f"{head}\n\n{info['content']}{tail}"})


def _archive_read_result(info: dict, sid: str) -> dict:
    """压缩包读取结果：成员清单 + 解压指引（含桥接路径，若有）。"""
    lines = [f"《{info['name']}》是压缩包（{info.get('archive_kind') or '未知格式'}），"
             f"含 {info['file_count']} 个成员："]
    for m in info["members"][:30]:
        note = m.get("note") or (f"{m['bytes']} 字节" if m.get("bytes") is not None else "")
        lines.append(f"  - {m['name']}  {note}")
    if info["file_count"] > 30:
        lines.append(f"  …还有 {info['file_count'] - 30} 个成员")
    lines.append("")
    lines.append("下一步：用 extract_attachment 解压后，再用 read_file / run_bash 读取"
                 "解压出的文件（解压目录会出现在工作区 .coding-agent/attachments/ 下）。")
    return {**info, "result": "\n".join(lines)}


def extract_attachment(name: str, ctx=None) -> str:
    """解压当前会话中的一个压缩包附件，返回解压目录与文件清单。

    解压后的文件落在工作区 .coding-agent/attachments/_extracted/<附件名>/ 下
    （附件本身也在工作区内），之后可用 read_file 读取、用 grep/run_bash 分析。
    带输出总量与成员数上限，防压缩炸弹。
    """
    sid = getattr(ctx, "session_id", None) if ctx is not None else None
    if not sid:
        return _err("当前会话未知，无法确定附件归属（系统内部问题）",
                    "请重试；若持续失败请联系服务部署者")
    try:
        result = db.extract_attachment(sid, name)
    except ValueError as e:
        return _err(f"附件解压被拒：{e}", "文件名应为纯文件名；可先用 list_attachments 查看")
    except FileNotFoundError:
        return _err(f"附件不存在：{name}", "先用 list_attachments 查看本会话有哪些附件，核对文件名")
    except OSError as e:
        return _err(f"附件解压失败：{e}", "稍后重试")

    if not result.get("ok"):
        return _err(f"解压失败：{result.get('error')}",
                    "若这不是压缩包，可直接用 read_attachment 读取（文本文件）")

    files = [m for m in result["members"] if "skipped" not in m]
    skipped = [m for m in result["members"] if "skipped" in m]
    lines = [f"已解压《{name}》（{result.get('kind')}），共 {len(files)} 个文件，"
             f"{result.get('total_bytes', 0)} 字节。", ""]
    for m in files[:40]:
        lines.append(f"  - {m['name']}  ({m.get('bytes', 0)} 字节)")
    if len(files) > 40:
        lines.append(f"  …还有 {len(files) - 40} 个文件")
    if skipped:
        lines.append("")
        lines.append(f"（{len(skipped)} 个成员被跳过：{skipped[0].get('skipped')}）")
    # 解压目录在工作区内：给 agent 可直接使用的相对路径
    rel = result.get("rel_dir")
    if rel:
        dir_rel = f"{db.ATTACH_DIR_NAME}/{rel}"
        lines.append("")
        lines.append(f"解压目录（工作区内）：{dir_rel}")
        if files:
            lines.append(f"例：read_file {{\"path\": \"{dir_rel}/{files[0]['name']}\"}}")
    return _ok({**result, "result": "\n".join(lines)})


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
    {
        "type": "function",
        "function": {
            "name": "extract_attachment",
            "description": "解压当前会话中的一个压缩包附件（.tar.gz/.zip/.gz 等），"
                           "返回解压目录与文件清单。什么时候用：read_attachment 提示"
                           "某个附件是压缩包时（运维日志包很常见）。解压后文件出现在"
                           "工作区 .coding-agent/attachments/ 下，可用 read_file 读取、"
                           "用 grep/run_bash 分析。带输出总量与成员数上限，防压缩炸弹。"
                           "示例：{\\\"name\\\": \\\"logs.tar.gz\\\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "压缩包附件的文件名（纯文件名，不含路径）"},
                },
                "required": ["name"],
            },
        },
    },
]

ATTACH_TOOL_REGISTRY = {
    "list_attachments": list_attachments,
    "read_attachment": read_attachment,
    "extract_attachment": extract_attachment,
}

# list/read 只读：只打开附件文件读，不改任何状态，可与其它只读工具并行。
# extract 会写磁盘（解压产物），不算只读——不进并行只读集合，走串行执行。
ATTACH_TOOL_READ_ONLY = {
    "list_attachments": True,
    "read_attachment": True,
    "extract_attachment": False,
}
