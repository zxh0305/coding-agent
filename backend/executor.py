"""
命令执行后端（Executor seam，借鉴 DeepSeek Harness 的 capability seam）
======================================================================

run_bash 的"怎么跑"与"跑什么"分离：工具层（code_tools.run_bash）只管参数
校验、信封组装与输出截断；进程级的执行交给 Executor 提供者。默认提供者是
LocalExecutor（本机子进程，与旧内联实现逐字节同行为）；把提供者换成
DockerExecutor 即获得沙箱隔离，工具层、权限闸门、审计钩子一行不改。

Provider 契约（实现者必读）：
- run(command, cwd, timeout) -> ExecResult：cwd 必须原样生效——工具层与权限
  闸门都以"工作区路径"为语义边界，沙箱实现必须把工作区【挂载在完全相同的
  绝对路径】下（而不是容器内的别的路径），否则 cwd 与文件路径的语义全部
  错位；timeout 由提供者负责强制（到点终止进程），超时返回
  ExecResult(timed_out=True)，信封文案由工具层统一给。
- 输出合并语义：stdout 与 stderr 分开捕获，由工具层拼合（stdout 尾部截断
  MAX_OUTPUT_CHARS、stderr 减半截断）——提供者不要自己截断或合并。
- 同步阻塞调用：工具在线程池/工作线程里跑，提供者内部可自由用子进程/远程
  API，但对外保持同步。
- 抛异常 = 提供者自身故障（守护进程不在等），会以"工具内部异常"信封回填，
  不是命令的非零退出——命令失败永远走 ExecResult.exit_code。

安装新提供者：构造 Agent 后向 ctx 注入（ToolContext.executor），或改
default_executor()。权限闸门先于执行判定，且只认工作区边界——沙箱是纵深
防御的第二层，不能替代闸门。
"""

import subprocess
from dataclasses import dataclass


@dataclass
class ExecResult:
    """一次命令执行的结果。timed_out=True 时 exit_code 无意义（恒 -1）。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class LocalExecutor:
    """本机子进程执行（默认提供者，与旧 run_bash 内联实现行为一致）。"""

    def run(self, command: str, cwd, timeout: int) -> ExecResult:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(exit_code=-1,
                              stdout=e.stdout or "" if isinstance(e.stdout, str) else "",
                              stderr=e.stderr or "" if isinstance(e.stderr, str) else "",
                              timed_out=True)
        return ExecResult(exit_code=proc.returncode,
                          stdout=proc.stdout or "", stderr=proc.stderr or "")


class DockerExecutor:
    """Docker 沙箱提供者（占位：本机未装 Docker，装好后实现 run 即启用）。

    实现要点（契约见模块 docstring）：
    1. 工作区挂载到【相同的绝对路径】（-v <workspace>:<workspace>），
       cwd 语义不变；
    2. `docker run --rm --network <policy>` ——网络策略按需收紧（默认桥接，
       沙箱场景可 none）；再加 --memory / --cpus 防资源失控；
    3. 以 `docker exec` 语义执行 shell 命令，timeout 到点 kill 容器进程；
    4. 镜像里预置 python3（回放剧本与验证链路依赖它）。
    """

    def run(self, command: str, cwd, timeout: int) -> ExecResult:
        raise NotImplementedError("DockerExecutor 未实现：本机未安装 Docker"
                                  "（候选运行时 OrbStack/colima），装好后按模块"
                                  " docstring 的要点实现 run() 并注入 ToolContext")


_default = LocalExecutor()


def default_executor():
    return _default
