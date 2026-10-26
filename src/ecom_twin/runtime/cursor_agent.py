"""Cursor Agent 底座：`cursor-agent -p --output-format stream-json`。

参数依据本机 cursor-agent 2026.09.02 的 --help 编写，尚未做真实调用验证（见 PROGRESS.md）。
- MCP：写入工作目录的 .cursor/mcp.json，并用 --approve-mcps 免交互批准
- 权限：写入 .cursor/cli.json，禁止 shell 和写文件；不使用 --force / --yolo
- 系统提示词：写入工作目录的 AGENTS.md
"""

from __future__ import annotations

import json
from typing import Any

from .base import RunEvent, RunSpec
from .claude_code import ClaudeCodeRuntime


class CursorAgentRuntime(ClaudeCodeRuntime):
    name = "cursor"
    binary = "cursor-agent"

    def prepare_workdir(self, spec: RunSpec) -> None:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        cursor_dir = spec.workdir / ".cursor"
        cursor_dir.mkdir(exist_ok=True)
        (cursor_dir / "mcp.json").write_text(json.dumps({"mcpServers": spec.mcp_servers}, ensure_ascii=False),
                                             encoding="utf-8")
        (cursor_dir / "cli.json").write_text(json.dumps(
            {"permissions": {"allow": [f"Mcp({name}:*)" for name in spec.mcp_servers],
                             "deny": ["Shell(*)", "Write(**)"]}}, ensure_ascii=False), encoding="utf-8")
        (spec.workdir / "AGENTS.md").write_text(spec.system_prompt, encoding="utf-8")

    def build_command(self, spec: RunSpec, resume_id: str | None = None) -> list[str]:
        cmd = [self.binary, "-p", "--output-format", "stream-json", "--workspace", str(spec.workdir),
               "--trust", "--approve-mcps"]
        if spec.model:
            cmd += ["--model", spec.model]
        if resume_id:
            cmd += ["--resume", resume_id]
        cmd.append(spec.prompt)
        return cmd

    def parse_line(self, obj: dict[str, Any]) -> list[RunEvent]:
        if obj.get("type") == "tool_call" and obj.get("subtype") == "started":
            call = obj.get("tool_call") or {}
            name = next(iter(call), "tool") if isinstance(call, dict) else "tool"
            return [RunEvent("tool_call", {"name": name, "input": call.get(name) if isinstance(call, dict) else None})]
        return super().parse_line(obj)
