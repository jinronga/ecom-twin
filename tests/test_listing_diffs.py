"""控制台「Listing 对比」：按商品汇总暂存区改动，每个字段第一次改之前 → 最后改成的值。"""

from ecom_twin.store import Store


def test_listing_diff_keeps_first_before_and_last_after():
    s = Store(":memory:")
    s.add_staging_change("T-1", "common", "101", "collect", after={"url": "https://detail.1688.com/x"})
    s.add_staging_change("T-1", "common", "101", "edit", before={"title": "夏季女装碎花长裙"},
                         after={"title": "夏季碎花连衣裙"})
    s.add_staging_change("T-1", "common", "101", "edit", before={"title": "夏季碎花连衣裙", "sku_prices_cny": {"红色": 30}},
                         after={"title": "2026 新款夏季碎花连衣裙", "sku_prices_cny": {"红色": 28.5}})
    s.add_staging_change("T-2", "tiktok", "9001", "claim_to_shop", after={"shop": "tiktok:1"}, shop_key="tiktok:1")
    items = {i["object_id"]: i for i in s.listing_diffs()}
    fields = {f["field"]: f for f in items["101"]["fields"]}
    assert fields["title"]["before"] == "夏季女装碎花长裙" and fields["title"]["after"] == "2026 新款夏季碎花连衣裙"
    assert (fields["sku_prices_cny.红色"]["before"], fields["sku_prices_cny.红色"]["after"]) == (30, 28.5)
    assert "url" not in fields                                  # 采集这种没有「改前」的不算字段改动
    assert len(items["101"]["changes"]) == 3 and items["101"]["tasks"] == ["T-1"]
    assert items["9001"]["fields"] == [] and items["9001"]["shop_key"] == "tiktok:1"   # 只认领 / 绑店：只进时间线


def test_filter_by_task_and_unchanged_field_dropped():
    s = Store(":memory:")
    s.add_staging_change("T-1", "common", "101", "edit", before={"title": "A"}, after={"title": "B"})
    s.add_staging_change("T-1", "common", "101", "edit", before={"title": "B"}, after={"title": "A"})   # 改回原样
    s.add_staging_change("T-2", "common", "202", "edit", before={"notes": "旧"}, after={"notes": "新"})
    assert [i["object_id"] for i in s.listing_diffs("T-2")] == ["202"]
    assert s.listing_diffs("T-1")[0]["fields"] == []          # 绕一圈改回原样：没有差别


def test_console_route(tmp_path):
    from ecom_twin.console import ConsoleApp
    from ecom_twin.memory import MemoryStore
    s = Store(":memory:")
    s.add_staging_change("T-1", "common", "101", "edit", before={"title": "A"}, after={"title": "B"})
    status, data = ConsoleApp(s, MemoryStore(s, tmp_path)).route("GET", "/api/listings", {"task": "T-1"}, {})
    assert status == 200 and data["listings"][0]["fields"][0]["after"] == "B"
