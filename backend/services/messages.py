"""用户消息组装与请求体上限
=========================

把 {message, attachments} 组装成 OpenAI 格式的用户消息；图片内联为 image_url、
文本文件落盘到会话附件区并只引用文件名。逐字搬移。
"""

import base64

import db

MAX_IMAGE_B64 = 6_000_000   # 单张图片 base64 长度上限（约 4.5MB 原图）
MAX_BODY_BYTES = 12_000_000  # 单请求体上限：覆盖"消息 + 若干内联附件"，防超大 body 撑内存


def _build_user_message(body: dict, sid: str) -> tuple[str, dict | None]:
    """把 {message, attachments} 组装成 OpenAI 格式的用户消息。

    图片 → image_url 视觉输入（需要所用模型支持视觉）；
    文本文件 → 落盘到会话附件区（data/attachments/<sid>/），消息里只注入
    文件名与大小，模型用 read_attachment 工具按需分页读取。
    返回 (纯文本预览, 完整消息)；预览用于任务标题。

    为什么文本附件不再注入正文：早期做法把全文解码后塞进消息，300KB 中文
    就约 10 万 token，且随会话历史每轮重复携带，上限只能卡死在 300KB。改为
    引用式后，上下文成本从"全文"降到"几十 token"，单文件上限放宽到 5MB，
    模型还能通过分页覆盖全文。sid 用于确定附件归属，不可为空。
    """
    text = (body.get("message") or "").strip()
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    names, file_notes = [], []
    for att in (body.get("attachments") or [])[:6]:
        name = str(att.get("name") or "file").replace("\n", " ")[:80]
        data = str(att.get("data") or "")
        if att.get("kind") == "image":
            if not data:
                continue
            if len(data) > MAX_IMAGE_B64:
                raise ValueError(f"图片 {name} 超过 4MB 限制")
            mime = att.get("mime") if str(att.get("mime", "")).startswith("image/") else "image/png"
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
            names.append(name)
        else:
            try:
                blob = base64.b64decode(data)
            except Exception:
                continue
            if not blob:
                continue
            info = db.save_attachment(sid, name, blob)  # 落盘；超限抛 ValueError
            file_notes.append(
                f"### 附件文件：{info['name']}（已存入附件区，共 {info['bytes']} 字节）\n"
                f"请用 read_attachment 工具读取内容（支持 offset/limit 分页），"
                f"不要假设已经看到全文。")
            names.append(info["name"])
    if file_notes:
        parts.append({"type": "text", "text": "用户附带了以下文件：\n\n" + "\n\n".join(file_notes)})
    if not parts:
        return "", None
    plain = text or ("[附件] " + "、".join(names))
    if len(parts) == 1 and parts[0]["type"] == "text":
        return plain, {"role": "user", "content": parts[0]["text"]}
    return plain, {"role": "user", "content": parts}
