"""Claude Code 底座：`claude -p --output-format stream-json`。"""

from __future__ import annotations

import json
import shlex
from typing import Any

from .base import AgentRuntime, RunEvent, RunSpec

# 平台写操作从不暴露成工具；这里再显式禁用内置的执行和写文件类工具（DESIGN.md 5.6.4）
DISALLOWED_BUILTINS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch"]


class ClaudeCodeRuntime(AgentRuntime):
    name = "claude_code"
    binary = "claude"

    def prepare_workdir(self, spec: RunSpec) -> None:
        super().prepare_workdir(spec)
        (spec.workdir / "mcp.json").write_text(
            json.dumps({"mcpServers": spec.mcp_servers}, ensure_ascii=False), encoding="utf-8"
        )
        if spec.shell_commands and spec.shell_guard:
            # 每条 Bash 命令执行前先过 shell_guard：拦 do shell script 这类绕过白名单的写法和禁止目标
            hook = {"type": "command", "command": " ".join(shlex.quote(part) for part in spec.shell_guard)}
            (spec.workdir / "settings.json").write_text(json.dumps(
                {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}, ensure_ascii=False), encoding="utf-8")

    def build_command(self, spec: RunSpec, resume_id: str | None = None) -> list[str]:
        cmd = [self.binary, "-p", spec.prompt, "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", spec.system_prompt,
               "--mcp-config", str(spec.workdir / "mcp.json"), "--strict-mcp-config",
               "--disallowedTools", ",".join(b for b in DISALLOWED_BUILTINS
                                             if not (b == "Bash" and spec.shell_commands))]
        # 给 shell 时只按命令前缀放行（无人值守模式下名单外的命令直接被拒，&& 拼接的也会拆开校验）
        allowed = spec.allowed_tools + [f"Bash({c}:*)" for c in spec.shell_commands]
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]
        if spec.shell_commands and spec.shell_guard:
            cmd += ["--settings", str(spec.workdir / "settings.json")]
        if spec.model:
            cmd += ["--model", spec.model]
        if resume_id:
            cmd += ["--resume", resume_id]
        return cmd

    def parse_line(self, obj: dict[str, Any]) -> list[RunEvent]:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            return [RunEvent("session", {"session_id": obj.get("session_id")})]
        if kind == "assistant":
            events = []
            for block in (obj.get("message") or {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    events.append(RunEvent("message", {"text": block["text"]}))
                elif block.get("type") == "tool_use":
                    events.append(RunEvent("tool_call", {"name": block.get("name"), "input": block.get("input")}))
            return events
        if kind == "result":
            usage = obj.get("usage") or {}
            events = [RunEvent("usage", {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": obj.get("total_cost_usd"),
            })]
            if obj.get("is_error"):
                events.append(RunEvent("error", {"message": obj.get("result") or obj.get("subtype")}))
            events.append(RunEvent("done", {"session_id": obj.get("session_id"), "result": obj.get("result"),
                                            "ok": not obj.get("is_error")}))
            return events
        return []
