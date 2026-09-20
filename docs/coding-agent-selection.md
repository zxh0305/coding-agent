# Coding Agent 调研与选型报告

> 信息日期：2026-09-20，来源为各项目 GitHub 仓库、官方文档与定价页的实时抓取。
> 结论针对本项目的现状：已有从零实现的 agent loop（agent_demo），Python 技术栈，目标是学习 agent 开发并做出可用的 coding agent。

## 一句话结论

**主线：继续从零造——把你现有的 agent_demo 循环升级成 coding agent（加代码工具 + 编辑格式 + 安全网），这是学习价值最高、且被 mini-SWE-agent 证明完全可行的路线。** 需要现成能力时，读 mini-SWE-agent 和 Aider 的源码当教材；想直接拿生产级产品时，再考虑 Claude Agent SDK（省事但锁 Claude）或 Pydantic AI（模型自由）。

## 1. 框架选型（要不要用框架、用哪个）

| 框架 | 状态（2026-09） | 一句话定位 | 对"学习原理" | 接 DeepSeek/GLM | 适合做 coding agent |
|---|---|---|---|---|---|
| **OpenAI Agents SDK** | v0.22.3，极活跃，MIT | 轻量多 Agent 工作流 | ★★★★ 原语贴近 loop 本质 | ✅ 兼容 base_url | 可用，coding 循环仍要自己组装 |
| **Pydantic AI** | v2.46，发版极频繁 | 类型化 Agent SDK | ★★★ 抽象适中 | ✅ 官方支持 | 很适合，内置 Coder 参考实现 |
| **smolagents** (HF) | v1.26，节奏放缓 | 极简 Code Agent（~1000 行核心） | ★★★★★ 读源码即懂本质 | ✅ 经兼容层 | 学原理教材；生产需自补沙箱 |
| **LangGraph** | v1.2.11，活跃 | 图状态机编排，生产级 | ★★ 图样板遮蔽 loop | ✅ | 偏重，适合复杂长流程 |
| **Claude Agent SDK** | v0.2.x，每日发版 | Claude Code 的官方 SDK | ★ 黑盒封装 | ❌ 仅 Claude | 最省事的现成 coding agent |
| **AutoGen** | ⚠️ 维护模式 | 多 Agent 对话先驱 | — | — | 不建议新项目 |

## 2. 开源 coding agent：值得读/借用的设计

| 项目 | 状态 | 借鉴价值 |
|---|---|---|
| **mini-SWE-agent** | MIT，约 100 行，SWE-bench Verified 65%+ | **最重要的行业结论**：极简 scaffold ≈ 复杂 scaffold。一个 bash 工具 + 线性消息历史就能打平复杂框架。最佳源码教材 |
| **Aider** | Apache-2.0，趋近维护态 | 两个黄金设计：① SEARCH/REPLACE 编辑块（锚定原文、抗幻觉、失败可重试）② tree-sitter repo map（全库骨架按引用频率排序，1k token 预算）。base_coder/repomap 模块当教材精读 |
| **Codex CLI**（开源） | Apache-2.0，Rust，极活跃 | **沙箱×审批正交模型**：sandbox_mode（read-only / workspace-write / full-access，OS 级 Seatbelt/bwrap 强制）× approval_policy（何时问人）两个独立维度；workspace 默认断网 |
| **OpenHands agent-sdk** | MIT | 想要完整框架时的选择：LLM/Agent/Conversation/Tool 四原语 + Docker/K8s 临时 workspace |
| **Cline** | Apache-2.0，活跃 | IDE 内 agent 交互范式：每步审批、diff 审阅、checkpoint 回滚 |
| **Gemini CLI** | Apache-2.0，活跃 | TS 生态参考；1M 上下文换上下文管理简单化的思路 |

## 3. 模型选型（你的 llm_client 本来就是 OpenAI 兼容，换模型只改 .env）

| 模型 | 定位 | 价格量级（$/M token，入/出） | 建议 |
|---|---|---|---|
| **DeepSeek**（现用，V4.1-Flash） | 1M 上下文，agentic 优化，开放权重 | 0.15 / 0.6（错峰） | **起步主力**，key 现成，便宜到可以放心让 agent 多跑轮次 |
| **GLM-5.3** | 开源 SOTA coding 旗舰；Flash 档约 Claude Opus 1/40 价 | 1.4 / 4.4（Flash 0.15/0.5） | 第二梯队对比项；有 Coding Plan 订阅制 |
| **Qwen3-Coder-Next** | 开放权重，专为 coding agent/本地设计 | API 走百炼；本地 30B 档可桌面跑 | 想玩本地部署时选它 |
| **Claude Sonnet 5 / Opus 5** | agentic coding 质量天花板档 | 2/10、5/25 | 质量不够再上，先不急 |
| **GPT-5.6 / GPT-6** | OpenAI agentic 主力 | 2/12 ~ 10/50 | 同上 |

策略：**起步用现成的 DeepSeek key 跑通全流程**，加一个 GLM-5.3-Flash 做对照组；质量问题明确出现在模型能力时再考虑 Claude/GPT 档。

## 4. 沙箱选型（分阶段）

1. **现在**：本机 subprocess + 双保险——git 自动提交做安全网（改坏了能回滚，Aider 的做法）+ 危险操作审批门（Cline/Codex 的做法）。仅在你自己的项目目录里玩。
2. **中期**：Docker 容器内执行，文件系统/进程/网络隔离，工作区挂载进去。
3. **规模化**：E2B / Daytona 托管沙箱（按秒计费，E2B 有免费额度），或 OpenAI Containers——到多租户/无人值守时再考虑。

## 5. 针对你的分阶段路线

**阶段 1（立刻可做）：给 agent_demo 加第一批代码工具**
- `read_file` / `write_file` / `apply_patch`（SEARCH/REPLACE 块格式）/ `grep` / `list_dir` / `run_bash`
- 安全网：每次编辑前 `git commit`（或要求项目是 git 仓库）；`run_bash` 走审批或白名单
- 前端已有审批 UI 的好底子（配置面板 + 气泡），可加"待批准操作"卡片

**阶段 2：上下文与验证**
- repo map：tree-sitter 抽签名 + 引用频率排序（抄 Aider 思路，实现成本极低）
- 让 agent 自己跑测试并按结果修复（coding agent 最有价值的循环）

**阶段 3：评测与隔离**
- 找 SWE-bench Lite 的小子集或 Terminal-Bench 任务跑通（mini-SWE-agent 仓库自带评测 harness）
- 执行环境迁入 Docker

**需要现成能力的时刻（替代路线）**：
- 想读源码学：**mini-SWE-agent**（100 行）→ **smolagents** → Aider 的 `base_coder.py`/`repomap.py`
- 想直接要产品：**Claude Agent SDK**（锁 Claude）或 **Pydantic AI**（模型自由，有 Coder harness）
- 想要平台：**OpenHands agent-sdk**

## 6. 明确不建议

- ❌ AutoGen（维护模式）；
- ❌ 上来就用 LangGraph（学习期图编排是过度设计）；
- ❌ 一开始就上 E2B/云沙箱（原型期本机 + git 安全网足够）；
- ❌ 让 agent 直接 `eval`/整文件重写大文件（用锚定原文的编辑格式）。
