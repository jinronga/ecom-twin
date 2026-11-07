"""运营日报与异常巡检（DESIGN.md 4.2、8.1）：数据和异常判断全部由代码完成。

- 数据来源：先支持导入 ERP（领星等）导出的表格（CSV），列名在配置里映射，适配不同 ERP 的叫法；
  以后接上领星 API 只需要换数据来源，计算和报告不变
- 没有异常：按模板出日报，直接私聊负责人，不调用 agent
- 有异常：日报照发，同时建一个「异常巡检」任务，把代码算好的异常交给 agent 分析原因、出方案
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ReportSettings
from .models import TaskSource
from .store import utcnow

METRICS = ("sales_qty", "revenue", "ad_spend", "ad_sales", "inventory")


def _num(value: Any) -> float:
    text = str(value or "").replace(",", "").replace("¥", "").replace("$", "").replace("%", "").strip()
    return float(text) if text else 0.0


class DailyReport:
    def __init__(self, app: Any, settings: ReportSettings, dispatcher: Any = None):
        self.app = app
        self.cfg = settings
        self.dispatcher = dispatcher

    # ---- 导入 ----
    def import_csv(self, path: Path) -> int:
        cols = self.cfg.columns
        n = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = [c for k, c in cols.items() if k in ("date", "shop", "sku") and c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"表格缺少列：{', '.join(missing)}（在 [daily_report.columns] 里改列名映射）")
            for row in reader:
                day = str(row[cols["date"]]).strip()[:10].replace("/", "-")
                metrics = {m: _num(row.get(cols.get(m, ""), 0)) for m in METRICS}
                self.app.store.upsert_metrics(day, row[cols["shop"]].strip(), row[cols["sku"]].strip(), metrics)
                n += 1
        return n

    # ---- 计算 ----
    def _totals(self, day: str, shop: str) -> dict[str, float]:
        totals = dict.fromkeys(METRICS, 0.0)
        for r in self.app.store.metrics_for(day, shop):
            for m in METRICS:
                totals[m] += r["metrics"].get(m, 0.0)
        return totals

    def build(self, day: date) -> dict[str, Any]:
        """返回 {"text": 日报文字, "anomalies": [...]}，全部由代码计算。"""
        d = day.isoformat()
        lines, anomalies = [f"运营日报 {d}（数据来源：ERP 导出表格，由代码计算）"], []
        for shop in self.app.store.metric_shops(d):
            t = self._totals(d, shop)
            prev = [self._totals((day - timedelta(days=i)).isoformat(), shop) for i in range(1, 8)]
            prev_rev = [p["revenue"] for p in prev if p["revenue"] > 0]
            avg_rev = sum(prev_rev) / len(prev_rev) if prev_rev else 0
            acos = t["ad_spend"] / t["ad_sales"] if t["ad_sales"] else None
            tacos = t["ad_spend"] / t["revenue"] if t["revenue"] else None
            change = (t["revenue"] - avg_rev) / avg_rev if avg_rev else None
            lines.append(
                f"\n【{shop}】销售额 {t['revenue']:.2f}（较前 7 天日均 {'+' if (change or 0) >= 0 else ''}"
                f"{change:.1%}）" if change is not None else f"\n【{shop}】销售额 {t['revenue']:.2f}")
            lines.append(f"销量 {t['sales_qty']:.0f}；广告花费 {t['ad_spend']:.2f}；"
                         f"ACOS {acos:.1%}；TACOS {tacos:.1%}" if acos is not None and tacos is not None
                         else f"销量 {t['sales_qty']:.0f}；广告花费 {t['ad_spend']:.2f}")
            if acos is not None and acos > self.cfg.acos_max:
                anomalies.append({"shop": shop, "type": "ACOS 偏高", "value": round(acos, 4), "limit": self.cfg.acos_max})
            if tacos is not None and tacos > self.cfg.tacos_max:
                anomalies.append({"shop": shop, "type": "TACOS 偏高", "value": round(tacos, 4), "limit": self.cfg.tacos_max})
            if change is not None and change < -self.cfg.revenue_drop_pct / 100:
                anomalies.append({"shop": shop, "type": "销售额下滑", "value": round(change, 4),
                                  "limit": -self.cfg.revenue_drop_pct / 100})
            for r in self.app.store.metrics_for(d, shop):
                hist = [self.app.store.metric_value((day - timedelta(days=i)).isoformat(), shop, r["sku"], "sales_qty")
                        for i in range(0, 7)]
                daily = sum(hist) / 7
                inv = r["metrics"].get("inventory", 0)
                if daily > 0 and inv / daily < self.cfg.inventory_days_min:
                    anomalies.append({"shop": shop, "sku": r["sku"], "type": "库存不足",
                                      "value": round(inv / daily, 1), "limit": self.cfg.inventory_days_min})
        if len(lines) == 1:
            lines.append("当天没有导入数据")
        if anomalies:
            lines.append("\n异常：")
            lines += [f"- {a['shop']}{' ' + a['sku'] if a.get('sku') else ''}：{a['type']}（{a['value']}，阈值 {a['limit']}）"
                      for a in anomalies]
        else:
            lines.append("\n没有发现异常。")
        return {"text": "\n".join(lines), "anomalies": anomalies}

    # ---- 发送 ----
    def run(self, day: date, force: bool = False) -> dict[str, Any] | None:
        key = f"daily_report:{day.isoformat()}"
        if self.app.store.kv_get(key) and not force:
            return None
        report = self.build(day)
        owner = self.app.settings.feishu.owner_open_id
        self.app.feishu.send_text(owner, report["text"])
        self.app.store.kv_set(key, utcnow().isoformat())
        if report["anomalies"] and self.dispatcher is not None:
            task = self.app.tasks.create(f"异常巡检 {day.isoformat()}", TaskSource.SCHEDULE, "异常巡检",
                                         anchors=sorted({a.get("sku", "") for a in report["anomalies"]} - {""}),
                                         owner=owner)
            detail = "\n".join(f"- {a}" for a in report["anomalies"])
            self.dispatcher._submit(task.id, "定时巡检发现以下异常（数字由代码算出，不要改写）。请分析原因；"
                                             f"需要调整的，按对应 SOP 提交提案：\n{detail}", resume=False)
            report["task_id"] = task.id
        return report

    def due(self, now: datetime) -> date | None:
        """到点了就返回要发的日期（昨天，本地时区）。"""
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(self.cfg.timezone))
        if local.hour < self.cfg.send_hour:
            return None
        return local.date() - timedelta(days=1)
