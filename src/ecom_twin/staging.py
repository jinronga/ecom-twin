"""妙手采集箱暂存区操作（P1 上品闭环，DESIGN.md 8.5）。

用户决定（2026-10-27）：采集、编辑采集箱草稿、认领这类「暂存区」写入买家看不到、不花钱，
不逐次审批，agent 可以直接做；但每一次改动都留痕（改前 / 改后），
最后「发布」时只审批一次，审批卡片列出这批商品在暂存区的全部改动。

暂存区写入同样有边界：
- 货源链接只允许白名单域名
- 标题、描述先过合规检查，block 级违规直接拒绝
- 认领到店铺只允许可操作白名单内的店铺
- 每个任务的暂存区写入次数有上限
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .compliance import ComplianceChecker
from .config import Settings

SOURCE_DOMAINS = ("1688.com", "taobao.com", "tmall.com", "aliexpress.com", "alibaba.com", "pinduoduo.com",
                  "yangkeduo.com", "temu.com", "shein.com", "amazon.com", "tiktok.com")


class StagingError(ValueError):
    pass


def _sku_summary(sku_map: dict) -> list[dict[str, Any]]:
    return [{"sku_key": k, "price": (v or {}).get("price"), "stock": (v or {}).get("stock"),
             "item_num": (v or {}).get("itemNum")} for k, v in (sku_map or {}).items()]


class StagingService:
    def __init__(self, store: Any, client: Any, settings: Settings, compliance: ComplianceChecker | None = None,
                 max_writes_per_task: int = 200):
        self.store = store
        self.client = client
        self.settings = settings
        self.compliance = compliance or ComplianceChecker()
        self.max_writes = max_writes_per_task

    def _guard(self, task_id: str) -> None:
        if self.store.count_staging_writes(task_id) >= self.max_writes:
            raise StagingError(f"任务 {task_id} 的暂存区写入已达上限 {self.max_writes} 次")

    def _check_text(self, text: str | None, field: str, platform: str) -> list[str]:
        if not text:
            return []
        violations = self.compliance.check(text, platform, field)
        blocking = self.compliance.blocking(violations)
        if blocking:
            raise StagingError("合规检查不通过：" + "；".join(str(v) for v in blocking))
        return [str(v) for v in violations]

    # ---- 采集 ----
    def collect(self, task_id: str, links: list[str]) -> dict[str, int]:
        if not links or len(links) > 50:
            raise StagingError("一次采集 1～50 个链接")
        for link in links:
            host = (urlparse(link).hostname or "").lower()
            if urlparse(link).scheme not in ("http", "https") or not any(
                    host == d or host.endswith("." + d) for d in SOURCE_DOMAINS):
                raise StagingError(f"货源链接不在白名单内：{link}")
        self._guard(task_id)
        mapping = self.client.common_fetch_item(links)
        for source_id, detail_id in mapping.items():
            self.store.add_staging_change(task_id, "common", str(detail_id), "collect",
                                          after={"source_item_id": source_id, "links": links})
        return mapping

    # ---- 公共采集箱 ----
    def common_summary(self, detail_id: int) -> dict[str, Any]:
        data = self.client.common_detail(int(detail_id))
        detail = data.get("editCommonCollectBoxDetail") or {}
        return {"detail_id": detail_id, "title": detail.get("title"), "price_cny": detail.get("price"),
                "stock": detail.get("stock"), "image_count": len(detail.get("imgUrls") or []),
                "skus": _sku_summary(detail.get("skuMap")),
                "sources": [s.get("sourceItemUrl") for s in detail.get("sourceList") or []]}

    def edit_common(self, task_id: str, detail_id: int, title: str | None = None,
                    sku_prices_cny: dict[str, float] | None = None, notes: str | None = None) -> dict[str, Any]:
        warnings = self._check_text(title, "标题", "tiktok") + self._check_text(notes, "描述", "tiktok")
        self._guard(task_id)
        data = self.client.common_detail(int(detail_id))
        detail = dict(data.get("editCommonCollectBoxDetail") or {})
        if not detail:
            raise StagingError(f"读取公共采集箱 {detail_id} 失败或内容为空")
        before, after = {}, {}
        if title is not None:
            before["title"], after["title"] = detail.get("title"), title
            detail["title"] = title
        if notes is not None:
            before["notes"], after["notes"] = (detail.get("notes") or "")[:200], notes[:200]
            detail["notes"] = notes
        if sku_prices_cny:
            sku_map = {k: dict(v or {}) for k, v in (detail.get("skuMap") or {}).items()}
            for key, price in sku_prices_cny.items():
                if key not in sku_map:
                    raise StagingError(f"SKU「{key}」不存在，可用：{', '.join(sku_map)}")
                if float(price) <= 0:
                    raise StagingError(f"SKU「{key}」价格必须大于 0")
                before.setdefault("sku_prices_cny", {})[key] = sku_map[key].get("price")
                after.setdefault("sku_prices_cny", {})[key] = float(price)
                sku_map[key]["price"] = float(price)
            detail["skuMap"] = sku_map
        if not after:
            raise StagingError("没有要修改的内容")
        self.client.common_edit(int(detail_id), detail, data.get("ossMd5", ""))
        self.store.add_staging_change(task_id, "common", str(detail_id), "edit", before, after)
        return {"ok": True, "changed": after, "warnings": warnings}

    def claim_to_tiktok(self, task_id: str, common_ids: list[int]) -> dict[str, int]:
        self._guard(task_id)
        mapping = self.client.common_claim([int(c) for c in common_ids], "tiktok")
        for common_id, platform_id in mapping.items():
            self.store.link_staging(common_id, "tiktok", str(platform_id))
            self.store.add_staging_change(task_id, "tiktok", str(platform_id), "claim",
                                          after={"from_common_id": common_id})
        return mapping

    # ---- TikTok 采集箱（店铺模式） ----
    def claim_to_shop(self, task_id: str, tiktok_ids: list[int], shop_key: str) -> dict[str, Any]:
        if shop_key not in self.settings.limits.allowed_shops:
            raise StagingError(f"店铺 {shop_key} 不在可操作白名单内")
        self._guard(task_id)
        shop_id = int(shop_key.split(":", 1)[1])
        self.client.tiktok_claim_to_shop([int(t) for t in tiktok_ids], [shop_id])
        for t in tiktok_ids:
            self.store.add_staging_change(task_id, "tiktok", str(t), "claim_to_shop", after={"shop": shop_key},
                                          shop_key=shop_key)
        return {"ok": True, "detail_ids": tiktok_ids, "shop": shop_key}

    def listing_summary(self, tiktok_id: int, shop_key: str) -> dict[str, Any]:
        shop_id = int(shop_key.split(":", 1)[1])
        info = (self.client.tiktok_shop_item_info(int(tiktok_id), shop_id) or {}).get("shopCollectItemInfo") or {}
        return {"detail_id": tiktok_id, "shop": shop_key, "title": info.get("title"), "cid": info.get("cid"),
                "image_count": len(info.get("imgUrls") or []), "skus": _sku_summary(info.get("skuMap"))}

    def edit_listing(self, task_id: str, tiktok_id: int, shop_key: str, title: str | None = None,
                     sku_prices: dict[str, float] | None = None, cid: str | None = None) -> dict[str, Any]:
        if shop_key not in self.settings.limits.allowed_shops:
            raise StagingError(f"店铺 {shop_key} 不在可操作白名单内")
        warnings = self._check_text(title, "标题", shop_key.split(":", 1)[0])
        self._guard(task_id)
        shop_id = int(shop_key.split(":", 1)[1])
        data = self.client.tiktok_shop_item_info(int(tiktok_id), shop_id) or {}
        info = dict(data.get("shopCollectItemInfo") or {})
        if not info:
            raise StagingError(f"读取 TikTok 采集箱 {tiktok_id} 在 {shop_key} 的信息失败，先认领到店铺")
        before, after = {}, {}
        if title is not None:
            before["title"], after["title"] = info.get("title"), title
            info["title"] = title
        if cid is not None:
            before["cid"], after["cid"] = info.get("cid"), cid
            info["cid"] = cid
        if sku_prices:
            sku_map = {k: dict(v or {}) for k, v in (info.get("skuMap") or {}).items()}
            for key, price in sku_prices.items():
                if key not in sku_map:
                    raise StagingError(f"SKU「{key}」不存在，可用：{', '.join(sku_map)}")
                if float(price) <= 0:
                    raise StagingError(f"SKU「{key}」售价必须大于 0")
                before.setdefault("sku_prices", {})[key] = sku_map[key].get("price")
                after.setdefault("sku_prices", {})[key] = float(price)
                sku_map[key]["price"] = float(price)
            info["skuMap"] = sku_map
        if not after:
            raise StagingError("没有要修改的内容")
        self.client.tiktok_shop_item_save(int(tiktok_id), shop_id, data.get("ossMd5", ""), info)
        self.store.add_staging_change(task_id, "tiktok", str(tiktok_id), "edit_listing", before, after,
                                      shop_key=shop_key)
        return {"ok": True, "changed": after, "warnings": warnings}

    # ---- 发布卡片用 ----
    def changes_for_publish(self, tiktok_ids: list[str]) -> list[dict]:
        ids = [str(t) for t in tiktok_ids]
        changes = self.store.staging_changes_for("tiktok", ids)
        changes += self.store.staging_changes_for("common", self.store.common_ids_for("tiktok", ids))
        return sorted(changes, key=lambda c: c["id"])


def describe_change(change: dict) -> str:
    """给审批人看的一行。"""
    obj = f"{'公共采集箱' if change['object_type'] == 'common' else 'TikTok 采集箱'} {change['object_id']}"
    kind = change["change_type"]
    if kind == "collect":
        return f"{obj}：从货源采集（{(change['after'] or {}).get('source_item_id', '')}）"
    if kind == "claim":
        return f"{obj}：由公共采集箱 {(change['after'] or {}).get('from_common_id')} 认领"
    if kind == "claim_to_shop":
        return f"{obj}：认领到店铺 {(change['after'] or {}).get('shop')}"
    parts = []
    before, after = change.get("before") or {}, change.get("after") or {}
    for field, label in (("title", "标题"), ("notes", "描述"), ("cid", "类目")):
        if field in after:
            parts.append(f"{label}「{before.get(field) or '空'}」→「{after[field]}」")
    for field, label in (("sku_prices", "售价"), ("sku_prices_cny", "货源价(CNY)")):
        for key, value in (after.get(field) or {}).items():
            parts.append(f"{key} {label} {(before.get(field) or {}).get(key)} → {value}")
    return f"{obj}：" + "；".join(parts)
