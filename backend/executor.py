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

import os
import signal
import subprocess
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 超时终止：必须杀【进程组】，不能只杀直接子进程
# ---------------------------------------------------------------------------
#
# 为什么：shell=True 时直接子进程是 `sh -c "<command>"`。复合命令（`a && b`、
# 管道、`&` 后台作业、`for` 循环）会由 sh 再 fork 出孙进程。只 kill(sh) 的话
# 孙进程被 init 收养后继续跑——实测 `sleep 300 & sleep 300` 超时 1s 后两个
# sleep 仍存活。对 coding agent 这是真实损耗：被超时掐掉的 npm install /
# pytest / dev server 会继续吃 CPU、占端口、持文件锁，长会话里累积。
#
# 解法：Popen 时 start_new_session=True（POSIX setsid / Windows
# CREATE_NEW_PROCESS_GROUP）让子进程自成进程组组长，超时后 killpg 整组。
_SPAWN_NEW_GROUP: dict = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)

# 杀组之后给"抽干管道 + 回收子进程"的宽限（秒）。SIGKILL 后句柄立即关闭，
# 正常情况下毫秒级返回；留这个上界只为防不可中断睡眠（D 状态）把 worker
# 线程钉死——有界失败好过无限挂起。
_KILL_DRAIN_TIMEOUT = 5.0


def _kill_process_group(proc: subprocess.Popen) -> None:
    """强杀 proc 所在【进程组】。进程组已消失时安静返回。

    只对"我们亲手 setsid 出来的组长"下手：pid 回绕导致误杀无关进程组的
    风险在于 killpg 的目标 id 可能被复用——这里以"组长已退出则跳过"收敛
    （组长退出后进程组可能仍在，故先 poll 再 killpg）。
    """
    if proc.poll() is not None:
        return  # 直接子进程已结束；组内残留交给下面的兜底
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # 组不存在/无权限：退回杀直接子进程，绝不因清理动作本身抛错
        try:
            proc.kill()
        except OSError:
            pass


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
        """跑一条 shell 命令。超时杀【整个进程组】并以 timed_out=True 返回。

        与 subprocess.run(timeout=) 的差别有两点，都是有意的：
        1. 超时杀的是进程组（见模块头注释），不留孤儿孙进程；
        2. 超时时返回【已产生的部分输出】——subprocess.run 在 POSIX 上只
           kill 再 wait、不抽管道，TimeoutExpired.stdout 通常是 None，模型
           拿不到任何线索。这里主动 drain，让模型能看到"超时前跑出了什么"
           （如测试日志的最后一屏），据此判断是加大超时还是换做法。
        """
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **_SPAWN_NEW_GROUP,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # 杀组后必须再 communicate 一次：既抽干管道（拿到部分输出），
            # 也回收子进程（否则留僵尸）。SIGKILL 下管道立即关闭，正常很快；
            # _KILL_DRAIN_TIMEOUT 是有界兜底，绝不无限等。
            try:
                out, err = proc.communicate(timeout=_KILL_DRAIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return ExecResult(exit_code=-1,
                              stdout=out or "", stderr=err or "",
                              timed_out=True)
        return ExecResult(exit_code=proc.returncode,
                          stdout=out or "", stderr=err or "")


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
