"""HTTP 路由层（Handler 的按域拆分）
=================================

原 app.py 里的 Handler 类太大（鉴权 + 响应工具 + 25 个 _handle_* 全挤在一起），
这里按域拆成若干 mixin，由 app.py 组装成一个 Handler：

    class Handler(AuthRoutes, SessionRoutes, ChatRoutes, ModelRoutes,
                  GitRoutes, WorkspaceRoutes, FileRoutes, HandlerBase):
        ...

为什么用 mixin 而不是"注册式路由表"：所有 _handle_* 方法体一字不改，直接依赖
self._json / self._body / self._query / self._require_auth 等基类方法；mixin 多
继承是**纯搬移**，不触碰分发逻辑，风险最低。（真正的注册式分发是独立的后续
项，会改行为，不夹带在本次结构重排里。）

依赖纪律：routes/* → services/* 与底层模块；routes/* 之间只通过 base 的方法
协作。
"""
