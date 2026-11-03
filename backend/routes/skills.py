"""Authenticated personal skill CRUD."""
import skills

class SkillRoutes:
    def _handle_skills(self):
        uid = self.user["id"]
        try:
            if self.command == "GET":
                return self._json({"skills": skills.listing(uid)})
            if self.command == "POST":
                return self._json({"ok": True, "skill": skills.save(uid, self._body())})
            ident = (self._query().get("id") or [""])[0]
            skills.path_for(uid, ident).unlink(missing_ok=True)
            return self._json({"ok": True})
        except (ValueError, UnicodeError) as e:
            return self._json({"error": str(e)}, 400)
