"""控制台「客服话术」页：按「店铺/语言」新建、修改、删除；预览只算不存；运营能预览不能改。"""

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from ecom_twin.config import load_settings
from ecom_twin.console import ConsoleApp, make_server
from ecom_twin.cs import normalize_profile, profile_for
from ecom_twin.memory import MemoryStore
from ecom_twin.store import Store

BASE = ('[feishu]\nowner_open_id = "ou_boss"\n[limits]\nallowed_shops = ["tiktok:7300"]\n'
        '[approvers."tiktok:7300"]\nprimary = "ou_a"\n[customer_service]\nmax_chars = 550\n')


def test_normalize_profile_rules():
    assert normalize_profile("tiktok:7300/en", {"voice_samples": "Hi!\n\n  Thanks  ", "max_chars": "400"}) == \
        {"voice_samples": ["Hi!", "Thanks"], "max_chars": 400}
    for bad_key in ("tiktok7300", "tiktok:7300/english", "../x"):
        with pytest.raises(ValueError, match="名称要写成"):
            normalize_profile(bad_key, {"notes": "x"})
    with pytest.raises(ValueError, match="至少填一项"):
        normalize_profile("default", {})
    with pytest.raises(ValueError, match="字数上限"):
        normalize_profile("default", {"max_chars": 10})
    with pytest.raises(ValueError, match="最多 12 句"):
        normalize_profile("default", {"voice_samples": [f"句子{i}" for i in range(13)]})


def test_save_delete_and_preview(tmp_path):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(BASE)
    store = Store(":memory:")
    app = ConsoleApp(store, MemoryStore(store, tmp_path / "mem"), cfg)
    key = "tiktok:7300/en"
    status, _ = app.route("PUT", f"/api/cs-profiles/{quote(key, safe='')}", {},
                          {"voice_samples": "Hey! Checking now.", "banned_phrases": "asap => 不说 asap，给具体时间",
                           "max_chars": 300, "notes": "美区，30 天内可退换"})
    assert status == 200
    p = profile_for(load_settings(cfg).customer_service, "tiktok:7300", "en")
    assert (p.key, p.max_chars, p.notes) == (key, 300, "美区，30 天内可退换")
    status, view = app.route("GET", "/api/cs-profiles", {}, {})
    assert list(view["profiles"]) == [key] and view["defaults"]["max_chars"] == 550
    # 预览：补充的禁用说法生效，三级判定照常
    status, r = app.route("POST", "/api/cs-preview", {}, {"shop_key": "tiktok:7300", "language": "en",
                                                         "buyer_message": "refund please", "draft": "We will fix it asap"})
    assert r["profile"] == key and not r["passed"] and any("asap" in x for x in r["problems"]) and r["risk_level"] == 3
    assert "cs-preview" not in json.dumps(store.audit_events("config"))           # 预览不留改动记录
    status, _ = app.route("DELETE", f"/api/cs-profiles/{quote(key, safe='')}", {}, {})
    assert status == 200 and load_settings(cfg).customer_service.profiles == {}
    assert load_settings(cfg).customer_service.max_chars == 550                    # 同节其它项不受影响
    assert app.route("DELETE", f"/api/cs-profiles/{quote(key, safe='')}", {}, {})[0] == 404


def test_http_slash_in_name_and_roles(tmp_path):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(BASE)
    store = Store(":memory:")
    srv, owner = make_server(store, MemoryStore(store, tmp_path / "mem"), port=0, token="own", review_token="ops",
                             settings_path=cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, token, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json", "X-Console-Token": token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        path = "/api/cs-profiles/" + quote("tiktok:7300/en", safe="")
        assert call("PUT", path, "ops", {"notes": "x"})[0] == 403                    # 运营不能改话术
        assert call("PUT", path, "own", {"notes": "美区"})[0] == 200                  # %2F 落在同一段里
        assert list(load_settings(cfg).customer_service.profiles) == ["tiktok:7300/en"]
        status, r = call("POST", "/api/cs-preview", "ops", {"shop_key": "tiktok:7300", "language": "en", "draft": "On the way!"})
        assert status == 200 and r["passed"] and r["profile"] == "tiktok:7300/en"   # 运营能预览
    finally:
        srv.shutdown()


def test_replace_marker_is_stripped_even_when_base_has_no_section(tmp_path):
    """settings.toml 里没有 [customer_service] 时，覆盖文件里 profiles 的替换标记也要剥掉（2026-11-13 浏览器实测发现）。"""
    from ecom_twin.config_overrides import REPLACE, deep_merge
    merged = deep_merge({}, {"customer_service": {"profiles": {REPLACE: True, "default": {"notes": "x"}}}})
    assert merged == {"customer_service": {"profiles": {"default": {"notes": "x"}}}}
    cfg = tmp_path / "settings.toml"
    cfg.write_text(BASE.replace("[customer_service]\nmax_chars = 550\n", ""))
    store = Store(":memory:")
    app = ConsoleApp(store, MemoryStore(store, tmp_path / "mem"), cfg)
    assert app.route("PUT", "/api/cs-profiles/default", {}, {"notes": "通用"})[0] == 200
    assert list(load_settings(cfg).customer_service.profiles) == ["default"]
