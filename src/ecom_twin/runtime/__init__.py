from .base import AgentRuntime, RunEvent, RunSpec
from .claude_code import ClaudeCodeRuntime
from .cli_check import CLI_SPECS, CliStatus, check, check_all, resolve
from .codex import CodexRuntime
from .cursor_agent import CursorAgentRuntime
from .opencode import OpenCodeRuntime

RUNTIMES: dict[str, type[AgentRuntime]] = {
    "claude_code": ClaudeCodeRuntime,
    "codex": CodexRuntime,
    "cursor": CursorAgentRuntime,
    "opencode": OpenCodeRuntime,
}


def build_runtime(key: str, configured_cli: dict[str, str]) -> AgentRuntime:
    """按配置的 CLI 路径创建运行时；未配置时自动查找。"""
    if key not in RUNTIMES:
        raise ValueError(f"运行时 {key} 的适配器还没实现，可用：{', '.join(RUNTIMES)}")
    path, _ = resolve(key, configured_cli.get(key, ""))
    return RUNTIMES[key](binary=path or None)


__all__ = ["AgentRuntime", "RunEvent", "RunSpec", "ClaudeCodeRuntime", "CodexRuntime", "CursorAgentRuntime", "OpenCodeRuntime", "RUNTIMES",
           "CLI_SPECS", "CliStatus", "build_runtime", "check", "check_all", "resolve"]
