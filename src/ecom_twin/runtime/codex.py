"""Codex 底座：`codex exec --json`。

Codex 没有追加系统提示词的参数，系统提示词写进工作目录的 AGENTS.md。
沙箱固定为只读，审批策略为 never（需要提权的操作直接失败，不会自动放行）。
"""

from __future__ import annotations

import json
from typing import Any

from .base import AgentRuntime, RunEvent, RunSpec


class CodexRuntime(AgentRuntime):
    name = "codex"
    binary = "codex"

    def prepare_workdir(self, spec: RunSpec) -> None:
        super().prepare_workdir(spec)
        (spec.workdir / "AGENTS.md").write_text(spec.system_prompt, encoding="utf-8")

    def build_command(self, spec: RunSpec, resume_id: str | None = None) -> list[str]:
        cmd = [self.binary, "exec"]
        if resume_id:
            cmd += ["resume", resume_id]
        cmd += ["--json", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(spec.workdir),
                "-c", 'approval_policy="never"']
        for name, server in spec.mcp_servers.items():
            cmd += ["-c", f"mcp_servers.{name}.command={json.dumps(server['command'])}",
                    "-c", f"mcp_servers.{name}.args={json.dumps(server.get('args', []))}"]
        if spec.model:
            cmd += ["--model", spec.model]
        cmd.append(spec.prompt)
        return cmd

    def parse_line(self, obj: dict[str, Any]) -> list[RunEvent]:
        kind = obj.get("type")
        if kind == "thread.started":
            return [RunEvent("session", {"session_id": obj.get("thread_id")})]
        if kind == "item.completed":
            item = obj.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                return [RunEvent("message", {"text": item.get("text", "")})]
            if itype == "mcp_tool_call":
                return [RunEvent("tool_call", {"name": f"{item.get('server')}.{item.get('tool')}",
                                               "input": item.get("arguments")})]
            if itype == "command_execution":
                return [RunEvent("tool_call", {"name": "shell", "input": item.get("command"),
                                               "exit_code": item.get("exit_code")})]
            return []
        if kind == "turn.completed":
            usage = obj.get("usage") or {}
            return [RunEvent("usage", {"input_tokens": usage.get("input_tokens", 0),
                                       "output_tokens": usage.get("output_tokens", 0)}),
                    RunEvent("done", {"ok": True})]
        if kind in ("turn.failed", "error"):
            message = (obj.get("error") or {}).get("message") if kind == "turn.failed" else obj.get("message")
            return [RunEvent("error", {"message": message}), RunEvent("done", {"ok": False})]
        return []
