"""记忆路由：列出 / 读取 / 编辑 / 删除 / 换层
=================================================

记忆 v2 之前，记忆只有两个"用户"：模型（读索引）和轮末提取器（写文件）。
人看不见它、也改不了它——一个号称要知道你的事的系统，你对它如何了解你零控制。
本模块是第一个给【人】用的记忆入口。

层：
  personal —— data/memory/users/<当前用户 id>/   关于用户这个人，跨项目
  project  —— data/memory/projects/<项目身份>/   当前会话工作区所属项目

归属与安全：
  · personal 层一律用 self.user["id"] 解析，【绝不接受请求里传来的 uid】；
  · project 层用会话的工作区解析（工作区归属由 db.session_owner 校验）；
  · 文件名走 memory.valid_memory_filename 白名单（小写+短横线+.md），
    再加一层 realpath 包含校验——UI 是新增的写入面，不能比提取器那条路松。

响应形状沿用既有约定：成功 {"ok": True, ...} / 列表 {"memories": [...]} /
参数错 400 {"error": ...} / 目标不存在 404 {"error": ...}。
"""

import memory


class MemoryRoutes:

    # ---------- 层解析 ----------

    def _memory_sid(self, body: dict | None = None) -> str:
        """当前该用哪个会话：POST 从 body 取，GET/DELETE 从查询串取。

        归属一律现查（db.session_owner），拿别人的 id 解析不出目录——
        与 _query_sid 同一条纪律：读接口不能因为一个 id 就泄露别人的内容。
        """
        import db
        sid = ""
        if body:
            sid = str(body.get("session_id") or "")
        if not sid:
            sid = (self._query().get("session_id") or [""])[0]
        if sid and db.session_owner(sid) != (self.user or {}).get("id"):
            return ""
        return sid

    def _memory_layer_dir(self, layer: str, body: dict | None = None):
        """层名 → (目录, 该层是否可用)。无法解析时返回 (None, 原因)。"""
        if layer == "personal":
            uid = (self.user or {}).get("id")
            if uid is None:
                return None, "未登录"
            return memory.memory_dir_for_user(uid), None
        if layer == "project":
            import db
            sid = self._memory_sid(body)
            if not sid:
                return None, "需要指定一个属于你的会话（项目记忆按会话的工作区归属）"
            workspace = db.get_session_workspace(sid)
            if not workspace:
                return None, "该会话未绑定工作区，没有项目记忆"
            return memory.memory_dir_for_project(workspace), None
        return None, f"未知的层：{layer!r}（只支持 personal / project）"

    def _layer_or_error(self, layer: str, body: dict | None = None):
        d, err = self._memory_layer_dir(layer, body)
        if err:
            self._json({"error": err}, 400)
            return None
        return d

    # ---------- GET /api/memory?layer=&session_id=&name= ----------

    def _handle_memory_list(self):
        """列出某层全部记忆；带 name 则读单条原文。

        project 层的可用性依赖当前会话的工作区，所以前端要带 session_id。
        personal 层不需要（它跟着人走）。
        """
        q = self._query()
        layer = (q.get("layer") or ["personal"])[0]
        mem_dir = self._layer_or_error(layer)
        if mem_dir is None:
            return
        name = (q.get("name") or [""])[0]
        if name:
            try:
                return self._json({"layer": layer, **memory.read_entry(mem_dir, name)})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except FileNotFoundError:
                return self._json({"error": "记忆不存在"}, 404)
        self._json({"layer": layer, "dir": str(mem_dir),
                    "memories": memory.list_entries(mem_dir)})

    # ---------- POST /api/memory ----------

    def _handle_memory_save(self):
        """新建或覆盖一条记忆。

        body: {layer, name, body, description?, type?, session_id?}
        同名即覆盖（= 编辑）。描述可省——省了从正文首行兜底，避免索引退化成空钩子。
        """
        data = self._body()
        layer = str(data.get("layer") or "personal")
        mem_dir = self._layer_or_error(layer, data)
        if mem_dir is None:
            return
        name = str(data.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name 参数"}, 400)
        try:
            info = memory.write_entry(
                mem_dir, name, str(data.get("body") or ""),
                description=str(data.get("description") or ""),
                mtype=str(data.get("type") or ("user" if layer == "personal" else "project")),
                owner="user" if layer == "personal" else None,
                scope="global" if layer == "personal" else None)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"写入失败：{e}"}, 500)
        self._json({"ok": True, "layer": layer, **info})

    # ---------- POST /api/memory/move ----------

    def _handle_memory_move(self):
        """把一条记忆在两层之间搬家——"用户自己挑"的核心动作。

        个人层初始为空，只有靠它才会长出内容（74 条历史记忆全在项目层，
        迁移时按约定没有自动提升任何一条）。
        body: {name, from, to, session_id?}
        """
        data = self._body()
        name = str(data.get("name") or "").strip()
        src_layer = str(data.get("from") or "")
        dst_layer = str(data.get("to") or "")
        if not name:
            return self._json({"error": "缺少 name 参数"}, 400)
        if src_layer == dst_layer:
            return self._json({"error": "源层与目标层相同"}, 400)
        src = self._layer_or_error(src_layer, data)
        if src is None:
            return
        dst = self._layer_or_error(dst_layer, data)
        if dst is None:
            return
        try:
            info = memory.move_entry(
                src, dst, name,
                owner="user" if dst_layer == "personal" else None)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except FileNotFoundError:
            return self._json({"error": "记忆不存在"}, 404)
        except OSError as e:
            return self._json({"error": f"搬移失败：{e}"}, 500)
        self._json({"ok": True, "from": src_layer, "to": dst_layer, **info})

    # ---------- DELETE /api/memory/<name>?layer=&session_id= ----------

    def _handle_memory_delete(self, name: str):
        layer = (self._query().get("layer") or ["personal"])[0]
        mem_dir = self._layer_or_error(layer)
        if mem_dir is None:
            return
        try:
            existed = memory.delete_entry(mem_dir, name)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"删除失败：{e}"}, 500)
        self._json({"ok": True, "file": name, "existed": existed})
