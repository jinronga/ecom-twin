"""控制台：规则配置（覆盖文件、校验、高风险二次确认、审计）、投递与合并、本机 CLI、成本、影子。"""

import tomllib

from ecom_twin.config import load_settings
from ecom_twin.console import ConsoleApp
from ecom_twin.memory import MemoryStore
from ecom_twin.store import Store


def console(tmp_path):
    cfg = tmp_path / "settings.toml"
    cfg.write_text('# 手写配置\n[feishu]\nowner_open_id = "ou_boss"\n[limits]\nallowed_shops = ["tiktok:1"]\n'
                   '[approvers."tiktok:1"]\nprimary = "ou_a"\n[approvers."tiktok:2"]\nprimary = "ou_b"\n')
    store = Store(":memory:")
    return ConsoleApp(store, MemoryStore(store, tmp_path / "mem"), cfg), cfg, store


def call(app, method, path, body=None, query=None):
    return app.route(method, path, query or {}, body or {})


def test_config_view_lists_fields_with_current_values(tmp_path):
    app, _, _ = console(tmp_path)
    status, data = call(app, "GET", "/api/config")
    fields = {(f["section"], f["key"]): f for f in data["fields"]}
    assert fields[("limits", "allowed_shops")]["value"] == ["tiktok:1"] and fields[("limits", "allowed_shops")]["high_risk"]
    assert set(fields[("approvers", "")]["value"]) == {"tiktok:1", "tiktok:2"}


def test_normal_field_saves_to_override_file_without_touching_settings(tmp_path):
    app, cfg, store = console(tmp_path)
    original = cfg.read_text()
    status, res = call(app, "POST", "/api/config", {"section": "intake", "key": "quiet_seconds", "value": "30"})
    assert status == 200 and res["ok"]
    assert cfg.read_text() == original                                   # 手写文件和注释不变
    assert load_settings(cfg).intake.quiet_seconds == 30
    assert store.audit_log(("config",))[0]["entity_id"] == "intake.quiet_seconds"


def test_high_risk_requires_confirmation(tmp_path):
    app, cfg, _ = console(tmp_path)
    body = {"section": "limits", "key": "min_margin_rate", "value": "0.2"}
    status, res = call(app, "POST", "/api/config", body)
    assert res["need_confirm"] and res["after"] == 0.2
    assert load_settings(cfg).limits.min_margin_rate == 0.15            # 没确认不保存
    call(app, "POST", "/api/config", {**body, "confirm": True})
    assert load_settings(cfg).limits.min_margin_rate == 0.2


def test_invalid_values_are_rejected(tmp_path):
    app, cfg, _ = console(tmp_path)
    assert call(app, "POST", "/api/config", {"section": "limits", "key": "allowed_shops", "value": "10001",
                                             "confirm": True})[0] == 400      # 格式不对：validate_settings 报错
    assert call(app, "POST", "/api/config", {"section": "intake", "key": "anchor_patterns", "value": "(["})[0] == 400
    assert call(app, "POST", "/api/config", {"section": "storage", "key": "db_path", "value": "/x"})[0] == 400


def test_json_section_replaces_instead_of_merging(tmp_path):
    app, cfg, _ = console(tmp_path)
    call(app, "POST", "/api/config", {"section": "approvers", "key": "",
                                      "value": '{"tiktok:1": {"primary": "ou_a", "delegate": "ou_c"}}', "confirm": True})
    approvers = load_settings(cfg).approvers
    assert set(approvers) == {"tiktok:1"} and approvers["tiktok:1"].delegate == "ou_c"   # tiktok:2 被移除
    overrides = tomllib.loads(cfg.with_name("settings.console.toml").read_text())
    assert overrides["approvers"]["__replace__"] is True


def test_observation_endpoints(tmp_path):
    app, _, store = console(tmp_path)
    store.add_merge_decision("oc_1", ["om_1"], "T-0001", "锚点", True, "锚点 TMS-1")
    store.add_outbound("T-0001", "oc_1", "om_1", "receipt", "私聊负责人", "收到", "om_2")
    status, data = call(app, "GET", "/api/intake")
    assert data["decisions"][0]["layer"] == "锚点" and data["outbound"][0]["kind"] == "receipt"
    status, data = call(app, "GET", "/api/runtimes")
    assert {r["key"] for r in data["runtimes"]} >= {"claude_code", "codex"}
    status, data = call(app, "GET", "/api/costs", query={"month": "2026-10"})
    assert data["month"] == "2026-10" and set(data) >= {"runtime", "scene", "shop_key"}
    assert call(app, "GET", "/api/shadow")[1]["summary"] == []
