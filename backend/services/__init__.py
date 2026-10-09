"""业务服务层（从 app.py 抽出的顶层业务函数）
===========================================

这些函数原先散落在 app.py 顶层（与 HTTP 入口混在一起），按职责归拢到本包：

  model_resolve  激活模型 / 客户端 / 上下文窗口 / 视觉后端的解析链
  title          首轮问答生成会话标题
  trace          执行过程轨迹的裁剪、落库与"进行中快照"
  turn           回合执行体（POST 只入队，真正的生成在这里跑）
  browser        浏览器工具截图 → SSE 推送
  messages       用户消息组装（文本 + 图片/附件）与请求体上限常量
  workspace      任务工作区解析
  session_boot   取回（或创建）任务会话，装配好 Agent

依赖纪律（单向，禁止回环）：
  services/* → config / state / db / agent / tools / llm_client 等底层模块；
  services/* 【不得】import routes/* 或 app。

例外说明：session_boot 需要在重建 Agent 时装配权限闸门与视觉后端，这两个能力
由装配层（app.py）以"注入"方式提供（见 app.install_service_hooks），保持本层
对装配层零依赖。
"""
