from datetime import datetime, timezone
from pathlib import Path

import pytest

from ecom_twin.app import build_app
from ecom_twin.config import Approver, Limits, Settings
from ecom_twin.feishu import FakeFeishu
from ecom_twin.models import TaskSource
from ecom_twin.store import Store

NOW = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
SHOP = "tiktok:10001"


class FakeMiaoshou:
    def __init__(self):
        self.titles = {"901": "Pet Hair Remover", "902": "Feeding Mat"}
        self.published = []
        self.publish_error = None
        self.published_list = []
        self.shops = [{"shopId": 10001, "platform": "tiktok", "site": "US"}]
        self.common = {}
        self.listings = {}
        self.writes = []
        self._next_id = 100

    def list_shops(self, platform, site=""):
        return self.shops

    def tiktok_search_collect_box(self, status="notPublished", page=1, size=20):
        return self.published_list if status == "published" and page == 1 else []

    def tiktok_shop_item_info(self, detail_id, shop_id):
        if str(detail_id) in self.listings:
            entry = self.listings[str(detail_id)]
            return {"ossMd5": entry["md5"], "claimToShopIds": entry["shops"], "shopCollectItemInfo": dict(entry["info"])}
        return {"shopCollectItemInfo": {"title": self.titles[str(detail_id)]}}

    # ---- 采集箱暂存区（有状态的假实现） ----
    def common_fetch_item(self, links):
        mapping = {}
        for link in links:
            self._next_id += 1
            cid = str(self._next_id)
            self.common[cid] = {"md5": "c0", "detail": {"title": "源标题 " + cid, "price": 25.0,
                                "skuMap": {"Blue;M": {"price": 25.0, "stock": 10}, "Red;M": {"price": 26.0, "stock": 3}},
                                "imgUrls": ["a.jpg", "b.jpg"], "sourceList": [{"sourceItemUrl": link}]}}
            mapping["src_" + cid] = int(cid)
        return mapping

    def common_detail(self, detail_id):
        entry = self.common[str(detail_id)]
        return {"ossMd5": entry["md5"], "editCommonCollectBoxDetail": dict(entry["detail"])}

    def common_edit(self, detail_id, detail, oss_md5):
        entry = self.common[str(detail_id)]
        assert oss_md5 == entry["md5"], "ossMd5 不匹配"
        entry["detail"], entry["md5"] = detail, entry["md5"] + "+"
        self.writes.append(("common_edit", detail_id))

    def common_claim(self, detail_ids, platform="tiktok"):
        mapping = {}
        for d in detail_ids:
            tid = str(900 + int(d))
            src = self.common[str(d)]["detail"]
            skus = {k: {"price": 0.0, "stock": v.get("stock")} for k, v in src["skuMap"].items()}
            self.listings[tid] = {"md5": "t0", "shops": [], "info": {"title": src["title"], "cid": "", "skuMap": skus,
                                                                      "imgUrls": src["imgUrls"]}}
            self.titles[tid] = src["title"]
            mapping[str(d)] = int(tid)
        return mapping

    def tiktok_claim_to_shop(self, detail_ids, shop_ids):
        for d in detail_ids:
            self.listings[str(d)]["shops"] = sorted(set(self.listings[str(d)]["shops"]) | set(shop_ids))
        self.writes.append(("claim_to_shop", tuple(detail_ids)))

    def tiktok_shop_item_save(self, detail_id, shop_id, oss_md5, item_info):
        entry = self.listings[str(detail_id)]
        assert oss_md5 == entry["md5"], "ossMd5 不匹配"
        entry["info"], entry["md5"] = item_info, entry["md5"] + "+"
        self.titles[str(detail_id)] = item_info.get("title", "")
        self.writes.append(("listing_save", detail_id))

    def tiktok_publish(self, detail_ids, shop_ids):
        if self.publish_error:
            raise self.publish_error
        self.published.append((detail_ids, shop_ids))


def publish_target(**overrides):
    target = {
        "detail_ids": [901, 902],
        "pricing_params": {"commission_rate": 0.08, "target_margin_rate": 0.30, "exchange_rate": 0.14,
                           "rounding": "0.99", "currency": "USD"},
        "skus": [
            {"detail_id": 901, "sku": "901-Blue", "source_price": 25, "shipping": 3.5, "other_cost": 0.5},
            {"detail_id": 902, "sku": "902-Grey", "source_price": 40, "shipping": 4.0, "other_cost": 0.5},
        ],
    }
    target.update(overrides)
    return target


@pytest.fixture
def settings():
    s = Settings(
        db_path=Path(":memory:"),
        approvers={SHOP: Approver(primary="ou_boss", delegate="ou_backup")},
        limits=Limits(allowed_shops=[SHOP]),
    )
    s.feishu.owner_open_id = "ou_boss"
    s.feishu.base_app_token = "app"
    s.feishu.tasks_table_id = "tbl_tasks"
    s.feishu.steps_table_id = "tbl_steps"
    s.feishu.proposals_table_id = "tbl_proposals"
    return s


@pytest.fixture
def env(settings):
    feishu, miaoshou = FakeFeishu(), FakeMiaoshou()
    app = build_app(settings, Store(":memory:"), feishu, miaoshou)
    task = app.tasks.create("上品测试", TaskSource.SELF_DM, "上品", shop_key=SHOP)

    def submit(target=None, rationale="目标毛利率 30%", now=NOW):
        return app.proposals.submit(task.id, SHOP, "miaoshou.tiktok.publish", target or publish_target(),
                                    {"publish": True}, rationale, now=now)

    return type("Env", (), {"app": app, "feishu": feishu, "miaoshou": miaoshou, "task": task,
                            "submit": staticmethod(submit)})
