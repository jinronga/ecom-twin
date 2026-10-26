from datetime import timedelta
from pathlib import Path

from conftest import NOW
from ecom_twin.models import StepStatus, TaskStatus
from ecom_twin.runtime import ClaudeCodeRuntime, CodexRuntime, RunSpec


def test_board_coalesces_updates_and_then_updates_same_record(env):
    tasks = env.app.tasks
    tasks.set_status(env.task.id, TaskStatus.RUNNING)
    tasks.set_status(env.task.id, TaskStatus.WAITING, blocker="等运营确认主图")
    assert env.app.board.flush(now=NOW, seconds=10) == 1  # 三次变化合并成一次写入
    record = next(r for r in env.feishu.records.values() if r["table"] == "tbl_tasks")
    assert record["状态"] == "等待回复" and record["卡点"] == "等运营确认主图"

    tasks.set_status(env.task.id, TaskStatus.CLOSED)
    env.app.board.flush(now=NOW, seconds=10)
    assert len([r for r in env.feishu.records.values() if r["table"] == "tbl_tasks"]) == 1
    assert record["状态"] == "已关闭"


def test_board_failure_backs_off_without_losing_update(env):
    env.feishu.fail_base = True
    assert env.app.board.flush(now=NOW) == 0
    env.feishu.fail_base = False
    assert env.app.board.flush(now=NOW) == 0  # 还在退避期
    assert env.app.board.flush(now=NOW + timedelta(seconds=5)) == 1


def test_steps_update_current_step_and_board(env):
    step = env.app.tasks.start_step(env.task.id, "拉取采集箱数据")
    assert env.app.store.get_task(env.task.id).current_step == "拉取采集箱数据"
    env.app.tasks.finish_step(step, StepStatus.SUCCESS, summary="2 款商品")
    env.app.board.flush(now=NOW, seconds=10)
    assert any(r.get("步骤名") == "拉取采集箱数据" and r["状态"] == "成功" for r in env.feishu.records.values())


def spec(tmp_path: Path) -> RunSpec:
    return RunSpec(prompt="处理任务 T-0001", system_prompt="你是运营分身", workdir=tmp_path,
                   mcp_servers={"ecom": {"command": "python", "args": ["-m", "ecom_twin.mcp"]}},
                   allowed_tools=["mcp__ecom__list_shops"])


def test_claude_command_restricts_tools(tmp_path):
    rt = ClaudeCodeRuntime()
    s = spec(tmp_path)
    rt.prepare_workdir(s)
    cmd = rt.build_command(s, resume_id="sess-1")
    assert "--strict-mcp-config" in cmd and "Bash" in cmd[cmd.index("--disallowedTools") + 1]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__ecom__list_shops"
    assert cmd[-2:] == ["--resume", "sess-1"]
    assert (tmp_path / "mcp.json").exists()


def test_claude_events_are_normalized():
    rt = ClaudeCodeRuntime()
    assert rt.parse_line({"type": "system", "subtype": "init", "session_id": "s1"})[0].type == "session"
    events = rt.parse_line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "开始"}, {"type": "tool_use", "name": "mcp__ecom__list_shops", "input": {}}]}})
    assert [e.type for e in events] == ["message", "tool_call"]
    events = rt.parse_line({"type": "result", "is_error": False, "session_id": "s1", "result": "完成",
                            "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01})
    assert [e.type for e in events] == ["usage", "done"] and events[1].data["ok"]


def test_codex_command_is_read_only_and_writes_agents_md(tmp_path):
    rt = CodexRuntime()
    s = spec(tmp_path)
    rt.prepare_workdir(s)
    cmd = rt.build_command(s)
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in cmd
    assert (tmp_path / "AGENTS.md").read_text() == "你是运营分身"
    assert rt.build_command(s, resume_id="th-1")[:4] == ["codex", "exec", "resume", "th-1"]


def test_codex_events_are_normalized():
    rt = CodexRuntime()
    assert rt.parse_line({"type": "thread.started", "thread_id": "th-1"})[0].data["session_id"] == "th-1"
    assert rt.parse_line({"type": "item.completed", "item": {"type": "agent_message", "text": "好"}})[0].type == "message"
    assert [e.type for e in rt.parse_line({"type": "turn.failed", "error": {"message": "quota"}})] == ["error", "done"]


def test_cursor_agent_writes_config_and_denies_shell(tmp_path):
    from ecom_twin.runtime import CursorAgentRuntime
    import json as _json
    rt = CursorAgentRuntime()
    s = spec(tmp_path)
    rt.prepare_workdir(s)
    perms = _json.loads((tmp_path / ".cursor" / "cli.json").read_text())["permissions"]
    assert "Shell(*)" in perms["deny"] and perms["allow"] == ["Mcp(ecom:*)"]
    cmd = rt.build_command(s, resume_id="c1")
    assert "--force" not in cmd and "--yolo" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "c1" and cmd[-1] == s.prompt
    ev = rt.parse_line({"type": "tool_call", "subtype": "started", "tool_call": {"mcpToolCall": {"name": "x"}}})
    assert ev[0].type == "tool_call"


def test_opencode_writes_config_and_parses_events(tmp_path):
    from ecom_twin.runtime import OpenCodeRuntime
    import json as _json
    rt = OpenCodeRuntime()
    s = spec(tmp_path)
    rt.prepare_workdir(s)
    cfg = _json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["permission"]["bash"] == "deny" and cfg["mcp"]["ecom"]["command"][0] == "python"
    assert rt.build_command(s, resume_id="ses1")[:6] == ["opencode", "run", "--format", "json", "--dir", str(tmp_path)]
    events = rt.parse_line({"type": "text", "sessionID": "ses1", "part": {"text": "好"}})
    assert [e.type for e in events] == ["session", "message"]
    usage = rt.parse_line({"type": "step_finish", "sessionID": "ses1", "part": {"tokens": {"input": 5, "output": 2}}})
    assert usage[0].data == {"input_tokens": 5, "output_tokens": 2}


def test_console_feishu_prints_to_stderr(capsys):
    from ecom_twin.feishu import ConsoleFeishu
    f = ConsoleFeishu()
    mid = f.send_text("ou_boss", "你好")
    assert mid.startswith("om_console_") and "你好" in capsys.readouterr().err
