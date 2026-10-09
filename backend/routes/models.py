"""模型供应商路由：激活模型 / 供应商增删改 / 模型增删 / 测试链接
=============================================================

从原 app.py 的 Handler 逐字搬移：
  _handle_active_model / _handle_provider_save / _handle_provider_delete
  _handle_model_save / _handle_model_delete / _handle_provider_test
"""

import time
import uuid

import db
from config import ENV_FILE
from llm_client import create_client, save_env_values
from services.model_resolve import _mask, _resolve_active  # noqa: F401  （保留供后续扩展）

from routes import busref


class ModelRoutes:
    def _handle_active_model(self):
        """切换激活模型。带 session_id = 只改该任务用哪个模型；不带 = 改全局默认，
        即"新任务的初始模型"（新任务还没有会话行，无法按会话存）。
        校验的是"该供应商下存在这个模型"而非"已启用"：与旧行为一致，已停用的
        模型仍可被显式选中（解析时会回落，但用户的显式选择不被静默吞掉）。"""
        body = self._body()
        pid, model = body.get("provider_id"), body.get("model")
        sid = str(body.get("session_id") or "").strip()
        if sid and db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        prov = db.get_provider(str(pid)) if pid else None
        if prov is None:
            return self._json({"error": "供应商不存在"}, 400)
        if model not in [m["name"] for m in prov["models"]]:
            return self._json({"error": f"供应商 {prov['name']} 下没有模型 {model}"}, 400)
        if sid:
            db.set_session_model(sid, prov["id"], model)
            busref.log.info("会话 %s 的模型切换为 %s / %s", sid, prov["name"], model)
        else:
            db.set_setting("active_model", {"provider_id": prov["id"], "model": model})
            busref.log.info("默认模型（新任务初始模型）切换为 %s / %s", prov["name"], model)
        self._json({"ok": True, **self._config_view(sid or None)})

    def _handle_provider_save(self):
        b = self._body()
        name = (b.get("name") or "").strip()
        base_url = (b.get("base_url") or "").strip()
        api_format = (b.get("api_format") or "openai").strip()
        if api_format not in ("openai", "anthropic"):
            return self._json({"error": f"不支持的 API 格式：{api_format}（支持 openai / anthropic）"}, 400)
        if not name or not base_url:
            return self._json({"error": "名称和 Base URL 不能为空"}, 400)
        pid = (b.get("id") or "").strip() or uuid.uuid4().hex[:6]
        exists = db.get_provider(pid) is not None
        api_key = (b.get("api_key") or "").strip() or None  # None/空 = 保持原 key
        # 供应商级默认窗口：不传（None）= 保持原值；窗口解析链"模型自填 → 这里 → .env"
        try:
            prov_window = int(b.get("context_window")) if b.get("context_window") else None
        except (TypeError, ValueError):
            return self._json({"error": "上下文窗口须为整数（token 数）"}, 400)
        db.upsert_provider(pid, name, base_url, api_key, bool(b.get("enabled", True)),
                           api_format, context_window=prov_window,
                           # 思考模型的推理回传开关；请求里没带 = 保持原值不变
                           reasoning_replay=(bool(b["reasoning_replay"])
                                             if "reasoning_replay" in b else None))
        for m in b.get("models") or []:  # 可选：创建时一并带模型列表
            if (m.get("name") or "").strip():
                db.upsert_model(pid, m["name"].strip(), int(m.get("context_window") or 262144),
                                bool(m.get("enabled", True)), bool(m.get("vision", False)))
        # "默认"供应商的改动同步回 .env，保证命令行版（读 .env）一致
        if pid == "default" and exists:
            prov = db.get_provider(pid)
            save_env_values({
                "LLM_BASE_URL": prov["base_url"],
                "LLM_API_KEY": prov["api_key"],
            }, str(ENV_FILE))
        db.ensure_active_model()  # 激活模型被改名/删除时自动纠正，避免指向失效模型
        busref.log.info("供应商已保存: %s (%s)", name, pid)
        self._json({"ok": True, "id": pid})

    def _handle_provider_delete(self):
        pid = (self._body().get("id") or "").strip()
        if pid == "default":
            return self._json({"error": "默认供应商不可删除，只能停用"}, 400)
        db.delete_provider(pid)
        db.ensure_active_model()
        busref.log.info("供应商已删除: %s", pid)
        self._json({"ok": True})

    def _handle_model_save(self):
        b = self._body()
        pid, name = (b.get("provider_id") or "").strip(), (b.get("name") or "").strip()
        if db.get_provider(pid) is None:
            return self._json({"error": "供应商不存在"}, 400)
        if not name:
            return self._json({"error": "模型名不能为空"}, 400)
        vision = b.get("vision")
        db.upsert_model(pid, name, int(b.get("context_window") or 262144),
                        bool(b.get("enabled", True)),
                        None if vision is None else bool(vision))
        db.ensure_active_model()
        self._json({"ok": True})

    def _handle_model_delete(self):
        b = self._body()
        db.delete_model((b.get("provider_id") or "").strip(), (b.get("name") or "").strip())
        db.ensure_active_model()
        self._json({"ok": True})

    def _handle_provider_test(self):
        """测试链接：用表单里的协议格式 / Base URL / Key / 模型名发一次真实的 ping 请求。"""
        b = self._body()
        base = (b.get("base_url") or "").strip()
        model = (b.get("model") or "").strip()
        api_format = (b.get("api_format") or "openai").strip()
        api_key = (b.get("api_key") or "").strip()
        if not api_key and b.get("provider_id"):  # Key 留空 = 用已保存的
            prov = db.get_provider(b["provider_id"])
            api_key = prov["api_key"] if prov else ""
        if not (base and model and api_key):
            return self._json({"ok": False, "error": "Base URL、模型名、API Key 缺一不可（Key 留空则用已保存的）"})
        client = create_client(api_format, api_key=api_key, base_url=base, model=model, timeout=20)
        start = time.time()
        try:
            msg = client.chat([{"role": "user", "content": "只回复：ok"}])
            latency = round((time.time() - start) * 1000)
            content = (msg.get("content") or "").strip()[:60]
            busref.log.info("测试链接成功 %s (%dms)", base, latency)
            self._json({"ok": True, "latency_ms": latency, "reply": content})
        except RuntimeError as e:
            self._json({"ok": False, "error": str(e)})
