"""写操作类型（ActionHandler）。

每种写操作实现同一组方法，执行器只依赖这组方法（DESIGN.md 6.1 Writer / 7.1）：
- prepare：由代码读取当前值、计算证据和后果（不依赖模型）
- check_limits：该操作特有的硬上限
- snapshot / drifted：执行前快照与漂移校验
- apply / verify / rollback
"""

from __future__ import annotations

from typing import Any, Protocol

from .config import Limits
from .models import ExecMethod, Proposal
from .numbers import price_sku


class ActionHandler(Protocol):
    action: str
    method: ExecMethod

    def prepare(self, proposal: Proposal) -> None: ...
    def check_limits(self, proposal: Proposal, limits: Limits) -> list[str]: ...
    def snapshot(self, proposal: Proposal) -> dict[str, Any]: ...
    def drifted(self, proposal: Proposal, snapshot: dict[str, Any]) -> list[str]: ...
    def apply(self, proposal: Proposal) -> tuple[dict[str, Any], Any]: ...
    def verify(self, proposal: Proposal, response: Any) -> bool: ...
    def rollback(self, proposal: Proposal, snapshot: dict[str, Any]) -> dict[str, Any] | None: ...


class TiktokPublishViaMiaoshou:
    """经妙手把采集箱商品发布到 TikTok 店铺。

    target = {
        "detail_ids": [...],                       # 妙手 TikTok 采集箱详情 ID
        "pricing_params": {...},                   # 定价参数（来自用户规则）
        "skus": [{"detail_id", "sku", "source_price", "shipping", "other_cost"}],
    }
    """

    action = "miaoshou.tiktok.publish"
    method = ExecMethod.API

    def __init__(self, client: Any):
        self.client = client

    def _titles(self, proposal: Proposal) -> dict[str, str]:
        shop_id = int(proposal.shop_id)
        titles = {}
        for detail_id in proposal.target["detail_ids"]:
            info = self.client.tiktok_shop_item_info(int(detail_id), shop_id)
            titles[str(detail_id)] = (info.get("shopCollectItemInfo") or {}).get("title", "")
        return titles

    def prepare(self, proposal: Proposal) -> None:
        params = proposal.target.get("pricing_params", {})
        priced = [price_sku(sku, params) for sku in proposal.target.get("skus", [])]
        ok = [p for p in priced if "error" not in p]
        margins = [p["margin_rate"] for p in ok]
        prices = [p["price"] for p in ok]
        currency = params.get("currency", "USD")

        proposal.current = {"titles": self._titles(proposal)}
        proposal.evidence = {
            "商品数": len(proposal.target["detail_ids"]),
            "SKU 数": len(ok),
            "定价参数": params,
            "定价明细": priced,
        }
        proposal.consequences = {
            "花费": "上架本身不产生平台费用；按售价成交后扣佣金",
            "售价区间": f"{min(prices):.2f}~{max(prices):.2f} {currency}" if prices else "无",
            "最低毛利率": min(margins) if margins else None,
            "定价失败的 SKU": [p["sku"] for p in priced if "error" in p],
            "能否撤销": "可在 TikTok 后台下架；下架前产生的曝光和订单无法撤回",
            "不批准": "商品留在妙手采集箱，不上架",
        }
        proposal.risk = "中"

    def check_limits(self, proposal: Proposal, limits: Limits) -> list[str]:
        violations = []
        n = len(proposal.target["detail_ids"])
        if n > limits.max_publish_items_per_proposal:
            violations.append(f"商品数 {n} 超过单个提案上限 {limits.max_publish_items_per_proposal}")
        for item in proposal.evidence.get("定价明细", []):
            if "error" in item:
                violations.append(f"SKU {item['sku']} 定价失败：{item['error']}")
            elif item["margin_rate"] < limits.min_margin_rate:
                violations.append(
                    f"SKU {item['sku']} 毛利率 {item['margin_rate']:.2%} 低于下限 {limits.min_margin_rate:.0%}"
                )
        return violations

    def snapshot(self, proposal: Proposal) -> dict[str, Any]:
        return {"titles": self._titles(proposal)}

    def drifted(self, proposal: Proposal, snapshot: dict[str, Any]) -> list[str]:
        before = proposal.current.get("titles", {})
        return [
            f"商品 {detail_id} 标题已变化"
            for detail_id, title in snapshot.get("titles", {}).items()
            if before.get(detail_id) != title
        ]

    def apply(self, proposal: Proposal) -> tuple[dict[str, Any], Any]:
        request = {"detail_ids": proposal.target["detail_ids"], "shop_ids": [int(proposal.shop_id)]}
        response = self.client.tiktok_publish([int(d) for d in request["detail_ids"]], request["shop_ids"])
        return request, response

    def verify(self, proposal: Proposal, response: Any) -> bool:
        # 发布是妙手侧的异步任务：这里只确认接口受理成功（apply 未抛错即受理）。
        # 「已发布」的回读确认由 followups.py 在执行后定时轮询完成（confirm）。
        return True

    def rollback(self, proposal: Proposal, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        return None  # 妙手没有撤销发布的接口，需人工在 TikTok 后台下架

    # ---- 执行后跟进 ----
    followup_kinds = ("confirm_publish",)

    def confirm(self, proposal: Proposal, max_pages: int = 5, page_size: int = 50) -> tuple[bool, str]:
        """回读确认：在妙手 TikTok 采集箱「已发布」列表里找到全部商品才算上架成功。"""
        wanted = {str(d) for d in proposal.target["detail_ids"]}
        found: set[str] = set()
        for page in range(1, max_pages + 1):
            data = self.client.tiktok_search_collect_box(status="published", page=page, size=page_size)
            # 妙手当前返回 ``detailList``；保留其它常见包装名以兼容历史版本。
            rows = data if isinstance(data, list) else next(
                (data[k] for k in ("detailList", "list", "records", "data", "items")
                 if isinstance(data, dict) and isinstance(data.get(k), list)), [])
            found |= {str(r.get("collectBoxDetailId")) for r in rows} & wanted
            if found == wanted or len(rows) < page_size:
                break
        missing = sorted(wanted - found)
        return (not missing, "全部已发布" if not missing else f"尚未在已发布列表中：{', '.join(missing)}")
