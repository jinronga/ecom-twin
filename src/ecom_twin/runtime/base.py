"""Agent 运行时适配（DESIGN.md 5.6）：不同底座实现同一接口，输出统一事件流。

统一事件类型：session / step / tool_call / message / usage / error / done
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


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
        proc = subprocess.Popen(cmd, cwd=spec.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, env=env)
        assert proc.stdout is not None
        done = False
        watchdog = threading.Timer(spec.timeout_sec, proc.kill)  # 超时强制结束，避免任务卡死
        watchdog.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
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
            if proc.poll() is None:
                proc.kill()
