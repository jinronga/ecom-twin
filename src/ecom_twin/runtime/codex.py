"""Codex 底座：`codex exec --json`。

Codex 没有追加系统提示词的参数，系统提示词写进工作目录的 AGENTS.md。
沙箱固定为只读，审批策略为 never（需要提权的操作直接失败，不会自动放行）。

配置隔离：codex 默认读本人的 ~/.codex，里面装的插件（Computer Use、浏览器、ChatGPT 应用连接器）会原样
带进任务，绕开我们的工具白名单。所以每次都用一份只属于 ecom-twin 的 CODEX_HOME：只链接登录凭证、只抄模型
设置；另外在命令行上显式关掉这几项默认开着的内置功能。要操作电脑，走我们自己挂的 computer MCP（见 computer_use.py）。
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR
from .base import AgentRuntime, RunEvent, RunSpec

ISOLATED_HOME = DATA_DIR / "runtime" / "codex-home"
# 本人配置里只抄推理强度和自定义提供方；插件、MCP、应用连接器、notify 一概不抄。
# model 不抄：ChatGPT 桌面端会往 ~/.codex 写只有它自带 CLI 才认的模型名（实测 gpt-6.1-sol 在 Homebrew 的
# codex 0.156 下报 model_not_found），照抄会让任务直接失败。要指定模型用 ecom-twin 的运行时配置。
# 只有配了自定义提供方时才一起抄 model —— 那种情况下模型名本来就是提供方的。
_COPIED_KEYS = ("model_reasoning_effort", "model_provider")
# 默认开着、会让 agent 直接操作电脑 / 浏览器 / 外部应用的内置功能
# shell_tool / unified_exec：Codex 的只读沙箱仍允许读整个磁盘，有 shell 就能 cat 数据目录的 .env 拿到妙手密钥
# （2026-10-29 发现）。agent 只该用 ecom-twin 的 MCP 工具，和 Claude Code 禁用 Bash 对齐
_DISABLED_FEATURES = ("computer_use", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                      "apps", "js_repl", "shell_tool", "unified_exec")


def _source_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))


def prepare_isolated_home(home: Path = ISOLATED_HOME, source: Path | None = None) -> Path:
    """准备隔离的 CODEX_HOME：auth.json 链接到本人的（token 刷新两边同步），config.toml 只留模型设置。"""
    source = source or _source_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    auth, link = source / "auth.json", home / "auth.json"
    if auth.exists() and not (link.is_symlink() and link.resolve() == auth.resolve()):
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(auth)
    raw = {}
    if (source / "config.toml").exists():
        try:
            raw = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            raw = {}
    lines = ["# ecom-twin 自动生成，每次运行前覆盖。只保留模型设置；插件和 MCP 由 ecom-twin 按任务挂载"]
    keys = _COPIED_KEYS + (("model",) if raw.get("model_provider") else ())
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    for name, provider in (raw.get("model_providers") or {}).items():
        lines.append(f"\n[model_providers.{json.dumps(name)}]")
        lines += [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in provider.items()
                  if isinstance(v, (str, int, float, bool))]
    text = "\n".join(lines) + "\n"
    config = home / "config.toml"
    if not config.exists() or config.read_text(encoding="utf-8") != text:
        config.write_text(text, encoding="utf-8")
    return home


class CodexRuntime(AgentRuntime):
    name = "codex"
    binary = "codex"

    def __init__(self, binary: str | None = None, home: Path | None = None):
        super().__init__(binary)
        self.home = home or ISOLATED_HOME

    def prepare_workdir(self, spec: RunSpec) -> None:
        super().prepare_workdir(spec)
        note = ("\n\n## 本底座的限制\nCodex 的沙箱会拦掉 AppleScript 发给其它软件的指令（报错 -10827），"
                "上面「用 AppleScript 操作电脑」那一节在本底座不可用：需要操作电脑时用 computer 工具（如果有），"
                "否则在汇报里写明卡点。\n") if spec.shell_commands else ""
        (spec.workdir / "AGENTS.md").write_text(spec.system_prompt + note, encoding="utf-8")
        prepare_isolated_home(self.home)

    def runtime_env(self, env: dict[str, str]) -> dict[str, str]:
        return {**env, "CODEX_HOME": str(self.home)}

    def build_command(self, spec: RunSpec, resume_id: str | None = None) -> list[str]:
        cmd = [self.binary, "exec"]
        cmd += ["--json", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(spec.workdir),
                "-c", 'approval_policy="never"']
        for feature in _DISABLED_FEATURES:
            cmd += ["-c", f"features.{feature}=false"]
        allowed = spec.tools_by_server()
        for name, server in spec.mcp_servers.items():
            cmd += ["-c", f"mcp_servers.{name}.command={json.dumps(server['command'])}",
                    "-c", f"mcp_servers.{name}.args={json.dumps(server.get('args', []))}"]
            # 只开放白名单里的工具（Codex 的 enabled_tools），没有白名单的 server 一个工具都不给
            cmd += ["-c", f"mcp_servers.{name}.enabled_tools={json.dumps(allowed.get(name, []))}"]
            # enabled_tools 只控制可见性，不代表批准调用。无头运行不能弹出审批；仅对本轮白名单工具
            # 显式批准，平台写操作仍由 ecom-twin 的提案/执行器审批，浏览器与电脑代理继续检查权限。
            for tool in allowed.get(name, []):
                # Tool names come from the MCP protocol and use identifier characters only. Do not
                # JSON-quote this TOML dotted-key segment: Codex treats a quoted segment as a
                # literal key and falls back to its default approval mode.
                cmd += ["-c", f'mcp_servers.{name}.tools.{tool}.approval_mode="approve"']
        if spec.model:
            cmd += ["--model", spec.model]
        # --sandbox / -C belong to exec, not the resume subcommand.
        if resume_id:
            cmd += ["resume", resume_id]
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
