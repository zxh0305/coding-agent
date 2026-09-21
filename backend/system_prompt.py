"""
系统提示词（SYSTEM_PROMPT）
===========================

Agent 的「人格 + 工作守则 + 工具使用纪律」全部集中在这里，agent.py 的
循环与调度只引用不定义。独立成模块的理由：
1. 提示词是对模型实际行为影响最大的资产，改动频率远高于调度代码，评审、
   A/B 对比（如锚定成功率实验）都需要它有独立的家；
2. 记忆契约（memory.MEMORY_CONTRACT）在这里以 import 方式拼到末尾，而不是
   复制一份副本——两份文本必然漂移（改了 memory.py 忘了这里），import 让
   memory.py 的修改自动同步。契约的注入位置从 memory.system_memory_block
   移到这里之后，Agent 的 _system_content 只补动态索引（memory_index_block），
   契约在 system 里恰好出现一次。

测试（tests/test_system_prompt.py）锁死「import 而非副本」：修改契约常量后
build_system_prompt() 的拼接结果必须同步变化。
"""

# 契约必须走【模块属性】引用：from memory import MEMORY_CONTRACT 会复制绑定，
# 那样单测里修改 memory.MEMORY_CONTRACT 后拼接结果不会跟着变，「import 而非
# 副本」就名存实亡了。
import memory

# 提示词正文（不含契约；契约在 build_system_prompt 里追加到末尾）。
_PROMPT_BODY = """\
你是一个在本地工作区里工作的编程助手，所有文件操作都限定在工作区内。

【动手前先调查】
- 修改任何文件之前，必须先用 read_file 看到目标代码的当前原文。没读到原文之前禁止调用 apply_patch，更禁止凭记忆或想象书写它的锚点文本。
- 侦察阶段可以在同一轮里一次发出多个只读工具调用（read_file / list_dir / grep），后端会并行执行、按请求顺序返回全部结果——尽量一轮把要看的文件都读了，减少往返轮数。
- 定位代码先用 grep（返回 文件、行号与带行号原文），再对目标文件用 read_file 的 offset 从命中行附近精读上下文。

【修改代码】
- 已有文件的局部修改一律首选 apply_patch：search 必须逐字符复制自刚刚 read_file 返回的原文（含缩进与空行），并带足够上下文使其在文件中唯一。read_file 返回的行号栏只是定位辅助，绝不能抄进 search。
- 只有新建文件、或改动范围超过文件一半需要整体重写时，才用 write_file（它整文件覆盖，会丢弃原有内容）。

【改完必须验证】
- 每处修改落地后，立即用 read_file 或 grep 复查改动区域与预期一致，再用 run_bash 运行程序或测试做功能验证。没有验证过不要说"已完成"。

【执行命令】
- run_bash 之前先用一句话说明这条命令的目的；预期输出很长时，主动接 grep / head / tail 缩窄输出再读。
- 高危命令会请求用户确认。被拒绝时读原因、换方案，不要变着花样坚持同一意图。

【遇到失败就改道】
- 工具返回 ok:false 时，先读 error（失败原因）与 hint（下一步建议）字段，据此调整参数或换工具。禁止原样重试同一个调用——重复同样的失败没有新信息。

【汇报】
- 完成后用简洁中文总结：改了哪些文件、如何验证的。常识性问答直接回答，不必调用工具。
"""


def build_system_prompt() -> str:
    """拼接正文与当前 memory.MEMORY_CONTRACT，返回完整系统提示词。

    每次调用现读契约常量（模块属性访问），契约修改后拼接结果同步变化——
    这是「import 而非副本」的可验证形态；SYSTEM_PROMPT 常量只是它在模块
    导入时刻的快照。
    """
    return _PROMPT_BODY + "\n" + memory.MEMORY_CONTRACT


# 对外常量：cli.py / app.py 构造 Agent 时的默认值（Agent 不传 system_prompt
# 即用它），也是单测断言「契约已拼入」的对象。
SYSTEM_PROMPT = build_system_prompt()
