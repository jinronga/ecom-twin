"""P1 上品闭环：合规检查、采集箱暂存区写入留痕、发布卡片汇总改动。"""

import json

import pytest

from conftest import NOW, SHOP
from ecom_twin.adapters.miaoshou import MiaoshouClient
from ecom_twin.compliance import ComplianceChecker
from ecom_twin.memory import MemoryStore
from ecom_twin.mcp_server import ToolServer
from ecom_twin.models import ProposalStatus
from ecom_twin.proposals import ProposalRejected
from ecom_twin.staging import StagingError

GOOD_TITLE = "Pet Hair Remover Roller Reusable Lint Brush for Dog Cat Couch"


# ---- 合规检查 ----
def test_compliance_rules():
    c = ComplianceChecker()
    assert c.blocking(c.check(GOOD_TITLE, "tiktok")) == []
    hits = [str(v) for v in c.check("Best Nike Shoes cure pain, add WhatsApp 123", "tiktok")]
    assert any("绝对化" in h for h in hits) and any("Nike" in h for h in hits)
    assert any("医疗" in h for h in hits) and any("站外联系" in h for h in hits)
    assert c.blocking(c.check("x" * 300, "tiktok"))                       # 超长：禁止
    assert not c.blocking(c.check("Short title", "tiktok"))              # 太短：只提醒
    assert not c.check("Dog treatment brush with gentle bristles for daily grooming", "tiktok")  # treat 不误伤 treatment


def test_own_brand_is_not_infringement():
    c = ComplianceChecker({"common": {"brand_terms": ["Stanley"], "own_brands": ["Stanley"]}})
    assert c.check("Stanley tumbler 40oz insulated with handle and straw", "tiktok") == []


# ---- 适配器请求结构 ----
def test_staging_adapter_request_shapes():
    calls = []

    def transport(url, headers, body, timeout):
        payload = json.loads(body)
        calls.append((url.rsplit("/", 1)[-1], payload))
        data = {"fetch_item": {"sourceItemIdAndDetailIdMap": {"s1": 11}},
                "claimed": {"platformCollectBoxDetailIdMap": {"tiktok": {"11": 911}}}}.get(url.rsplit("/", 1)[-1], {})
        return 200, json.dumps({"result": "success", "code": "0", "data": data})

    c = MiaoshouClient("ak", "as", transport=transport, clock=lambda: 1)
    assert c.common_fetch_item(["https://detail.1688.com/offer/1.html"]) == {"s1": 11}
    assert c.common_claim([11]) == {"11": 911}
    c.common_edit(11, {"title": "t"}, "md5")
    c.tiktok_shop_item_save(911, 10001, "m2", {"title": "t"})
    shapes = dict(calls)
    assert shapes["fetch_item"] == {"collectLinks": ["https://detail.1688.com/offer/1.html"]}
    assert shapes["claimed"] == {"detailSerialNumberPlatformList": [{"detailId": 11, "platform": "tiktok", "serialNumber": 1}]}
    assert shapes["edit_common_collect_box_detail"] == {"commonCollectBoxDetailId": 11,
                                                         "editCommonCollectBoxDetail": {"title": "t"}, "ossMd5": "md5"}
    assert shapes["save_shop_collect_item_info"] == {"ossMd5": "m2", "detailId": 911, "shopId": 10001,
                                                     "shopCollectItemInfo": {"title": "t"}}


# ---- 暂存区服务 ----
def test_collect_rejects_non_source_domains(env):
    with pytest.raises(StagingError, match="白名单"):
        env.app.staging.collect(env.task.id, ["https://evil.example.com/x"])
    with pytest.raises(StagingError):
        env.app.staging.collect(env.task.id, [])


def test_edit_common_is_read_modify_write_and_logged(env):
    cid = list(env.app.staging.collect(env.task.id, ["https://detail.1688.com/offer/1.html"]).values())[0]
    res = env.app.staging.edit_common(env.task.id, cid, title=GOOD_TITLE, sku_prices_cny={"Blue;M": 23.5})
    assert res["changed"]["title"] == GOOD_TITLE
    detail = env.miaoshou.common[str(cid)]["detail"]
    assert detail["skuMap"]["Blue;M"]["price"] == 23.5 and detail["skuMap"]["Red;M"]["price"] == 26.0  # 没改的保留
    assert detail["imgUrls"] == ["a.jpg", "b.jpg"]  # 先读后改，不丢字段
    with pytest.raises(StagingError, match="合规"):
        env.app.staging.edit_common(env.task.id, cid, title="Best Nike shoes for running and walking every day")
    with pytest.raises(StagingError, match="不存在"):
        env.app.staging.edit_common(env.task.id, cid, sku_prices_cny={"Green;M": 1})


def test_claim_to_shop_requires_whitelisted_shop(env):
    with pytest.raises(StagingError, match="白名单"):
        env.app.staging.claim_to_shop(env.task.id, [901], "tiktok:99999")


def test_staging_write_limit(env):
    env.app.staging.max_writes = 1
    env.app.staging.collect(env.task.id, ["https://detail.1688.com/offer/1.html"])
    with pytest.raises(StagingError, match="上限"):
        env.app.staging.collect(env.task.id, ["https://detail.1688.com/offer/2.html"])


# ---- 端到端：agent 工具走完整条上品流程 ----
def call(server, name, args):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    text = r["result"]["content"][0]["text"]
    return r["result"]["isError"], (json.loads(text) if text[:1] in "{[" else text)


def test_full_listing_flow_through_agent_tools(env, tmp_path):
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id)
    err, res = call(server, "staging_collect", {"links": ["https://detail.1688.com/offer/1.html"]})
    assert not err
    cid = list(res["source_to_common_id"].values())[0]
    assert not call(server, "staging_edit_common", {"detail_id": cid, "title": GOOD_TITLE})[0]
    err, mapping = call(server, "staging_claim_to_tiktok", {"common_ids": [cid]})
    tid = mapping[str(cid)]
    assert not call(server, "staging_claim_to_shop", {"detail_ids": [tid], "shop_key": SHOP})[0]
    err, res = call(server, "staging_edit_listing", {"detail_id": tid, "shop_key": SHOP, "title": GOOD_TITLE,
                                                     "cid": "601234", "sku_prices": {"Blue;M": 12.99}})
    assert not err and res["changed"]["sku_prices"] == {"Blue;M": 12.99}
    err, text = call(server, "staging_edit_listing", {"detail_id": tid, "shop_key": SHOP, "title": "Best cure 100%"})
    assert err and "合规" in text

    target = {"detail_ids": [tid], "pricing_params": {"commission_rate": 0.08, "target_margin_rate": 0.3,
                                                      "exchange_rate": 0.14, "rounding": "0.99", "currency": "USD"},
              "skus": [{"detail_id": tid, "sku": "Blue;M", "source_price": 25, "shipping": 3.5, "other_cost": 0.5}]}
    err, res = call(server, "submit_proposal", {"shop_key": SHOP, "action": "miaoshou.tiktok.publish",
                                                "target": target, "rationale": "目标毛利率 30%"})
    assert not err
    p = env.app.store.get_proposal(res["proposal_id"])
    changes = p.evidence["暂存区改动"]
    assert any("从货源采集" in c for c in changes) and any("认领到店铺" in c for c in changes)
    assert any("Blue;M 售价 0.0 → 12.99" in c for c in changes) and any("类目" in c for c in changes)
    card = "\n".join(e["text"]["content"] for e in env.feishu.sent[-1]["card"]["elements"] if e["tag"] == "div")
    assert "暂存区改动" in card and "合规检查" in card
    assert "_合规禁止项" not in card

    env.app.approvals.handle_message({"message_id": "om_a", "sender_id": "ou_boss", "content": f"批准 {p.id}"}, now=NOW)
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.EXECUTED
    assert env.miaoshou.published[-1] == ([tid], [10001])


def test_publish_rejected_when_not_claimed_or_title_violates(env):
    cid = list(env.app.staging.collect(env.task.id, ["https://detail.1688.com/offer/9.html"]).values())[0]
    tid = env.app.staging.claim_to_tiktok(env.task.id, [cid])[str(cid)]
    target = {"detail_ids": [tid], "pricing_params": {"commission_rate": 0.08, "target_margin_rate": 0.3},
              "skus": [{"detail_id": tid, "sku": "Blue;M", "source_price": 3}]}
    with pytest.raises(ProposalRejected, match="还没认领到店铺"):
        env.app.proposals.submit(env.task.id, SHOP, "miaoshou.tiktok.publish", target, {}, "", now=NOW)
    env.app.staging.claim_to_shop(env.task.id, [tid], SHOP)
    env.miaoshou.listings[str(tid)]["info"]["title"] = "Disney princess dress for girls birthday party costume"
    with pytest.raises(ProposalRejected, match="侵权"):
        env.app.proposals.submit(env.task.id, SHOP, "miaoshou.tiktok.publish", target, {}, "", now=NOW)
