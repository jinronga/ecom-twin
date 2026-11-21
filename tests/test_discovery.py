"""找方向：品类库扫描、代码打分排序、排除、节日窗口、存快照、工具和控制台接口。网络请求全部替换成假数据。"""

import json
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ecom_twin.discovery import CATEGORIES, discover
from ecom_twin.mcp_server import ToolServer
from ecom_twin.memory import MemoryStore
from ecom_twin.models import TaskSource
from ecom_twin.selection import Playbook, parse_rules

SEED = Path(__file__).resolve().parents[1] / "memory"
NO_SLEEP = lambda s: None  # noqa: E731


def playbook(toml: str) -> Playbook:
    body = f"## 规则\n\n```toml\n{toml}\n```\n"
    return Playbook("剧本", parse_rules(body), body, "v1")


def fake_get(rich: dict[str, list[str]]):
    """rich 里的品类词返回给定的下拉词，其余品类只返回自己。"""
    calls = []

    def get(url, headers=None, allowed=None):
        calls.append(url)
        q = parse_qs(urlparse(url).query)["q"][0]
        return json.dumps([q, rich.get(q, [q])])
    get.calls = calls
    return get


RICH = {
    "pet toys": ["pet toys", "pet toys for dogs", "pet toys for cats", "pet toys set", "durable pet toys",
                 "best pet toys", "pet toys for puppies", "pet toys bulk", "pet toys for small dogs", "interactive pet toys"],
    "pet toys for": ["pet toys for dogs", "pet toys for cats", "pet toys for aggressive chewers", "pet toys for puppies"],
    "kids toys": ["kids toys", "kids toys for girls", "kids toys for boys", "best kids toys", "kids toys gift", "kids toys set"],
}


def test_discover_ranks_by_code_and_saves_snapshot(env):
    pb = playbook('regions = ["US"]\nholiday_lead_weeks = 10')
    get = fake_get(RICH)
    r = discover(env.app.store, pb, "us", get=get, sleep=NO_SLEEP, today=date(2026, 11, 19))
    top = r["方向"][0]
    assert top["品类词"] == "pet toys" and top["下拉词数"] == 11
    assert top["分项"]["能细分"] == 62.5 and "pet toys for dogs" in top["细分切入点"] and "best pet toys" in top["痛点词"]
    # 剧本要提前 10 周备货：圣诞只剩 36 天来不及，情人节还有 87 天正好赶上
    assert top["节日"].startswith("情人节 2027-02-14") and "正好赶上" in top["节日"] and top["分项"]["节日"] == 100.0
    assert "amazon.com/s?k=pet+toys" in top["核实链接"]["Amazon 搜索结果（看价格带、评价数、卖家多不多）"]
    kids = next(d for d in r["方向"] if d["品类词"] == "kids toys")
    assert "CPC" in kids["提醒"]
    snap = env.app.store.get_research_snapshot(r["快照"])
    assert snap["region"] == "US" and snap["data"]["discovery"]["items"][0]["品类词"] == "pet toys"
    assert len(get.calls) == 2 * len(CATEGORIES)                                # 英语市场：品类词 + 「品类词 for」


def test_discover_excludes_and_adds_categories(env):
    pb = playbook('regions = ["DE"]\nexclude_categories = ["床品"]\nskip_categories = ["钓鱼"]\ncategories = ["hundebett"]')
    r = discover(env.app.store, pb, "DE", exclude=["儿童"], focus=["garden decor"], get=fake_get({}), sleep=NO_SLEEP,
                 today=date(2026, 11, 19), top=60)
    names = {d["方向"] for d in r["方向"]}
    skipped = {x["品类"] for x in r["排除的"]}
    assert {"床品", "钓鱼用品", "儿童玩具", "母婴用品"} <= skipped and not ({"床品", "儿童玩具"} & names)
    full = env.app.store.get_research_snapshot(r["快照"])["data"]["discovery"]["items"]   # 返回最多 20 条，完整排序在快照里
    assert {"hundebett", "garden decor"} <= {d["品类词"] for d in full}
    assert any("主要语言是 de" in n for n in r["说明"])                            # 非英语市场提醒补当地语言的品类词


def test_discover_tool_and_console(env, tmp_path, monkeypatch):
    import ecom_twin.discovery as discovery
    from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
    monkeypatch.setattr(discovery, "http_get", fake_get(RICH))
    monkeypatch.setattr(discovery.time, "sleep", NO_SLEEP)
    for p in SEED.glob("selection-*.md"):
        shutil.copy(p, tmp_path / p.name)
    mem = MemoryStore(env.app.store, tmp_path)
    task = env.app.tasks.create("美国做什么品", TaskSource.CHAT, "待定")
    server = ToolServer(env.app, mem, env.miaoshou, task.id)
    text, err = server.call("selection_discover", {"region": "US", "exclude": ["电子"], "top": 5})
    r = json.loads(text)
    assert not err and r["剧本"] == "选品剧本-美国" and len(r["方向"]) == 5 and "radar_rule_propose" in r["下一步"]
    assert env.app.store.get_task(task.id).scene == "选品"
    app = ConsoleApp(env.app.store, mem, tmp_path / "settings.toml")
    status, c = app.route("POST", "/api/selection-discover", {}, {"region": "US", "exclude": "儿童，床品", "top": 3})
    assert status == 200 and len(c["方向"]) == 3 and {"儿童玩具", "床品"} <= {x["品类"] for x in c["排除的"]}
    assert not allowed(ROLE_REVIEWER, "POST", "/api/selection-discover")          # 会访问外网，只给负责人


def test_seed_playbooks_still_parse_with_new_options():
    for p in SEED.glob("selection-*.md"):
        parse_rules(p.read_text(encoding="utf-8"))


def test_holiday_timing_late_and_early(env):
    from ecom_twin.discovery import discover
    pb = playbook('regions = ["US"]\nholiday_lead_weeks = 10')
    late = discover(env.app.store, pb, "US", get=fake_get(RICH), sleep=NO_SLEEP, today=date(2027, 1, 20), top=40)
    toy = next(d for d in late["方向"] if d["品类词"] == "pet toys")
    # 情人节只剩 25 天赶不上，后面还有母亲节可以提前布局：报母亲节
    assert toy["节日"].startswith("母亲节") and "提前布局" in toy["节日"] and toy["分项"]["节日"] == 50.0
    only_late = discover(env.app.store, playbook('regions = ["US"]\nholiday_lead_weeks = 52'), "US", get=fake_get(RICH),
                         sleep=NO_SLEEP, today=date(2026, 12, 1), top=40)
    toy = next(d for d in only_late["方向"] if d["品类词"] == "pet toys")
    assert "偏晚" in toy["节日"] and toy["分项"]["节日"] == 30.0               # 要提前一年备货：半年内的节日都来不及
