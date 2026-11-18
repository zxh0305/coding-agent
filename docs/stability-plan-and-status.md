# 稳定性改造：方案与实施进度

> 调研 2026-11-18　|　对照：ZCode、deepseek-harness
> 本文是会话内《稳定性优化改造方案》的仓库副本，并持续记录**实际实施状态**。
> 与 `docs/agent-reliability-plan-2026-11-05.md` 是两个问题域：那份管「Agent 做对事」，
> 这份管「产品用起来稳」。

## 结论先放前面

别人的稳定主要来自：**显式状态机 + 可回放的事件日志 + 前后端共用契约 + 极细模块 +
大规模回归测试**。本项目的传输层（seq 补发/重连/resync、提交幂等收据）其实已经是
优等生；真正薄的是状态管理、持久化语义、可观测性、契约层。

## 实施进度

| 期 | 内容 | 状态 | 关键文件 |
|---|---|---|---|
| 0-A | 日志结构化上下文（sid/turn/tool） | ✅ 完成 `0711a54` | `backend/logger.py` |
| 0-C | 状态条卡住自证（静默 >45s 显示已等待秒数） | ✅ 完成 `0711a54` | `frontend/stall_watch.js` |
| 0-D | 回合诊断轨迹落盘（只记边界事件） | ✅ 完成 `1b68275` | `backend/diagnostics.py` |
| 0-E | 稳定性指标聚合 + `GET /api/stability` | ✅ 完成 `ae53193` | `backend/stability_metrics.py` |
| 1-A | 回合显式状态机（首版只断言告警） | ✅ 完成 `491344b` | `backend/turn_state.py` |
| 1-B | 清理工具结果可无损还原 | ✅ 完成 `7697aff` | `Agent.restore_cleared` |
| 1-C | 崩溃尾语义配平 | ✅ 完成 `45de394` | `backend/crash_tail.py` |
| 2-A | SSE 事件契约单一真相源 | ✅ 完成 `a228d5f` | `backend/contracts/events.py` |
| 2-C | 恢复/重放契约测试 | ✅ 完成 `3ff532b` | `tests/test_event_contract.py` |
| 3-1 | 异常收尾去静默 | ✅ 完成 `f991419` | `services/turn.py` |
| 3-2 | 架构门禁（分层 + import 环） | ✅ 完成 `9fff00f` | `tests/test_architecture.py` |
| 3-3 | pyproject + 依赖锁 + 静态名字检查 | ✅ 完成 `573e3c8` | `pyproject.toml` |
| 3-4 | 拆文件（agent.py / app.js） | ⬜ 未做 | — |

## 怎么用这些设施（日常排查）

**1. 用户说"卡住了"**

```bash
# 打开日志上下文后，一次任务的所有日志都带 sid
LOG_CONTEXT=1 ./start.sh          # 或 python3 backend/app.py
grep "sid=abc12345" data/logs/*.log
```

更直接的是看这回合跑到哪一步、慢在模型还是工具：

```bash
ls data/diagnostics/<sid>/         # 每回合一个 jsonl
tail -1 data/diagnostics/<sid>/*.jsonl | python3 -m json.tool
# turn_end 里的 llm_total_s / tool_total_s 直接告诉你慢在哪
```

**2. 看整体稳不稳**

```bash
curl -s localhost:8000/api/stability?days=7 | python3 -m json.tool
```

口径（改之前先想清楚，别让两次统计前后矛盾）：
- 用户主动停止**不算失败**，单独计入 `stop_rate`
- 缺字段的回合不参与分位计算（不按 0 秒拉低）
- `tool_time_share > 0.6` 通常说明慢在命令而不是慢在模型

**3. 事件契约校验（改事件字段时）**

```bash
EVENT_CONTRACT_STRICT=1 ./start.sh      # 只告警
EVENT_CONTRACT_STRICT=2 ./start.sh      # 直接抛错（CI/开发时用）
```

改了 `contracts/events.py` 记得重导 schema，否则 `test_event_contract`
里的 `test_schema_file_is_in_sync` 会红：

```bash
cd backend && python3 -c "import json,sys;sys.path.insert(0,'.');from contracts import events;\
open('../frontend/events.schema.json','w').write(json.dumps(events.schema(),ensure_ascii=False,indent=2)+'\n')"
```

**4. 状态机严格模式**

默认只记 warning、**不拦截**。观察一段时间零告警后再考虑：

```bash
AGENT_STATE_STRICT=1 ./start.sh
```

非法迁移会出现在日志里（`backend/turn_state.py` 的 `log.warning`）。

## 刻意的取舍（别"顺手优化"掉）

- **状态机默认只告警**：回合循环存在合法但看起来奇怪的路径（压缩后重来、收尾轮
  复用同一份流）。第一版就拦会在用户面前打断正常运行。
- **崩溃尾默认判 `TOOL_OUTCOME_UNKNOWN`**：把"其实没开始"误判成未知，代价是多一步
  确认；把"其实已改文件"误判成没开始，代价是重复副作用。两者不对称。
- **`_derived.original` 只在内存**：它只为 `restore_cleared()` 提供退路，落库等于
  把清掉的几万字符又存一份（见 `db._storable_body` 的剥离逻辑）。
- **契约默认不校验**：逐事件 dict 检查会给每个 token 加开销；失败只告警不阻断——
  一个字段名拼错不该让用户看到"卡住"。
- **db.py:400 的 `import memory` 必须留在函数内**：这是有意的破环手法，
  `test_architecture.py` 里有一条测试钉着。

## 验证方式

```bash
cd backend && python3 -m unittest discover -s tests -t .
node --test frontend/*.test.mjs
```

不需要 pytest（.venv 里没有）。当前基线：后端 1027 用例、前端 135 用例全绿。
