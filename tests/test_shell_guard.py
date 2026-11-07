"""AppleScript 方式的命令闸门：do shell script 这类绕过白名单的写法、禁止目标，在执行前拦下。

2026-11-06 真实 Claude Code 实测：模型照指令去跑 osascript -e 'do shell script "echo ..."'，
PreToolUse hook 拦下，命令没执行，原因交给了模型，审计里有 shell_blocked。
"""

import io
import json
import sys

from ecom_twin.runtime import ClaudeCodeRuntime
from ecom_twin.runtime.base import RunSpec
from ecom_twin.shell_guard import check, main

BLOCKED = ["sellercentral", "keychain", "1password"]


def test_escape_hatches_are_blocked():
    for cmd in ["osascript -e 'do shell script \"cat ~/.env\"'",
                "osascript -e 'DO   SHELL\nSCRIPT \"id\"'",
                "osascript -l JavaScript -e 'Application.currentApplication().doShellScript(\"id\")'",
                "osascript -e 'run script (\"do shell \" & \"script \\\"id\\\"\")'",
                "osascript -l JavaScript -e 'ObjC.import(\"Foundation\"); $.NSTask.alloc'",
                "osascript -e \"use framework \\\"Foundation\\\"\" -e \"current application's NSTask\""]:
        assert check(cmd, BLOCKED), cmd


def test_normal_ui_scripting_and_blocked_targets():
    assert check("osascript -e 'tell application \"System Events\" to tell process \"Finder\" to get name of window 1'",
                 BLOCKED) is None
    assert check("open -g -a Calculator", BLOCKED) is None
    assert "sellercentral" in check("open -a Safari https://sellercentral.amazon.com", BLOCKED)
    assert "keychain" in check("osascript -e 'tell application \"Keychain Access\" to activate'", BLOCKED)


def test_hook_exit_code_and_audit(tmp_path, monkeypatch):
    db = tmp_path / "guard.db"
    cfg = tmp_path / "settings.toml"
    cfg.write_text(f'[storage]\ndb_path = "{db}"\n', encoding="utf-8")
    event = {"tool_name": "Bash", "tool_input": {"command": "osascript -e 'do shell script \"id\"'"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert main(["--task", "T-7", "--config", str(cfg)]) == 2              # 2 = Claude Code 不执行这条命令
    from ecom_twin.store import Store
    assert "shell_blocked" in Store(db).audit_events("T-7")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "open -a Notes"}})))
    assert main(["--config", str(cfg)]) == 0


def test_claude_code_gets_the_hook_only_with_shell(tmp_path):
    guard = [sys.executable, "-m", "ecom_twin.shell_guard", "--task", "T-1"]
    spec = RunSpec(prompt="p", system_prompt="s", workdir=tmp_path, shell_commands=["osascript"], shell_guard=guard)
    rt = ClaudeCodeRuntime()
    rt.prepare_workdir(spec)
    cmd = rt.build_command(spec)
    settings = json.loads((tmp_path / "settings.json").read_text())
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash" and "ecom_twin.shell_guard" in hook["hooks"][0]["command"]
    assert cmd[cmd.index("--settings") + 1] == str(tmp_path / "settings.json")
    plain = RunSpec(prompt="p", system_prompt="s", workdir=tmp_path / "x")
    assert "--settings" not in rt.build_command(plain)
