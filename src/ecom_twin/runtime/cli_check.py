"""本机 agent CLI 的路径配置与验证（DESIGN.md 5.6、12.2 规则配置）。

- 路径以配置为准；配置留空时按 PATH 和常见安装位置自动查找
- 验证方式：运行 `<路径> --version`，成功即「验证通过」，并记录版本号
- 「验证通过」只说明 CLI 能跑；底座是否可被分身调度，还要看对应的运行时适配器是否已实现
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CliSpec:
    label: str
    candidates: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    adapter_ready: bool = False  # 运行时适配器（runtime/*.py）是否已实现


CLI_SPECS: dict[str, CliSpec] = {
    "claude_code": CliSpec("Claude Code", ("claude", "~/.local/bin/claude"), adapter_ready=True),
    "codex": CliSpec("Codex", ("codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex"), adapter_ready=True),
    "cursor": CliSpec("Cursor Agent", ("cursor-agent", "agent", "~/.local/bin/cursor-agent"), adapter_ready=True),
    "opencode": CliSpec("OpenCode", ("opencode", "~/.opencode/bin/opencode"), adapter_ready=True),
}

# 常见误配：版本输出里出现这些内容时给出修正建议
_HINTS = {
    "No Cursor IDE installation found": "这是 Cursor IDE 的启动器，不是 agent CLI；请改填 cursor-agent（或 agent）的路径",
}


@dataclass
class CliStatus:
    key: str
    label: str
    path: str = ""
    source: str = ""  # 配置 / 自动查找 / 未找到
    ok: bool = False
    version: str = ""
    error: str = ""
    hint: str = ""
    adapter_ready: bool = False

    @property
    def usable(self) -> bool:
        """能被分身调度：CLI 验证通过且适配器已实现。"""
        return self.ok and self.adapter_ready


def _find(candidate: str) -> str | None:
    if "/" in candidate or candidate.startswith("~"):
        p = Path(os.path.expanduser(candidate))
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(candidate)


def resolve(key: str, configured: str = "") -> tuple[str, str]:
    """返回 (路径, 来源)。配置了就只认配置，不再自动查找，避免和用户以为的不是同一个。"""
    if configured:
        return os.path.expanduser(configured), "配置"
    for candidate in CLI_SPECS[key].candidates:
        found = _find(candidate)
        if found:
            return found, "自动查找"
    from ..config import PROJECT_ROOT
    binary = {"claude_code": "claude", "codex": "codex", "cursor": "cursor-agent", "opencode": "opencode"}[key]
    found = _find(str(PROJECT_ROOT / "data/tools/node/bin" / binary))
    if found:
        return found, "项目自动安装"
    return "", "未找到"


def check(key: str, configured: str = "", timeout: float = 20) -> CliStatus:
    spec = CLI_SPECS[key]
    status = CliStatus(key=key, label=spec.label, adapter_ready=spec.adapter_ready)
    status.path, status.source = resolve(key, configured)
    if not status.path:
        status.error = "没有配置路径，也没在 PATH 和常见位置找到"
        return status
    if not (Path(status.path).is_file() and os.access(status.path, os.X_OK)):
        status.error = "路径不存在或不可执行"
        return status
    try:
        proc = subprocess.run([status.path, *spec.version_args], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        status.error = f"运行 --version 超过 {timeout:.0f} 秒没有返回"
        return status
    except OSError as e:
        status.error = f"无法运行：{e}"
        return status
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode == 0 and output:
        status.ok = True
        status.version = output.splitlines()[0]
    else:
        status.error = (proc.stderr or proc.stdout or f"退出码 {proc.returncode}").strip()[:500]
        status.hint = next((h for k, h in _HINTS.items() if k in status.error), "")
    return status


def check_all(configured: dict[str, str], timeout: float = 20) -> list[CliStatus]:
    return [check(key, configured.get(key, ""), timeout) for key in CLI_SPECS]
