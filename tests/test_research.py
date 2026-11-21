"""选品市场调研：数据源取数、快照和新词、过期闸、定时调研。网络请求全部替换成假数据。"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from ecom_twin.numbers import margin_at_price
from ecom_twin.research import (ResearchError, holiday_date, http_get, run_research, snapshot_problems,
                                upcoming_holidays, HOLIDAYS)
from ecom_twin.selection import Playbook, parse_rules

NO_SLEEP = lambda s: None  # noqa: E731


def playbook(toml: str, name: str = "剧本") -> Playbook:
    body = f"## 规则\n\n```toml\n{toml}\n```\n"
    return Playbook(name, parse_rules(body), body, "v1")


def fake_get(words_by_call):
    calls = []

    def get(url, headers=None, allowed=None):
        calls.append(url)
        if "suggestqueries" in url:
            return json.dumps(["q", words_by_call.pop(0) if words_by_call else []])
        if "trends.google.com/trending/rss" in url:
            return ('<rss xmlns:ht="https://trends.google.com/trending/rss"><channel>'
                    '<item><title>white friday</title><ht:approx_traffic>2000+</ht:approx_traffic></item>'
                    '</channel></rss>')
        if "example-api.test" in url:
            return json.dumps({"isSample": True, "items": [1]})
        raise AssertionError(url)
    get.calls = calls
    return get


# ---- 节日 ----
def test_holiday_dates():
    by_id = {h["id"]: h for h in HOLIDAYS}
    assert holiday_date(by_id["thanksgiving"], 2026) == date(2026, 11, 26)
    assert holiday_date(by_id["blackfriday"], 2026) == date(2026, 11, 27)
    assert holiday_date(by_id["whitefriday"], 2026) == date(2026, 11, 27)
    assert holiday_date(by_id["ramadan"], 2026) == date(2026, 2, 18)
    assert holiday_date(by_id["eid_fitr"], 2026) == date(2026, 3, 20)
    assert holiday_date(by_id["mothers"], 2026) == date(2026, 5, 10)
    assert holiday_date(by_id["easter"], 2026) == date(2026, 4, 5)


def test_upcoming_holidays_by_region():
    sa = [h["id"] for h in upcoming_holidays("SA", date(2026, 11, 19), weeks=14)]
    assert "whitefriday" in sa and "ramadan" in sa and "thanksgiving" not in sa
    us = [h["id"] for h in upcoming_holidays("US", date(2026, 11, 19), weeks=6)]
    assert us[:2] == ["thanksgiving", "blackfriday"] and "whitefriday" not in us


# ---- 跑调研、快照、新词 ----
def test_research_snapshot_baseline_then_new_terms(env):
    pb = playbook('regions = ["SA"]\nlanguages = ["en"]\nseeds = ["oud"]\nholiday_lead_weeks = 1\n'
                  'sources = ["google_suggest", "owner_input", "csv_import"]')
    first = run_research(env.app.store, pb, "sa", get=fake_get([["oud perfume", "oud oil"]]), sleep=NO_SLEEP,
                         today=date(2026, 6, 1))
    assert first["region"] == "SA" and first["new_terms"] == []                  # 第一次是基准，不标新词
    assert first["data"]["google_suggest"]["status"] == "ok"
    assert first["data"]["owner_input"]["status"] == "skipped"
    second = run_research(env.app.store, pb, "SA", get=fake_get([["oud perfume", "oud burner electric"]]),
                          sleep=NO_SLEEP, today=date(2026, 6, 8))
    assert second["new_terms"] == ["oud burner electric"]
    # 运营新加一个种子词：它第一次查到的词是基准，不算新词；老种子词下的新词照常标出
    pb2 = playbook('regions = ["SA"]\nlanguages = ["en"]\nseeds = ["oud", "abaya"]\nholiday_lead_weeks = 1\n'
                   'sources = ["google_suggest"]')
    third = run_research(env.app.store, pb2, "SA", get=fake_get([["oud perfume", "oud gift set"], ["abaya black"]]),
                         sleep=NO_SLEEP, today=date(2026, 6, 15))
    assert third["new_terms"] == ["oud gift set"]


def test_failed_source_is_recorded_not_fatal(env):
    def broken(url, headers=None, allowed=None):
        raise TimeoutError("timed out")
    pb = playbook('regions = ["SA"]\nseeds = ["oud"]\nsources = ["google_suggest", "holidays"]')
    snap = run_research(env.app.store, pb, "SA", get=broken, sleep=NO_SLEEP, today=date(2026, 11, 19))
    assert snap["data"]["google_suggest"]["status"] == "failed" and "timed out" in snap["data"]["google_suggest"]["error"]
    assert snap["data"]["holidays"]["status"] == "ok"


def test_erp_sales_and_third_party(env, monkeypatch):
    env.app.store.upsert_metrics("2026-11-10", "noon:1", "SKU-A", {"sales_qty": 5, "revenue": 100})
    env.app.store.upsert_metrics("2026-11-11", "noon:1", "SKU-A", {"sales_qty": 3, "revenue": 60})
    env.app.store.upsert_metrics("2026-11-11", "noon:2", "SKU-B", {"sales_qty": 1, "revenue": 10})
    pb = playbook('regions = ["SA"]\nshops = ["noon:1"]\nsources = ["erp_sales", "third_party_api"]')
    snap = run_research(env.app.store, pb, "SA", get=fake_get([]), sleep=NO_SLEEP, today=date(2026, 11, 19),
                        third_party={"url": "https://example-api.test/hot"})
    assert snap["data"]["erp_sales"]["items"] == [{"shop_key": "noon:1", "sku": "SKU-A", "sales_qty": 8.0, "revenue": 160.0}]
    assert snap["data"]["third_party_api"]["status"] == "sample"                # 示例数据不算数
    monkeypatch.delenv("HOT_KEY", raising=False)
    snap = run_research(env.app.store, pb, "SA", get=fake_get([]), sleep=NO_SLEEP, today=date(2026, 11, 19),
                        third_party={"url": "https://example-api.test/hot", "key_env": "HOT_KEY"})
    assert snap["data"]["third_party_api"]["status"] == "failed" and "HOT_KEY" in snap["data"]["third_party_api"]["error"]


def test_http_get_only_allows_fixed_public_hosts():
    with pytest.raises(ResearchError, match="不允许"):
        http_get("http://suggestqueries.google.com/x")
    with pytest.raises(ResearchError, match="不允许"):
        http_get("https://169.254.169.254/latest/meta-data")


# ---- 出清单前的闸 ----
def test_snapshot_gate(env):
    pb = playbook('regions = ["SA"]\nresearch_max_age_days = 7\nsources = ["owner_input"]')
    assert "没有调研快照" in snapshot_problems(None, pb.rules, "SA")[0]
    empty = run_research(env.app.store, pb, "SA", sleep=NO_SLEEP)
    assert any("没有一个数据源取到数据" in p for p in snapshot_problems(empty, pb.rules, "SA"))
    good = run_research(env.app.store, pb, "SA", owner_input="自己估的数", sleep=NO_SLEEP)
    assert snapshot_problems(good, pb.rules, "SA") == []
    later = good["created_at"] + timedelta(days=8)
    assert any("超过剧本要求的 7 天" in p for p in snapshot_problems(good, pb.rules, "SA", now=later))
    assert any("不是 AE 的" in p for p in snapshot_problems(good, pb.rules, "AE"))


# ---- 退货和货到付款算进毛利 ----
def test_margin_counts_returns():
    plain = margin_at_price(30, 70, 0.14, 4, commission_rate=0.1)
    assert plain["profit"] == 13.2 and plain["return_cost"] == 0
    r = margin_at_price(30, 70, 0.14, 4, commission_rate=0.1, extra_rate=0.02, return_rate=0.2, return_loss_rate=0.5)
    # (1-0.2)*(30-3.6) - 13.8 + 0.2*0.5*13.8 = 21.12 - 13.8 + 1.38 = 8.7
    assert r["profit"] == 8.7 and r["fees"] == 3.6 and r["return_cost"] == round(30 - 13.8 - 3.6 - 8.7, 4)
    assert r["margin_rate"] == 0.29


def test_rules_validate_new_fields():
    from ecom_twin.selection import PlaybookError
    with pytest.raises(PlaybookError, match="不认识的规则项"):
        playbook('regions = ["SA"]\nauto_research_days = 7')                    # 定时调研已改由雷达规则负责
    with pytest.raises(PlaybookError, match="两位语言代码"):
        playbook('regions = ["SA"]\nlanguages = ["arabic"]')
    with pytest.raises(PlaybookError, match="return_rate"):
        playbook('regions = ["SA"]\nreturn_rate = 1.5')


def test_empty_seeds_skip_suggest_and_empty_results_do_not_pass_the_gate(env):
    pb = playbook('regions = ["US"]\nholiday_lead_weeks = 1\nsources = ["google_suggest"]')
    snap = run_research(env.app.store, pb, "US", get=fake_get([]), sleep=NO_SLEEP, today=date(2026, 6, 1))
    assert snap["data"]["google_suggest"]["status"] == "skipped" and "种子词" in snap["data"]["google_suggest"]["note"]
    pb = playbook('regions = ["US"]\nseeds = ["zzqx"]\nholiday_lead_weeks = 1\nsources = ["google_suggest"]')
    snap = run_research(env.app.store, pb, "US", get=fake_get([[]]), sleep=NO_SLEEP, today=date(2026, 6, 1))
    assert snap["data"]["google_suggest"]["status"] == "ok"
    assert any("没有一个数据源取到数据" in p for p in snapshot_problems(snap, pb.rules, "US"))   # 成功但没内容也不算


def test_region_default_languages_used_when_playbook_has_none(env):
    pb = playbook('regions = ["DE", "TH"]\nseeds = ["lamp"]\nholiday_lead_weeks = 1\nsources = ["google_suggest"]')
    get = fake_get([["lampe"], ["โคมไฟ"]])
    de = run_research(env.app.store, pb, "DE", get=get, sleep=NO_SLEEP, today=date(2026, 6, 1))
    th = run_research(env.app.store, pb, "TH", get=get, sleep=NO_SLEEP, today=date(2026, 6, 1))
    assert list(de["data"]["google_suggest"]["items"]) == ["de:lamp"] and list(th["data"]["google_suggest"]["items"]) == ["th:lamp"]
    assert "hl=de" in get.calls[0] and "gl=DE" in get.calls[0] and "hl=th" in get.calls[1]


def test_local_holiday_seeds_per_language(env):
    pb = playbook('regions = ["DE"]\nholiday_lead_weeks = 6\nsources = ["google_suggest"]')
    get = fake_get([])
    run_research(env.app.store, pb, "DE", get=get, sleep=NO_SLEEP, today=date(2026, 11, 19))
    queried = [c for c in get.calls if "suggestqueries" in c]
    assert any("weihnachtsgeschenke" in c for c in queried) and all("hl=de" in c for c in queried)


# ---- 试算自动取数 ----
def test_fx_rate_cached_per_day(env):
    from ecom_twin.research import fx_rate
    calls = []

    def get(url, headers=None, allowed=None):
        calls.append(url)
        return json.dumps({"date": "2026-11-18", "rates": {"USD": 0.1405}})
    first = fx_rate(env.app.store, get=get, today=date(2026, 11, 19))
    again = fx_rate(env.app.store, get=get, today=date(2026, 11, 19))
    assert first["rate"] == again["rate"] == 0.1405 and len(calls) == 1 and "frankfurter" in calls[0]
    with pytest.raises(ResearchError, match="没返回"):
        fx_rate(env.app.store, get=lambda *a, **k: json.dumps({"rates": {}}), today=date(2026, 11, 20))


def test_estimate_demand_counts_matching_suggestions(env):
    from ecom_twin.research import estimate_demand
    pb = playbook('regions = ["US"]\nholiday_lead_weeks = 1\nsources = ["google_suggest"]')
    snap = run_research(env.app.store, pb, "US", extra_seeds=["heated eye mask"], sleep=NO_SLEEP, today=date(2026, 6, 1),
                        get=fake_get([["heated eye mask", "heated eye mask for dry eyes", "heated eye mask amazon", "eye mask"]]))
    d = estimate_demand(snap, "Heated Eye Mask")
    assert d["count"] == 3 and d["score"] == 15 and "eye mask" not in d["matches"]
    assert estimate_demand(None, "x")["score"] is None


def test_apply_defaults_and_guards():
    from ecom_twin.selection import apply_defaults, score_candidate
    rules = parse_rules('## 规则\n```toml\nregions = ["US"]\ncommission_rates = { amazon = 0.15 }\n'
                        'shipping_base = 2\nshipping_per_kg = 9\n```')
    full, filled = apply_defaults({"price": 25, "source_price": 30, "weight_kg": 0.3}, rules, "amazon", 0.14)
    assert full["commission_rate"] == 0.15 and full["shipping"] == 4.7 and full["exchange_rate"] == 0.14
    assert set(filled) == {"commission_rate", "shipping", "exchange_rate"}
    kept, filled = apply_defaults({"commission_rate": 0.2, "shipping": 3}, rules, "amazon", 0.14)
    assert kept["commission_rate"] == 0.2 and kept["shipping"] == 3 and "commission_rate" not in filled   # 填了的不动
    assert "负数" in score_candidate({"price": 25, "source_price": -0.21, "exchange_rate": 0.14}, rules)["reasons"][0]
    assert "缺汇率" in score_candidate({"price": 25, "source_price": 30}, rules)["reasons"][0]


def test_console_defaults_and_research(env, tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    import ecom_twin.research as research
    from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
    from ecom_twin.memory import MemoryStore
    seed = Path(__file__).resolve().parents[1] / "memory"
    for p in seed.glob("selection-*.md"):
        shutil.copy(p, tmp_path / p.name)

    def get(url, headers=None, allowed=None):
        if "frankfurter" in url:
            return json.dumps({"date": "2026-11-18", "rates": {"USD": 0.14}})
        if "suggestqueries" in url:
            return json.dumps(["q", ["heated eye mask", "heated eye mask for sleeping"]])
        raise AssertionError(url)
    monkeypatch.setattr(research, "http_get", get)
    monkeypatch.setattr(research.time, "sleep", lambda s: None)
    app = ConsoleApp(env.app.store, MemoryStore(env.app.store, tmp_path), tmp_path / "settings.toml")
    status, d = app.route("GET", "/api/selection-defaults", {"slug": "selection-us"}, {})
    assert status == 200 and d["options"] == {"regions": ["US"], "platforms": ["tiktok", "amazon"]}
    assert d["exchange_rate"]["value"] == 0.14 and d["demand"]["stale"] is True and d["shipping"]["value"] is None
    status, d = app.route("GET", "/api/selection-defaults", {"slug": "selection-us", "region": "US", "platform": "amazon",
                                                             "weight": "0.3", "keyword": "heated eye mask"}, {})
    assert d["commission_rate"]["value"] == 0.15 and d["shipping"]["value"] == 4.7
    status, r = app.route("POST", "/api/selection-research", {}, {"region": "US", "platform": "amazon", "keyword": "heated eye mask"})
    assert status == 200 and r["sources"]["google_suggest"] == "ok"
    status, d = app.route("GET", "/api/selection-defaults", {"slug": "selection-us", "region": "US", "platform": "amazon",
                                                             "keyword": "heated eye mask"}, {})
    assert d["demand"]["value"] == 10 and d["demand"]["stale"] is False and d["demand"]["snapshot"] == r["id"]
    assert allowed(ROLE_REVIEWER, "GET", "/api/selection-defaults")
    assert not allowed(ROLE_REVIEWER, "POST", "/api/selection-research")          # 调研会访问外网、写快照，只给负责人
