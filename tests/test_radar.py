"""选品雷达：cron、规则校验、到期判断、信号检测和去重、安静结束、有信号才叫醒 agent、线索池。"""

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ecom_twin.memory import MemoryError, MemoryStore
from ecom_twin.mcp_server import ToolServer
from ecom_twin.models import TaskSource
from ecom_twin.radar import (Cron, RadarError, describe_schedule, is_due, load_rules, parse_radar, render_rule,
                             request_trigger, run_due, run_rule)

SEED = Path(__file__).resolve().parents[1] / "memory"
DESC = "选品雷达规则：每天盯沙特斋月装饰和礼拜毯的下拉词，有新方向、节日窗口、痛点词就出线索给负责人看"
UTC = timezone.utc


def body(toml: str) -> str:
    return f"## 规则\n\n```toml\n{toml}\n```\n\n## 关注说明\n\n重点看儿童款和便携款\n"


BASIC = 'schedule = "0 9 * * *"\ntimezone = "Asia/Riyadh"\nregions = ["SA"]\nwatch = ["prayer mat"]'


# ---- cron ----
def test_cron_next_after_and_description():
    riyadh = ZoneInfo("Asia/Riyadh")
    c = Cron.parse("0 9 * * *")
    # 利雅得 09:00 = UTC 06:00
    assert c.next_after(datetime(2026, 11, 19, 5, 0, tzinfo=UTC), riyadh) == datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    assert c.next_after(datetime(2026, 11, 19, 6, 0, tzinfo=UTC), riyadh) == datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
    monday = Cron.parse("30 8 * * 1")
    assert monday.next_after(datetime(2026, 11, 19, tzinfo=UTC), ZoneInfo("UTC")) == datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    assert describe_schedule("0 9 * * *") == "每天 09:00"
    assert describe_schedule("0 9 * * 1,4") == "每周一、周四 09:00"
    assert describe_schedule("0 8 1 * *") == "每月 1 日 08:00"
    assert describe_schedule("15 */6 * * *") == "每 6 小时（第 15 分）"
    for bad in ("0 9 * *", "61 9 * * *", "0 9 * * x"):
        with pytest.raises(RadarError):
            Cron.parse(bad)


# ---- 规则 ----
def test_rule_validation_and_render():
    rule = parse_radar("r", body(BASIC + '\nmodifiers = ["for kids"]\nexpires_at = "2027-03-31"'))
    assert rule.regions == ["SA"] and rule.expires_at == date(2027, 3, 31) and "儿童款" in rule.instruction
    for toml, words in [(BASIC + "\nfoo = 1", "不认识"), (BASIC.replace('"Asia/Riyadh"', '"Mars/Base"'), "timezone"),
                        (BASIC.replace('["prayer mat"]', "[]"), "watch 必填"), (BASIC.replace('"0 9 * * *"', '"每天"'), "5 段"),
                        (BASIC + "\nsignals = { hype = 1 }", "signals 只能")]:
        with pytest.raises(RadarError, match=words):
            parse_radar("r", body(toml))
    text = render_rule("r", "0 9 * * 1", "Asia/Riyadh", ["SA", "AE"], ["prayer mat"], "noon", ["for kids"], 2,
                       "2027-03-31", "看儿童款")
    rule = parse_radar("r", text)
    assert rule.platform == "noon" and rule.max_leads == 2 and "每周一 09:00" in text


def test_memory_validates_radar_type(env, tmp_path):
    mem = MemoryStore(env.app.store, tmp_path)
    with pytest.raises(MemoryError, match="雷达规则有问题"):
        mem.save("雷达-坏", DESC, "radar", body(BASIC.replace('["SA"]', '["SAU"]')))
    mem.save("雷达-沙特", DESC, "radar", body(BASIC))
    rules, errors = load_rules(mem.all())
    assert [r.name for r in rules] == ["雷达-沙特"] and not errors


def test_seed_radar_example_is_valid_and_disabled():
    text = (SEED / "radar-example.md").read_text(encoding="utf-8")
    rule = parse_radar("示例", text)
    assert rule.enabled is False


# ---- 到期 ----
def test_due_from_first_seen_then_from_last_run(env):
    rule = parse_radar("r", body(BASIC))
    t0 = datetime(2026, 11, 19, 5, 0, tzinfo=UTC)
    assert not is_due(rule, env.app.store, t0)                                   # 第一次看到：从现在往后找
    assert is_due(rule, env.app.store, datetime(2026, 11, 19, 6, 0, tzinfo=UTC))
    env.app.store.add_radar_run("r", "定时", "安静", "", [], [], started_at=datetime(2026, 11, 19, 6, 0, tzinfo=UTC))
    assert not is_due(rule, env.app.store, datetime(2026, 11, 19, 12, 0, tzinfo=UTC))
    assert is_due(rule, env.app.store, datetime(2026, 11, 20, 6, 1, tzinfo=UTC))
    paused = parse_radar("r", body(BASIC + "\nenabled = false"))
    assert not is_due(paused, env.app.store, datetime(2026, 11, 21, 6, 1, tzinfo=UTC))
    expired = parse_radar("r", body(BASIC + '\nexpires_at = "2026-11-20"'))
    assert not is_due(expired, env.app.store, datetime(2026, 11, 22, 6, 1, tzinfo=UTC))


# ---- 跑一次 ----
def seeded_memory(env, tmp_path) -> MemoryStore:
    for p in SEED.glob("selection-*.md"):
        shutil.copy(p, tmp_path / p.name)
    mem = MemoryStore(env.app.store, tmp_path)
    mem.save("雷达-沙特", DESC, "radar", body(BASIC + '\nmodifiers = ["for kids"]\nsignals = { new_terms_min = 2, rising_runs = 2 }'))
    return mem


def fake_research(results):
    """按顺序返回预设的快照（含 new_terms、new_by_group），记录调用参数。"""
    calls = []

    def research(store, pb, region, platform, task_id, **kw):
        calls.append({"region": region, "playbook": pb.name, **kw})
        spec = results.pop(0)
        data = {"google_suggest": {"status": "ok", "items": {"en:prayer mat": ["prayer mat"]}},
                "holidays": {"status": "ok", "items": spec.get("holidays", [])}}
        sid = store.add_research_snapshot(region, platform, pb.name, pb.version, task_id, data, [], spec.get("new", []))
        snap = store.get_research_snapshot(sid)
        snap["new_by_group"] = spec.get("groups", {})
        return snap
    research.calls = calls
    return research


def test_quiet_run_then_signals_wake_agent_once(env, tmp_path):
    mem = seeded_memory(env, tmp_path)
    rule = load_rules(mem.all())[0][0]
    submitted = []
    submit = lambda tid, prompt: submitted.append((tid, prompt))  # noqa: E731
    now = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    research = fake_research([
        {"new": ["prayer mat x"], "groups": {"en:prayer mat": ["prayer mat x"]}},            # 1 个新词，没到门槛
        {"new": ["prayer mat for kids", "best prayer mat for elderly"],                        # 2 个新词 + 连续 2 次 + 痛点
         "groups": {"en:prayer mat": ["prayer mat for kids", "best prayer mat for elderly"]},
         "holidays": [{"id": "ramadan", "name": "斋月", "date": "2027-02-08", "days_left": 50, "seeds": []}]},
    ])
    quiet = run_rule(env.app, mem, rule, submit, now=now, research=research)
    assert quiet["status"] == "安静" and submitted == [] and "没有新信号" in quiet["note"]
    assert "prayer mat for kids" in research.calls[0]["extra_seeds"]                       # 品类词 × 修饰词
    loud = run_rule(env.app, mem, rule, submit, now=now + timedelta(days=1), research=research)
    types = {s["type"] for s in loud["signals"]}
    assert types == {"新词", "连续冒新词", "节日备货窗口", "痛点词"}
    assert len(submitted) == 1 and "radar_lead_submit" in submitted[0][1] and "儿童款" in submitted[0][1]
    task = env.app.store.get_task(loud["task_id"])
    assert task.scene == "选品" and task.source == TaskSource.SCHEDULE
    runs = env.app.store.list_radar_runs("雷达-沙特")
    assert [r["status"] for r in runs] == ["有信号", "安静"] and runs[0]["task_id"] == task.id


def test_holiday_and_pain_signals_report_once(env, tmp_path):
    mem = seeded_memory(env, tmp_path)
    rule = load_rules(mem.all())[0][0]
    hol = [{"id": "ramadan", "name": "斋月", "date": "2027-02-08", "days_left": 50, "seeds": []}]
    research = fake_research([{"holidays": hol, "new": ["best prayer mat"]}, {"holidays": hol, "new": ["best prayer mat"]}])
    now = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    first = run_rule(env.app, mem, rule, None, now=now, research=research)
    second = run_rule(env.app, mem, rule, None, now=now, research=research)
    assert {s["type"] for s in first["signals"]} == {"节日备货窗口", "痛点词"}
    assert second["status"] == "安静"                                                     # 同一节日、同一痛点词不重复报


def test_run_due_handles_manual_trigger(env, tmp_path):
    mem = seeded_memory(env, tmp_path)
    research = fake_research([{"new": []}])
    now = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
    assert run_due(env.app, mem, None, now, research=research) == []                    # 还没到 9 点
    request_trigger(env.app.store, "雷达-沙特")
    done = run_due(env.app, mem, None, now, research=research)
    assert len(done) == 1 and env.app.store.list_radar_runs()[0]["trigger"] == "手动"
    assert run_due(env.app, mem, None, now, research=research) == []                    # 触发标记用掉了


# ---- 线索 ----
def test_lead_submit_dedupes_and_status_rules(env, tmp_path):
    task = env.app.tasks.create("选品雷达", TaskSource.SCHEDULE, "选品")
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    text, err = server.call("radar_lead_submit", {"region": "sa", "title": "儿童礼拜毯", "evidence": "R-9 里 prayer mat for kids",
                                                  "snapshot_id": "R-9"})
    assert err and "不存在" in text
    sid = env.app.store.add_research_snapshot("SA", "", "p", "v", "", {}, [], [])
    args = {"rule": "雷达-沙特", "region": "sa", "title": "儿童礼拜毯", "evidence": f"{sid}：prayer mat for kids", "snapshot_id": sid}
    first = json.loads(server.call("radar_lead_submit", args)[0])["lead_id"]
    again = json.loads(server.call("radar_lead_submit", args)[0])["lead_id"]
    assert first == again and env.app.store.get_lead(first)["region"] == "SA"
    with pytest.raises(ValueError, match="要写原因"):
        env.app.store.update_lead(first, "放弃")
    assert env.app.store.update_lead(first, "放弃", "季节性太强")["status"] == "放弃"
    assert json.loads(server.call("radar_lead_submit", args)[0])["lead_id"] != first          # 放弃后同标题可以重新出


# ---- 控制台 ----
def test_console_radar_create_trigger_toggle_and_leads(env, tmp_path):
    from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
    mem = seeded_memory(env, tmp_path)
    app = ConsoleApp(env.app.store, mem, tmp_path / "settings.toml")
    status, d = app.route("POST", "/api/radar", {}, {"name": "雷达-美国眼罩", "regions": ["us"], "watch": ["heated eye mask"],
                                                     "modifiers": ["for kids"], "schedule": "0 9 * * 1", "timezone": "Asia/Shanghai",
                                                     "max_leads": 2, "note": "看人群"})
    assert status == 200
    status, bad = app.route("POST", "/api/radar", {}, {"name": "雷达-坏", "regions": ["US"], "watch": ["x"], "schedule": "每天"})
    assert status == 400 and "5 段" in bad["error"]
    view = app.radar_view()
    rule = next(r for r in view["rules"] if r["name"] == "雷达-美国眼罩")
    assert rule["describe"] == "每周一 09:00" and rule["next_run"] and view["serve_alive"] is False
    status, x = app.route("POST", f"/api/radar/{rule['slug']}/trigger", {}, {})
    assert status == 200 and "没在运行" in x["note"] and env.app.store.kv_get("radar:trigger:雷达-美国眼罩")
    env.app.store.kv_set("serve:heartbeat", datetime.now(UTC).isoformat())
    assert app.radar_view()["serve_alive"] is True
    app.route("POST", f"/api/radar/{rule['slug']}/toggle", {}, {"enabled": False})
    assert next(r for r in app.radar_view()["rules"] if r["name"] == "雷达-美国眼罩")["enabled"] is False
    # 线索：放弃要原因，转选品建任务并排进队列
    lid = env.app.store.add_lead({"rule": "雷达-美国眼罩", "region": "US", "title": "儿童加热眼罩", "evidence": "R-1"})
    status, err = app.route("POST", f"/api/leads/{lid}", {}, {"status": "放弃"})
    assert status == 400 and "原因" in err["error"]
    status, conv = app.route("POST", f"/api/leads/{lid}/convert", {}, {})
    assert status == 200 and env.app.store.get_task_queue(conv["task_id"])["state"] == "queued"
    assert env.app.store.get_lead(lid)["status"] == "已转选品" and env.app.store.get_task(conv["task_id"]).scene == "选品"
    assert app.route("POST", f"/api/leads/{lid}/convert", {}, {})[0] == 400                 # 不重复转
    # 权限：运营能处理线索、转选品；建规则、触发、暂停要负责人
    assert allowed(ROLE_REVIEWER, "POST", f"/api/leads/{lid}") and allowed(ROLE_REVIEWER, "POST", f"/api/leads/{lid}/convert")
    assert not allowed(ROLE_REVIEWER, "POST", "/api/radar")
    assert not allowed(ROLE_REVIEWER, "POST", f"/api/radar/{rule['slug']}/trigger")
