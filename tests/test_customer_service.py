"""客服-0（DESIGN.md 8.7）：只起草不发送；风险由代码分级；花钱的不能在回复里答应；买家隐私打码。"""

import json

import pytest

from ecom_twin.cs import CsSettings, check_reply, mask_pii, profile_for, risk_level
from ecom_twin.memory import MemoryStore
from ecom_twin.mcp_server import ToolServer

CS = CsSettings()
PROFILE = profile_for(CS, "tiktok:7300", "en")


def test_risk_levels_by_code():
    assert risk_level("Where is my order? It's been 10 days.", "Your parcel is in transit, tracking shows it left LA today.") == (1, [])
    level, why = risk_level("Where is my order?", "It will arrive within 3 days.")
    assert level == 2 and "within 3" in why[0]
    level, why = risk_level("I want a refund, the dress is torn", "Sorry about that, we're checking with the warehouse.")
    assert level == 3 and "买家提到「refund」" in why[0]
    assert risk_level("这个能退款吗", "我们核实一下")[0] == 3
    assert risk_level("能便宜点吗", "可以给您 $5 优惠")[0] == 3            # 回复里写了金额也算三级


def test_reply_checks():
    ok = "Hi! Your parcel left our warehouse yesterday and is on the way. Thanks for your patience."
    assert check_reply(ok, PROFILE, "tiktok") == []
    assert any("不能诱导" in p for p in check_reply("Please leave a 5-star review for us!", PROFILE, "tiktok"))
    assert any("站外" in p or "联系方式" in p for p in check_reply("Contact us at WhatsApp for faster help", PROFILE, "tiktok"))
    assert any("答应了花钱" in p for p in check_reply("We will refund you right away.", PROFILE, "tiktok"))
    assert any("金额" in p for p in check_reply("We can offer $5 back.", PROFILE, "tiktok"))
    assert check_reply("We're checking your refund request with the team and will update you.", PROFILE, "tiktok") == []
    assert any("超过上限" in p for p in check_reply("ok " * 400, PROFILE, "tiktok"))
    assert any("guaranteed" in p for p in check_reply("Delivery is guaranteed tomorrow.", PROFILE, "tiktok"))


def test_mask_pii_and_profiles():
    assert mask_pii("I'm Jane, jane.doe@gmail.com, +1 (415) 555-0199, order 5781234") == \
        "I'm Jane, <邮箱>, <电话>, order 5781234"
    cs = CsSettings(profiles={"tiktok:7300/ar": {"voice_samples": ["مرحبا"], "max_chars": 300},
                              "*/en": {"notes": "美区语气随意"}, "default": {"voice_samples": ["您好"]}})
    assert profile_for(cs, "tiktok:7300", "ar").max_chars == 300
    assert profile_for(cs, "tiktok:9999", "en").notes == "美区语气随意"
    assert profile_for(cs, "noon:1", "zh").voice_samples == ["您好"]


def test_tool_flow_submit_rejects_then_accepts_and_marks(env, tmp_path):
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id)
    buyer = "I want a refund!! email me at angry@buyer.com"
    text, err = server.call("cs_submit_draft", {"shop_key": "tiktok:7300", "language": "en", "buyer_message": buyer,
                                                "draft": "Sure, we will refund you $20 today."})
    assert err and "没通过检查" in text
    text, err = server.call("cs_submit_draft", {"shop_key": "tiktok:7300", "language": "en", "buyer_message": buyer,
                                                "draft": "Sorry for the trouble. We're reviewing your request and will update you here.",
                                                "translation_zh": "抱歉，我们正在核实，会在这里回复您"})
    r = json.loads(text)
    assert not err and r["风险等级"] == 3 and "由负责人定" in r["说明"]
    saved = env.app.store.get_cs_draft(r["draft_id"])
    assert "angry@buyer.com" not in saved["buyer_message"] and "<邮箱>" in saved["buyer_message"]
    text, err = server.call("cs_mark_sent", {"draft_id": r["draft_id"], "status": "改后发送"})
    assert err and "实际发出去的文字" in text
    server.call("cs_mark_sent", {"draft_id": r["draft_id"], "status": "改后发送", "final_text": "Sorry! Checking now."})
    stats = env.app.store.cs_stats()
    assert stats["counts"] == {"改后发送": 1} and stats["edit_rate"] == 1.0


def test_console_tab_and_reviewer_can_mark(env, tmp_path):
    from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
    store = env.app.store
    did = store.add_cs_draft("T-1", "tiktok:7300", "tiktok", "en", "where is my order", "On the way!", "", 1, [])
    app = ConsoleApp(store, MemoryStore(store, tmp_path))
    status, data = app.route("GET", "/api/cs-drafts", {"status": "待发送"}, {})
    assert status == 200 and data["drafts"][0]["id"] == did
    assert allowed(ROLE_REVIEWER, "POST", f"/api/cs-drafts/{did}/decide")     # 运营发的回复，结果也由运营记
    status, _ = app.route("POST", f"/api/cs-drafts/{did}/decide", {}, {"status": "已发送"})
    assert status == 200 and store.get_cs_draft(did)["status"] == "已发送"
    with pytest.raises(ValueError):
        store.decide_cs_draft(did, "乱写的状态")


def test_sop_is_listed_and_complaint_sop_no_longer_self_approves():
    from ecom_twin.paths import SKILLS_DIR
    from ecom_twin.sops import SopLibrary
    assert "customer-service-reply" in SopLibrary([SKILLS_DIR]).index_text()
    complaint = (SKILLS_DIR / "customer-complaint" / "SKILL.md").read_text(encoding="utf-8")
    assert "可直接补发/退款" not in complaint and "先处理再补报备" not in complaint and "全额退款，无需退货" not in complaint


def test_console_saves_profiles_field_without_clobbering_the_section(tmp_path):
    """profiles 是 [customer_service] 里的一个 JSON 字段：保存只替换它，不能把整节换掉（2026-11-13 修的 bug）。"""
    from ecom_twin.config import load_settings
    from ecom_twin.console import ConsoleApp
    from ecom_twin.store import Store
    cfg = tmp_path / "settings.toml"
    cfg.write_text('[feishu]\nowner_open_id = "ou_boss"\n[limits]\nallowed_shops = ["tiktok:7300"]\n'
                   '[approvers."tiktok:7300"]\nprimary = "ou_a"\n[customer_service]\nmax_chars = 550\n')
    store = Store(":memory:")
    app = ConsoleApp(store, MemoryStore(store, tmp_path / "mem"), cfg)
    value = {"tiktok:7300/en": {"voice_samples": ["Hey! Checking now."], "notes": "30 天内可退换"}}
    assert app.route("POST", "/api/config", {}, {"section": "customer_service", "key": "profiles", "value": value})[0] == 200
    s = load_settings(cfg)
    assert s.customer_service.max_chars == 550
    assert profile_for(s.customer_service, "tiktok:7300", "en").notes == "30 天内可退换"
    app.route("POST", "/api/config", {}, {"section": "customer_service", "key": "profiles", "value": {"default": {"notes": "通用"}}})
    assert list(load_settings(cfg).customer_service.profiles) == ["default"]      # 整个字段替换，不是合并
