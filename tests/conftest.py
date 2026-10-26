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

    def list_shops(self, platform, site=""):
        return self.shops

    def tiktok_search_collect_box(self, status="notPublished", page=1, size=20):
        return self.published_list if status == "published" and page == 1 else []

    def tiktok_shop_item_info(self, detail_id, shop_id):
        return {"shopCollectItemInfo": {"title": self.titles[str(detail_id)]}}

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
