"""任务标题截断、店铺/场景回填、控制台提示（2026-11-16 写使用教程时体验出来的问题）。"""

import json

from ecom_twin.memory import MemoryStore
from ecom_twin.mcp_server import ToolServer
from ecom_twin.models import TaskSource
from ecom_twin.tasks import PENDING_SCENE, TITLE_MAX, title_from_text


def test_short_text_is_kept_as_is():
    assert title_from_text("  列出我的 TikTok   店铺 ") == "列出我的 TikTok 店铺"


def test_long_text_breaks_at_punctuation_and_never_leaves_an_open_quote():
    text = "帮我起草一条客服回复。店铺 tiktok:7300，买家用英文写的：「My order still hasn't arrived after 12 days.」"
    title = title_from_text(text)
    assert title == "帮我起草一条客服回复。店铺 tiktok:7300，买家用英文写的…"
    assert len(title) <= TITLE_MAX + 1 and "「" not in title


def test_long_text_without_breaks_drops_the_cut_word():
    title = title_from_text("x" * 5 + "abcdefghij" * 6)
    assert title.endswith("…") and len(title) <= TITLE_MAX + 1
    title = title_from_text("请看这个单号的问题" + "TMS1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert title == "请看这个单号的问题…"           # 被截断的半截单号整个去掉，不留一段看起来完整的假单号


def test_unclosed_quote_from_an_early_cut_falls_back_before_the_quote():
    title = title_from_text("把标题改成「Wireless Bluetooth Earbuds with Charging Case Black」再发")
    assert "「" not in title and title.endswith("…")


def _pending_task(env):
    return env.app.tasks.create("帮我起草一条客服回复", TaskSource.SELF_DM, PENDING_SCENE)


def test_cs_tools_fill_shop_and_scene(env, tmp_path):
    task = _pending_task(env)
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    _, err = server.call("cs_profile", {"shop_key": "tiktok:7300", "language": "en"})
    assert not err
    filled = env.app.store.get_task(task.id)
    assert (filled.shop_key, filled.scene) == ("tiktok:7300", "客服回复")


def test_sop_sets_scene_and_existing_values_are_not_overwritten(env, tmp_path):
    task = _pending_task(env)
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    server.call("load_sop", {"name": "tiktok-listing-via-miaoshou"})
    server.call("get_shop_context", {"shop_key": "tiktok:10001"})
    server.call("cs_profile", {"shop_key": "tiktok:7300", "language": "en"})      # 后来的不覆盖先认出来的
    filled = env.app.store.get_task(task.id)
    assert (filled.shop_key, filled.scene) == ("tiktok:10001", "上品")


def test_platform_keys_failed_calls_and_unrelated_tools_fill_nothing(env, tmp_path):
    task = _pending_task(env)
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    server.call("get_shop_context", {"shop_key": "platform:topwms"})           # 内部平台，不是店铺
    server.call("load_sop", {"name": "不存在的 SOP"})                           # 调用失败，不回填
    server.call("price_skus", {"params": {}, "skus": []})
    same = env.app.store.get_task(task.id)
    assert (same.shop_key, same.scene) == ("", PENDING_SCENE)


def test_usage_is_counted_under_the_filled_shop_and_scene(env, tmp_path):
    task = _pending_task(env)
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    server.call("cs_check_reply", {"draft": "Checking now!", "shop_key": "tiktok:7300", "language": "en"})
    env.app.store.add_usage(env.app.store.get_task(task.id), "claude_code", 10, 20, 0.5)
    month = env.app.store.get_task(task.id).created_at.strftime("%Y-%m")
    by_shop = env.app.store.usage_summary(month, "shop_key")
    assert any(r["key"] == "tiktok:7300" for r in by_shop), json.dumps(by_shop, ensure_ascii=False)


def test_console_points_to_setup_when_owner_or_shops_are_missing(tmp_path):
    from ecom_twin.console import ConsoleApp
    from ecom_twin.store import Store
    store = Store(":memory:")
    app = ConsoleApp(store, MemoryStore(store, tmp_path / "mem"), tmp_path / "settings.toml")
    status, data = app.route("POST", "/api/config", {}, {"section": "customer_service", "key": "max_chars", "value": 500})
    assert status == 400 and "ecom-twin setup" in data["error"]
