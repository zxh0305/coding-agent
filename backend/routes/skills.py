"""Authenticated personal skill CRUD + per-message lazy fetch.

GET  /api/skills                          列出当前用户的全部技能
GET  /api/skills?mid=&session_id=         懒取一条消息上记录的技能全文
POST /api/skills                          新建/覆盖（同名即编辑）
DELETE /api/skills?id=                    删除
"""
import db
import skills

class SkillRoutes:
    def _message_skill_bodies(self, mid: str) -> list:
        """按 mid 懒取一条消息上记录的技能全文（用户气泡徽章展开时调用）。

        归属两道闸，与记忆路由同一条纪律——id 是不可信输入：
        ①session_id 须属于当前用户（db.session_owner）；②mid 须在该会话名下
        （db.message_skills 的 WHERE 同时约束两者）。事件与消息列表只带
        {id,name,version} 元数据（skills.slim），全文只走这一条按需通道。
        """
        sid = (self._query().get("session_id") or [""])[0]
        if not sid or db.session_owner(sid) != (self.user or {}).get("id"):
            raise ValueError("需要指定一个属于你的会话")
        return db.message_skills(sid, mid)

    def _handle_skills(self):
        uid = self.user["id"]
        try:
            if self.command == "GET":
                mid = (self._query().get("mid") or [""])[0]
                if mid:
                    return self._json({"skills": self._message_skill_bodies(mid)})
                return self._json({"skills": skills.listing(uid)})
            if self.command == "POST":
                return self._json({"ok": True, "skill": skills.save(uid, self._body())})
            ident = (self._query().get("id") or [""])[0]
            skills.delete(uid, ident)
            return self._json({"ok": True})
        except (ValueError, UnicodeError) as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:  # 磁盘权限/IO 失败不是调用方的参数错，别回 400
            return self._json({"error": f"技能文件操作失败：{e}"}, 500)
