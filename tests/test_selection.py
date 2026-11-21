"""选品剧本：规则校验、按站点匹配、代码打分、随记忆维护和自动采纳。"""

import json
import shutil
from pathlib import Path

import pytest

from ecom_twin.mcp_server import ToolServer
from ecom_twin.memory import MemoryError, MemoryStore
from ecom_twin.models import TaskSource
from ecom_twin.numbers import margin_at_price
from ecom_twin.selection import (PlaybookError, load_playbooks, match_playbook, parse_rules, score_candidate,
                                 score_candidates)
from ecom_twin.tasks import PENDING_SCENE

SEED = Path(__file__).resolve().parents[1] / "memory"
DESC = "给测试国家选品时打开：调研步骤、筛选门槛、打分权重和这个市场的选品经验，按站点细化的剧本"


def rules_body(toml: str, extra: str = "") -> str:
    return f"## 规则\n\n```toml\n{toml}\n```\n\n## 重点方向\n\n- 暂无\n{extra}"


def seeded(tmp_path, env) -> MemoryStore:
    for name in sorted(p.name for p in SEED.glob("selection-*.md")):
        shutil.copy(SEED / name, tmp_path / name)
    return MemoryStore(env.app.store, tmp_path)


# ---- 规则校验 ----
SEED_PLAYBOOKS = sorted(p.name for p in SEED.glob("selection-*.md"))


def test_seed_playbooks_parse():
    assert {"selection-default.md", "selection-us.md", "selection-europe.md", "selection-southeast-asia.md",
            "selection-middle-east.md"} <= set(SEED_PLAYBOOKS)
    for name in SEED_PLAYBOOKS:
        text = (SEED / name).read_text(encoding="utf-8")
        rules = parse_rules(text)
        assert rules.regions and rules.auto_adopt_sections == ["重点方向"]


@pytest.mark.parametrize("toml, words", [
    ('regions = ["US"]\nfoo = 1', "不认识的规则项"),
    ('regions = ["USA"]', "不是两位国家代码"),
    ('regions = []', "regions 必填"),
    ('regions = ["US"]\nsources = ["tiktok_spider"]', "没有登记"),
    ('regions = ["US"]\nprice_band = [50, 10]', "price_band"),
    ('regions = ["US"]\nmin_margin = 1.5', "超出范围"),
    ('regions = ["US"]\nweights = { demand = 0, margin = 0 }', "不能全是 0"),
    ('regions = ["US"]\nweights = { hype = 10 }', "不认识"),
    ('regions = ["US"]\nauto_adopt_sections = ["规则"]', "不能自动采纳"),
    ('regions = ["US"]\nmin_margin = 0.3\nmargin_good = 0.2', "要大于 min_margin"),
    ('regions = ["US"\n', "不是合法的 toml"),
])
def test_bad_rules_are_explained(toml, words):
    with pytest.raises(PlaybookError, match=words):
        parse_rules(rules_body(toml))


def test_missing_rules_section():
    with pytest.raises(PlaybookError, match="要有「## 规则」"):
        parse_rules("## 重点方向\n- 无")


# ---- 和记忆一起维护 ----
def test_memory_save_rejects_bad_playbook_and_accepts_good(env, tmp_path):
    mem = MemoryStore(env.app.store, tmp_path)
    with pytest.raises(MemoryError, match="选品剧本的规则有问题"):
        mem.save("选品剧本-测试", DESC, "selection", rules_body('regions = ["XYZ"]'))
    saved = mem.save("选品剧本-测试", DESC, "selection", rules_body('regions = ["US"]'))
    assert saved.type == "selection"
    mem.save("普通经验", DESC, "experience", "## 规则\n随便写，不是剧本就不校验")       # 别的类型不受影响


def test_check_reports_hand_edited_broken_playbook(env, tmp_path):
    mem = MemoryStore(env.app.store, tmp_path)
    (tmp_path / "bad.md").write_text(f"---\nname: 坏剧本\ndescription: {DESC}\ntype: selection\n---\n"
                                     + rules_body('regions = ["US"]\nsources = ["nope"]'), encoding="utf-8")
    assert any("坏剧本" in text and "没有登记" in text for level, text in mem.check() if level == "error")


# ---- 匹配 ----
def test_match_prefers_specific_region_and_platform(env, tmp_path):
    playbooks, errors = load_playbooks(seeded(tmp_path, env).all())
    assert not errors
    assert match_playbook(playbooks, "SA", "noon").name == "选品剧本-中东"
    assert match_playbook(playbooks, "sa").name == "选品剧本-中东"             # 不给平台也能匹配，大小写不敏感
    assert match_playbook(playbooks, "US", "amazon").name == "选品剧本-美国"
    assert match_playbook(playbooks, "DE").name == "选品剧本-欧洲"
    assert match_playbook(playbooks, "TH", "shopee").name == "选品剧本-东南亚"
    assert match_playbook(playbooks, "CA", "tiktok").name == "选品剧本-默认"    # 没有专门剧本就用兜底
    assert match_playbook(playbooks, "SA", "amazon").name == "选品剧本-默认"    # 中东剧本只写了 noon、tiktok


def test_match_errors_when_none_or_ambiguous(env, tmp_path):
    mem = MemoryStore(env.app.store, tmp_path)
    mem.save("剧本A", DESC, "selection", rules_body('regions = ["US"]'))
    with pytest.raises(PlaybookError, match="没有适用于 JP"):
        match_playbook(load_playbooks(mem.all())[0], "JP")
    mem.save("剧本B", DESC, "selection", rules_body('regions = ["US"]'))
    with pytest.raises(PlaybookError, match="分不出用哪份"):
        match_playbook(load_playbooks(mem.all())[0], "US")


# ---- 打分 ----
def test_margin_at_price_numbers():
    r = margin_at_price(30, 70, 0.14, 4, commission_rate=0.1)
    assert r["landed_cost"] == 13.8 and r["fees"] == 3.0 and r["profit"] == 13.2 and r["margin_rate"] == 0.44


def test_score_applies_rules_and_global_floor():
    rules = parse_rules(rules_body('regions = ["SA"]\nprice_band = [15, 60]\nmax_weight_kg = 2\nmin_margin = 0.2\n'
                                   'exclude_categories = ["酒"]'))
    good = {"name": "斋月灯串", "category": "家居装饰", "price": 30, "source_price": 70, "exchange_rate": 0.14,
            "shipping": 4, "commission_rate": 0.1, "weight_kg": 0.5, "demand": 80, "competition": 40}
    r = score_candidate(good, rules, global_min_margin=0.15)
    assert r["passed"] and r["min_margin"] == 0.2 and r["margin"]["margin_rate"] == 0.44 and r["score"] > 60
    # 全局底线更严时，以全局为准（剧本只能更严，不能更松）
    assert score_candidate(good, rules, global_min_margin=0.5)["passed"] is False
    bad = {**good, "name": "红酒杯", "category": "酒具", "price": 80, "weight_kg": 3}
    reasons = score_candidate(bad, rules)["reasons"]
    assert any("价格带" in x for x in reasons) and any("重量" in x for x in reasons) and any("排除项" in x for x in reasons)
    missing = score_candidate({k: v for k, v in good.items() if k != "demand"}, rules)
    assert missing["missing"] == ["demand"] and missing["scores"]["demand"] == 0
    ranked = score_candidates([bad, good], rules)
    assert [x["name"] for x in ranked] == ["斋月灯串", "红酒杯"]


# ---- 自动采纳 ----
def test_drafts_on_allowed_section_auto_adopt_rules_need_a_human(env, tmp_path):
    mem = seeded(tmp_path, env)
    auto = mem.propose({"mode": "append", "target_name": "选品剧本-中东", "body": "## 重点方向\n- 斋月灯饰下拉词连续三周上涨",
                        "why": "调研发现新方向", "evidence": "11-18 SA 下拉词快照"})
    assert env.app.store.get_memory_draft(auto)["status"] == "已采纳"
    assert "斋月灯饰下拉词连续三周上涨" in mem.by_name("选品剧本-中东").body
    assert mem.history("selection-middle-east")                                  # 自动采纳也留了历史版本
    manual = mem.propose({"mode": "revise", "target_name": "选品剧本-中东", "target_section": "规则",
                          "old_text": "min_margin = 0.30", "body": "min_margin = 0.20",
                          "why": "想放宽毛利", "evidence": "无"})
    assert env.app.store.get_memory_draft(manual)["status"] == "待审核"
    assert "min_margin = 0.30" in mem.by_name("选品剧本-中东").body


# ---- agent 工具 ----
def fake_get(url, headers=None, allowed=None):
    if "suggestqueries" in url:
        return json.dumps(["x", ["ramadan lantern", "ramadan lights"]])
    raise AssertionError(f"测试里不该访问 {url}")


def test_tools_research_then_score_and_fill_scene(env, tmp_path, monkeypatch):
    import ecom_twin.research as research
    monkeypatch.setattr(research, "http_get", fake_get)
    monkeypatch.setattr(research.time, "sleep", lambda s: None)
    mem = seeded(tmp_path, env)
    task = env.app.tasks.create("给沙特选几款斋月的品", TaskSource.SELF_DM, PENDING_SCENE)
    server = ToolServer(env.app, mem, env.miaoshou, task.id)
    text, err = server.call("selection_playbook", {"region": "SA", "platform": "noon"})
    pb = json.loads(text)
    assert not err and pb["剧本"] == "选品剧本-中东" and "宗教文化禁忌" in pb["市场经验"]
    assert pb["规则"]["min_margin"] >= env.app.settings.limits.min_margin_rate
    candidate = {"name": "礼拜毯", "category": "家居", "price": 25, "source_price": 40, "exchange_rate": 0.14,
                 "shipping": 3, "commission_rate": 0.1, "weight_kg": 0.8, "demand": 70, "competition": 50}
    text, err = server.call("selection_score", {"region": "SA", "snapshot_id": "R-9999", "candidates": [candidate]})
    assert err and "没有调研快照" in text                                         # 没调研不许出清单
    text, err = server.call("selection_research", {"region": "SA", "platform": "noon", "owner_data": "noon 搜索 prayer mat 有 300+ 结果"})
    snap = json.loads(text)
    assert not err and snap["数据源"]["google_suggest"]["状态"] == "ok" and snap["数据源"]["owner_input"]["状态"] == "ok"
    assert snap["数据源"]["csv_import"]["状态"] == "skipped"
    text, err = server.call("selection_score", {"region": "SA", "snapshot_id": snap["快照"], "candidates": [candidate]})
    scored = json.loads(text)
    assert not err and scored["结果"][0]["passed"] and scored["快照"] == snap["快照"] and scored["版本"] == pb["版本"]
    assert scored["结果"][0]["margin"]["return_rate"] == 0.2                       # 剧本的退货率算进去了
    text, err = server.call("selection_score", {"region": "AE", "snapshot_id": snap["快照"], "candidates": [candidate]})
    assert err and "不是 AE 的" in text
    assert env.app.store.get_task(task.id).scene == "选品"
    text, err = server.call("selection_playbook", {"region": "JP", "platform": "amazon"})
    assert not err and json.loads(text)["剧本"] == "选品剧本-默认"                # 兜底剧本接住
    text, err = server.call("selection_score", {"region": "SA", "snapshot_id": snap["快照"], "candidates": []})
    assert err and "至少一个" in text
    text, err = server.call("selection_research", {"region": "SA", "csv_path": "/etc/passwd"})
    assert err and "本任务目录" in text


# ---- 控制台试算 ----
def test_console_preview_and_reviewer_can_use_it(env, tmp_path):
    from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
    mem = seeded(tmp_path, env)
    app = ConsoleApp(env.app.store, mem, tmp_path / "settings.toml")
    status, data = app.route("POST", "/api/selection-preview", {}, {"region": "AE", "candidate": {
        "name": "乌木香", "category": "香氛", "price": 40, "source_price": 60, "exchange_rate": 0.14, "shipping": 5,
        "commission_rate": 0.1, "weight_kg": 0.3, "demand": 60, "competition": 30}})
    assert status == 200 and data["playbook"] == "选品剧本-中东" and data["result"]["passed"]
    assert allowed(ROLE_REVIEWER, "POST", "/api/selection-preview")
    status, data = app.route("GET", "/api/memories", {}, {})
    assert "selection" in data["types"]
