"""运行观测：心跳、完整记录存档、成本汇总、按站点时区计算每日写次数。"""

import stat
from datetime import datetime, timezone

from conftest import NOW, SHOP
from ecom_twin.executor import check_common_limits, local_day_start
from ecom_twin.memory import MemoryStore
from ecom_twin.runtime import AgentRuntime, RunEvent, RunSpec


class ScriptRuntime(AgentRuntime):
    """真实子进程：运行一个打印 JSON 行的脚本。"""

    def build_command(self, spec, resume_id=None):
        return [self.binary]

    def parse_line(self, obj):
        if obj.get("type") == "usage":
            return [RunEvent("usage", {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02})]
        if obj.get("type") == "msg":
            return [RunEvent("message", {"text": obj["text"]})]
        return []


def make_script(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_transcript_is_archived_verbatim(tmp_path):
    exe = make_script(tmp_path / "agent", "echo '{\"type\":\"msg\",\"text\":\"hi\"}'; echo 'not json'")
    out = tmp_path / "t" / "run.jsonl"
    events = list(ScriptRuntime(exe).run(RunSpec("p", "s", tmp_path / "w", transcript_path=out)))
    assert [e.type for e in events] == ["message", "message", "done"]
    assert out.read_text().splitlines() == ['{"type":"msg","text":"hi"}', "not json"]


def test_dispatcher_records_transcript_usage_and_heartbeat(env, tmp_path):
    from ecom_twin.dispatcher import Dispatcher
    exe = make_script(tmp_path / "agent", "echo '{\"type\":\"usage\"}'; sleep 0.5; echo '{\"type\":\"msg\",\"text\":\"done\"}'")
    rt = ScriptRuntime(exe)
    d = Dispatcher(env.app, MemoryStore(env.app.store, tmp_path), lambda key: rt, tmp_path / "ws",
                   sleep=lambda s: None, heartbeat_sec=0.1)
    before = env.app.store.get_task(env.task.id).last_heartbeat
    result = d.run_task(env.task.id, "做事", resume=False)
    task = env.app.store.get_task(env.task.id)
    assert result.ok and task.transcript.endswith("-claude_code.jsonl")
    assert task.last_heartbeat and task.last_heartbeat != before
    rows = env.app.store.usage_summary(datetime.now(timezone.utc).strftime("%Y-%m"), "runtime")
    assert rows[0]["key"] == "claude_code" and rows[0]["input_tokens"] == 10 and abs(rows[0]["cost_usd"] - 0.02) < 1e-9


def test_usage_summary_by_scene_and_missing_cost(env):
    task = env.app.store.get_task(env.task.id)
    env.app.store.add_usage(task, "codex", 100, 50, None, at=NOW)
    env.app.store.add_usage(task, "claude_code", 10, 5, 0.5, at=NOW)
    by_scene = env.app.store.usage_summary("2026-10", "scene")
    assert by_scene[0]["key"] == "上品" and by_scene[0]["runs_without_cost"] == 1


def test_daily_limit_uses_site_local_day(env):
    tz = {SHOP: "America/Los_Angeles"}
    now = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)          # 洛杉矶 10-16 01:00
    assert local_day_start(now, "America/Los_Angeles").isoformat().startswith("2026-10-16T00:00:00-07:00")
    env.app.settings.limits.max_daily_writes_per_shop = 1
    env.app.store.conn.execute(  # 洛杉矶前一天 23:30 写过一次：不算今天
        "INSERT INTO executions(proposal_id, shop_key, request, created_at) VALUES('P-x', ?, '{}', ?)",
        (SHOP, "2026-10-16T06:30:00+00:00"))
    p = env.submit()
    assert check_common_limits(p, env.app.settings.limits, env.app.store, now, tz) == []
    assert check_common_limits(p, env.app.settings.limits, env.app.store, now) != []   # 按 UTC 算就超了
