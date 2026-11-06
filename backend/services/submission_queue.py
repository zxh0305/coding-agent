"""Durable queue operations share the submission transaction and receipt identity."""
import json
import db


class QueueConflict(ValueError):
    pass


def entries(sid):
    with db._conn() as conn:
        legacy = conn.execute("SELECT user_id,nonce,revision,payload FROM submission_queue WHERE session_id=? AND state='queued' AND preview_json IS NULL", (sid,)).fetchall()
        for row in legacy:
            conn.execute("UPDATE submission_queue SET preview_json=? WHERE user_id=? AND nonce=? AND revision=?",
                         (db.submission_preview(json.loads(row["payload"])),row["user_id"],row["nonce"],row["revision"]))
        rows = conn.execute("SELECT q.user_id,q.nonce,q.session_id,q.mid,q.position,q.revision,q.state,q.reason,q.preview_json,r.workspace FROM submission_queue q JOIN submission_receipts r "
                            "USING(user_id,nonce) WHERE q.session_id=? AND q.state IN ('queued','interrupted') ORDER BY q.position,q.rowid", (sid,)).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        value.update(json.loads(value.pop("preview_json")))
        result.append(value)
    return result


def detail(sid, mid):
    with db._conn() as conn:
        row = conn.execute("SELECT payload,revision FROM submission_queue WHERE session_id=? AND mid=? AND state IN ('queued','interrupted')", (sid,mid)).fetchone()
    if not row:
        raise QueueConflict("任务已开始或已移除")
    payload = json.loads(row["payload"])
    text = payload[1].get("_input_text",payload[0])
    if len(text.encode()) > 64000:
        raise ValueError("正文超过64KB，请移除后重新发送")
    return {"text":text,"revision":row["revision"]}


def next_entry(sid):
    with db._conn() as conn:
        row = conn.execute("SELECT q.*,r.workspace FROM submission_queue q JOIN submission_receipts r USING(user_id,nonce) "
                           "WHERE q.session_id=? AND q.state='queued' ORDER BY q.position,q.rowid LIMIT 1", (sid,)).fetchone()
    return dict(row) if row else None


def take_next(sid, workspace, worker):
    """Called only after the worker owns the session execution lock."""
    import time
    with db._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT q.*,r.workspace,s.archived,s.workspace AS current_workspace FROM submission_queue q "
                           "JOIN submission_receipts r USING(user_id,nonce) JOIN sessions s ON s.id=q.session_id "
                           "WHERE q.session_id=? AND q.state='queued' ORDER BY q.position,q.rowid LIMIT 1", (sid,)).fetchone()
        if not row:
            return None
        if row["reason"]:
            return None
        if row["workspace"] != workspace or row["archived"] or (row["current_workspace"] and row["current_workspace"] != workspace):
            reason = "任务已归档，请恢复后继续" if row["archived"] else "项目已变更，请切回原项目后继续"
            conn.execute("UPDATE submission_queue SET reason=?,revision=revision+1 WHERE user_id=? AND nonce=? AND reason!=?",
                         (reason,row["user_id"],row["nonce"],reason))
            return None
        value = dict(row)
        changed = conn.execute("UPDATE submission_receipts SET worker=?,payload=NULL WHERE user_id=? AND nonce=? AND worker IS NULL",
                               (worker,row["user_id"],row["nonce"])).rowcount
        if not changed:
            raise QueueConflict("提交已被其他执行器认领")
        preview = row["preview_json"] or db.submission_preview(json.loads(row["payload"]))
        conn.execute("UPDATE submission_queue SET state='claimed',preview_json=?,revision=revision+1 WHERE user_id=? AND nonce=?", (preview,row["user_id"],row["nonce"]))
        conn.execute("INSERT INTO session_turns(session_id,user_mid,state,started) VALUES(?,?,'running',?)",
                     (sid,row["mid"],time.time()))
        return value


def change(sid, uid, mid, revision, action, text=None):
    if type(revision) is not int or action not in ("edit", "cancel", "up", "down", "resume"):
        raise ValueError("无效的队列操作或版本")
    with db._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM submission_queue WHERE session_id=? AND user_id=? AND mid=?",
                           (sid, uid, mid)).fetchone()
        if not row or row["revision"] != revision or (row["state"] != "queued" and not (row["state"] == "interrupted" and action == "cancel")):
            raise QueueConflict("队列已变化或任务已开始，请刷新后重试")
        if action == "cancel":
            conn.execute("UPDATE submission_queue SET state='cancelled',payload=NULL,revision=revision+1 WHERE user_id=? AND nonce=?", (uid,row["nonce"]))
            # Retain the immutable receipt and digest so a lost old reply never resurrects a cancelled task.
            conn.execute("UPDATE submission_receipts SET payload=NULL,worker='cancelled' WHERE user_id=? AND nonce=?", (uid,row["nonce"]))
        elif action == "edit":
            if not isinstance(text, str) or len(text.encode()) > 64000:
                raise ValueError("正文须为文本，最多64KB")
            text = text.strip()
            payload = json.loads(row["payload"])
            message = payload[1]; content = message["content"]
            previous = message.get("_input_text", payload[0])
            if isinstance(content, str):
                content = [{"type":"text","text":content}]
            # Only replace the user's input part, preserving even a sole text-attachment reference.
            if previous and content and content[0].get("type") == "text" and content[0].get("text") == previous:
                content = content[1:]
            if not text and not content:
                raise ValueError("需求正文不能为空")
            parts = ([{"type":"text","text":text}] if text else []) + content
            message["content"] = parts[0]["text"] if len(parts)==1 and parts[0].get("type")=="text" else parts
            message["_input_text"] = text
            payload[0] = text or "[附件] " + "、".join(a.get("name", "") for a in payload[2])
            conn.execute("UPDATE submission_queue SET payload=?,preview_json=?,revision=revision+1 WHERE user_id=? AND nonce=?",
                         (json.dumps(payload),db.submission_preview(payload),uid,row["nonce"]))
        elif action in ("up", "down"):
            ordered = conn.execute("SELECT nonce,user_id FROM submission_queue WHERE session_id=? AND state='queued' ORDER BY position,rowid", (sid,)).fetchall()
            keys = [(r["user_id"],r["nonce"]) for r in ordered]
            index = keys.index((uid,row["nonce"])); other = index + (-1 if action == "up" else 1)
            if 0 <= other < len(keys):
                keys[index],keys[other] = keys[other],keys[index]
                for position, key in enumerate(keys, 1):
                    conn.execute("UPDATE submission_queue SET position=?,revision=revision+1 WHERE user_id=? AND nonce=?", (position*1024,*key))
        else:
            conn.execute("UPDATE submission_queue SET reason='',revision=revision+1 WHERE user_id=? AND nonce=?", (uid,row["nonce"]))


def pause(uid, nonce, reason):
    with db._conn() as conn:
        conn.execute("UPDATE submission_queue SET reason=?,revision=revision+1 WHERE user_id=? AND nonce=? AND state='queued' AND reason!=?",
                     (reason,uid,nonce,reason))


def finish(uid, nonce):
    with db._conn() as conn:
        conn.execute("UPDATE submission_queue SET state='finished',payload=NULL,revision=revision+1 WHERE user_id=? AND nonce=? AND state='claimed'", (uid,nonce))
