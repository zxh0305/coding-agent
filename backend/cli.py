"""
命令行入口（终端版，与 Web 版共用 backend/ 里的 Agent 核心和 .env 配置）
==========================================================================

用法：
  python3 backend/cli.py                 # 交互式多轮对话（REPL）
  python3 backend/cli.py "你的问题"       # 单次提问后退出，方便快速验证
  python3 backend/cli.py --quiet "问题"   # 关闭过程日志，只看最终回答

运行前需在 .env 中配置 LLM_API_KEY（也可以在网页版
「python3 backend/app.py」的设置面板里配置，两者共用一份 .env）。
"""

import logging
import os
import sys

from agent import Agent
from llm_client import create_llm_client
from logger import setup_logging
from tools import describe_tools
from ui import colored

log = logging.getLogger("cli")


def build_agent(verbose: bool) -> Agent:
    llm = create_llm_client()   # 先加载 .env，LOG_FILE / LOG_LEVEL 才能生效
    log_file = setup_logging()
    # CLI 没有网页版的管理面板，窗口从 .env 读（CONTEXT_WINDOW），默认 128k——
    # 自动压缩的触发线以它为基准，宁可保守早点压
    agent = Agent(llm=llm, verbose=verbose,
                  context_window=int(os.environ.get("CONTEXT_WINDOW", "128000")))
    log.info("会话开始 model=%s api=%s verbose=%s", llm.model, llm.api_url, verbose)
    print(colored("═" * 56, "blue"))
    print(colored("  🤖 Agent 问答 Demo（命令行版）", "bold"))
    print(colored(f"  模型: {llm.model}    API: {llm.api_url}", "gray"))
    print(colored(f"  工作区: {agent.ctx.workspace}", "gray"))
    print(colored("  已注册工具:", "blue"))
    print(colored(describe_tools(), "gray"))
    print(colored("  命令: /tools 查看工具  /reset 清空历史  /exit 退出", "gray"))
    print(colored(f"  📝 日志: {log_file}", "gray"))
    print(colored("═" * 56, "blue"))
    return agent


def ask_once(agent: Agent, question: str) -> None:
    log.info("用户提问: %s", question)
    print(colored(f"\n你：{question}", "green"))
    answer = agent.chat(question)
    log.info("最终回答: %s", answer)
    print(colored(f"\n🤖 {answer}", "magenta"))


def safe_ask(agent: Agent, question: str) -> None:
    """提问 + 统一的错误处理：完整堆栈进日志，终端只显示简短原因。"""
    try:
        ask_once(agent, question)
    except RuntimeError as e:
        log.exception("LLM 请求失败")
        print(colored(f"[出错] {e}", "red"))


def repl(agent: Agent) -> None:
    while True:
        try:
            user_input = input(colored("\n你 > ", "green")).strip()
        except (KeyboardInterrupt, EOFError):
            print(colored("\n再见！", "gray"))
            return
        if not user_input:
            continue
        if user_input in ("/exit", "/quit", "exit", "quit"):
            print(colored("再见！", "gray"))
            return
        if user_input == "/reset":
            agent.reset()
            log.info("用户清空了对话历史")
            print(colored("（对话历史已清空）", "gray"))
            continue
        if user_input == "/tools":
            print(colored("可用工具:", "blue"))
            print(colored(describe_tools(), "gray"))
            continue
        if user_input == "/help":
            print("命令: /tools /reset /exit；直接输入文字即提问")
            continue
        safe_ask(agent, user_input)


def main() -> None:
    args = [a for a in sys.argv[1:]]
    verbose = "--quiet" not in args
    args = [a for a in args if a != "--quiet"]

    agent = build_agent(verbose=verbose)

    if args:  # 命令行带了问题：单次提问模式
        safe_ask(agent, " ".join(args))
        return
    repl(agent)


if __name__ == "__main__":
    main()
