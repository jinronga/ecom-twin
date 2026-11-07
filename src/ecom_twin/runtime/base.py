"""Agent 运行时适配（DESIGN.md 5.6）：不同底座实现同一接口，输出统一事件流。

统一事件类型：session / step / tool_call / message / usage / error / done
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..config import agent_hidden_env


@dataclass
class RunEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunSpec:
    prompt: str
    system_prompt: str
    workdir: Path
    # MCP server 定义：{名称: {"command": ..., "args": [...]}}。只挂这些，不挂用户全局配置
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 允许 agent 使用的工具（MCP 工具名）。写平台的工具从不出现在这里（DESIGN.md 5.5）
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    timeout_sec: float = 1800
    # 底座原始输出（每行一个 JSON 事件）原样存档，用于审计和复盘（DESIGN.md 10.2）
    transcript_path: Path | None = None
    # 允许 agent 在 shell 里跑的命令（按命令名放行）。空 = 不给 shell。目前只给操作电脑用（osascript 等）
    shell_commands: list[str] = field(default_factory=list)
    # 给 shell 时，每条命令执行前先跑的检查命令（shell_guard）；目前只有 Claude Code 支持（PreToolUse hook）
    shell_guard: list[str] = field(default_factory=list)

    def tools_by_server(self) -> dict[str, list[str]]:
        """把 allowed_tools（mcp__server__tool）按 MCP server 分组，给没有 --allowedTools 参数的底座写配置用。"""
        grouped: dict[str, list[str]] = {name: [] for name in self.mcp_servers}
        for full in self.allowed_tools:
            _, server, tool = full.split("__", 2)
            grouped.setdefault(server, []).append(tool)
        return grouped


def kill_tree(proc: subprocess.Popen, grace_sec: float = 3.0) -> None:
    """结束 proc 所在的整个进程组（proc 以 start_new_session 启动，组号就是它的 pid）：先 TERM，等一会再 KILL。"""
    def signal_group(sig: int) -> bool:
        try:
            os.killpg(proc.pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False  # 整组都已经退出
    if not signal_group(signal.SIGTERM):
        return
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.1)
    else:
        signal_group(signal.SIGKILL)
    if proc.poll() is None:
        try:
            proc.wait(timeout=grace_sec)
        except subprocess.TimeoutExpired:
            proc.kill()


class AgentRuntime(ABC):
    name: str = ""
    binary: str = ""

    def __init__(self, binary: str | None = None):
        self.binary = binary or self.binary

    @abstractmethod
    def build_command(self, spec: RunSpec, resume_id: str | None = None) -> list[str]:
        """组装无头调用命令。resume_id 不为空时续跑该会话。"""

    @abstractmethod
    def parse_line(self, obj: dict[str, Any]) -> list[RunEvent]:
        """把底座原生的一行 JSON 事件转换成统一事件。"""

    def prepare_workdir(self, spec: RunSpec) -> None:
        spec.workdir.mkdir(parents=True, exist_ok=True)

    def runtime_env(self, env: dict[str, str]) -> dict[str, str]:
        """子进程环境变量的最后调整（例如指向隔离的配置目录）。默认不改。"""
        return env

    def run(self, spec: RunSpec, resume_id: str | None = None) -> Iterator[RunEvent]:
        self.prepare_workdir(spec)
        cmd = self.build_command(spec, resume_id)
        env = os.environ.copy()
        # Do not let a stale global Claude model override the runtime's normal model
        # selection. A proxy may expose a model alias that Claude Code cannot use.
        if self.name == "claude_code":
            for key in ("ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
                        "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
                        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"):
                env.pop(key, None)
        for key in agent_hidden_env():  # ecom-twin 的密钥不进 agent 环境（MCP 服务自己读 .env）
            env.pop(key, None)
        env = self.runtime_env(env)
        # 独立进程组：底座 CLI 会再起 MCP 服务、node 包装器等子进程。超时只杀 CLI 本身的话，它们留下来还占着
        # 输出管道，读输出那一步会一直卡着（缺口清单 2026-10-28：超时后子进程仍存活）。结束时整组清理
        proc = subprocess.Popen(cmd, cwd=spec.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
        assert proc.stdout is not None
        done = False
        watchdog = threading.Timer(spec.timeout_sec, kill_tree, args=(proc,))  # 超时强制结束整组，避免任务卡死
        watchdog.start()
        transcript = None
        if spec.transcript_path:
            spec.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript = open(spec.transcript_path, "a", encoding="utf-8")
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if transcript:
                    transcript.write(line + "\n")
                    transcript.flush()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    yield RunEvent("message", {"text": line, "raw": True})
                    continue
                for event in self.parse_line(obj):
                    done = done or event.type == "done"
                    yield event
            code = proc.wait(timeout=spec.timeout_sec)
            if code != 0:
                err = proc.stderr.read() if proc.stderr else ""
                yield RunEvent("error", {"exit_code": code, "message": err[-2000:]})
                if not done:
                    yield RunEvent("done", {"ok": False})
            elif not done:  # 有的底座不输出结束事件，以进程正常退出为准
                yield RunEvent("done", {"ok": True})
        finally:
            watchdog.cancel()
            if transcript:
                transcript.close()
            kill_tree(proc)  # 正常结束也清一遍：CLI 退出后留下的子进程不能一直挂着
