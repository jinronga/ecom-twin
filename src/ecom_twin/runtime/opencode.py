"""OpenCode 底座：`opencode run --format json`。

参数依据本机 opencode 1.2.13 的 --help 编写，事件格式尚未做真实调用验证（见 PROGRESS.md）。
- MCP 和权限：写入工作目录的 opencode.json（禁止 edit / bash / webfetch）
- 系统提示词：写入工作目录的 AGENTS.md
"""

from __future__ import annotations

import json
from typing import Any

from .base import AgentRuntime, RunEvent, RunSpec


class OpenCodeRuntime(AgentRuntime):
    name = "opencode"
    binary = "opencode"

    def prepare_workdir(self, spec: RunSpec) -> None:
        super().prepare_workdir(spec)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {name: {"type": "local", "command": [s["command"], *s.get("args", [])], "enabled": True}
                    for name, s in spec.mcp_servers.items()},
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        }
        (spec.workdir / "opencode.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        (spec.workdir / "AGENTS.md").write_text(spec.system_prompt, encoding="utf-8")

    def build_command(self, spec: RunSpec, resume_id: str | None = None) -> list[str]:
        cmd = [self.binary, "run", "--format", "json", "--dir", str(spec.workdir)]
        if resume_id:
            cmd += ["--session", resume_id]
        if spec.model:
            cmd += ["--model", spec.model]
        cmd.append(spec.prompt)
        return cmd

    def parse_line(self, obj: dict[str, Any]) -> list[RunEvent]:
        events = []
        session = obj.get("sessionID") or (obj.get("part") or {}).get("sessionID")
        if session and getattr(self, "_seen_session", None) != session:
            self._seen_session = session
            events.append(RunEvent("session", {"session_id": session}))
        kind, part = obj.get("type"), obj.get("part") or {}
        if kind == "text" and part.get("text"):
            events.append(RunEvent("message", {"text": part["text"]}))
        elif kind == "tool_use":
            events.append(RunEvent("tool_call", {"name": part.get("tool"), "input": (part.get("state") or {}).get("input")}))
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            events.append(RunEvent("usage", {"input_tokens": tokens.get("input", 0),
                                             "output_tokens": tokens.get("output", 0)}))
        elif kind == "error":
            err = obj.get("error") or {}
            events.append(RunEvent("error", {"message": err.get("message") if isinstance(err, dict) else str(err)}))
        return events
