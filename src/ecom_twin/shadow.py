"""影子模式（DESIGN.md 15.1）。

由分身主动发起写操作的场景（广告调价、Listing 改写等）上线前先跑影子模式：
- 配置在 [shadow] actions 里的操作类型：提案照常生成（证据、后果都由代码算），但不推送审批、不执行，状态记为「影子」
- 同时记录运营的实际操作（导入 ERP / 平台变更记录，或自动读取，例如妙手的已发布列表）
- 按操作类型统计吻合率，吻合率达标后再把这个操作类型移出影子模式
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Proposal, ProposalStatus
from .store import utcnow


class ShadowService:
    def __init__(self, store: Any, settings: Any, handlers: dict[str, Any]):
        self.store = store
        self.settings = settings
        self.handlers = handlers

    def is_shadow(self, action: str) -> bool:
        return action in self.settings.shadow_actions

    def record(self, p: Proposal) -> Proposal:
        p.status = ProposalStatus.SHADOW
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "shadow_recorded", "system")
        return p

    def objects(self, p: Proposal) -> dict[str, str]:
        """提案涉及的对象及分身的意图，例如 {采集箱ID: "上架"}。"""
        handler = self.handlers.get(p.action)
        if handler is not None and hasattr(handler, "shadow_objects"):
            return handler.shadow_objects(p)
        return {p.id: "执行"}

    # ---- 人工实际操作 ----
    def add_actual(self, action: str, shop_key: str, object_key: str, actual: str, at: datetime,
                   source: str = "manual") -> None:
        self.store.add_shadow_actual(action, shop_key, object_key, actual, at, source)

    def import_csv(self, path: Path) -> int:
        """CSV 列：action, shop_key, object, actual, at（ISO 时间）。例如从 ERP 导出的操作日志整理而来。"""
        n = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                at = datetime.fromisoformat(row["at"])
                self.add_actual(row["action"], row["shop_key"], row["object"], row["actual"], at, f"csv:{path.name}")
                n += 1
        return n

    def collect_publish(self, client: Any, now: datetime | None = None) -> int:
        """自动比对上品：看影子上品提案里的商品，人工有没有在妙手里发布。"""
        now = now or utcnow()
        shadow = [p for p in self.store.list_proposals(ProposalStatus.SHADOW) if p.action == "miaoshou.tiktok.publish"]
        if not shadow:
            return 0
        data = client.tiktok_search_collect_box(status="published", page=1, size=200)
        rows = data if isinstance(data, list) else next(
            (data[k] for k in ("list", "records", "data", "items", "detailList") if isinstance(data, dict) and k in data), [])
        published = {str(r.get("collectBoxDetailId")) for r in rows}
        added = 0
        for p in shadow:
            for obj in self.objects(p):
                if obj in published and not self.store.shadow_actuals(p.action, p.shop_key, obj):
                    self.add_actual(p.action, p.shop_key, obj, "上架", now, "miaoshou:published")
                    added += 1
        return added

    # ---- 吻合率 ----
    def report(self, now: datetime | None = None, window_days: float = 3) -> dict[str, Any]:
        """窗口期内有人工记录就比对；过了窗口期还没有记录，视为人工「未操作」。"""
        now = now or utcnow()
        summary: dict[str, dict] = {}
        details = []
        for p in self.store.list_proposals(ProposalStatus.SHADOW):
            s = summary.setdefault(p.action, {"action": p.action, "proposals": 0, "compared": 0, "matched": 0})
            s["proposals"] += 1
            start = p.created_at or now
            end = start + timedelta(days=window_days)
            for obj, proposed in self.objects(p).items():
                actuals = [a for a in self.store.shadow_actuals(p.action, p.shop_key, obj)
                           if start <= datetime.fromisoformat(a["at"]) <= end]
                if actuals:
                    actual = actuals[-1]["actual"]
                elif now > end:
                    actual = "未操作"
                else:
                    continue  # 还在窗口期，等人工操作
                matched = actual == proposed
                s["compared"] += 1
                s["matched"] += int(matched)
                details.append({"proposal_id": p.id, "action": p.action, "object": obj,
                                "proposed": f"{obj}：{proposed}", "actual": actual, "matched": matched})
        for s in summary.values():
            s["match_rate"] = s["matched"] / s["compared"] if s["compared"] else None
        return {"summary": list(summary.values()), "mismatches": [d for d in details if not d["matched"]],
                "details": details}
