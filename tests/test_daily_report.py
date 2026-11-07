"""运营日报与异常巡检：导入 ERP 表格（列名可映射）、代码计算、无异常不调 agent、有异常建任务。"""

from datetime import date, datetime, timezone

import pytest

from ecom_twin.config import ReportSettings
from ecom_twin.daily_report import DailyReport

HEADER = "日期,店铺,SKU,销量,销售额,广告花费,广告销售额,可售库存\n"


def history(rows_per_day, start_day=9, end_day=15, revenue=1000.0, qty=50):
    lines = []
    for d in range(start_day, end_day + 1):
        lines.append(f"2026-10-{d:02d},tiktok:10001,A-01,{qty},{revenue},100,800,5000")
    return lines


def write(tmp_path, lines, header=HEADER, name="erp.csv"):
    f = tmp_path / name
    f.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return f


class FakeDispatcher:
    def __init__(self):
        self.submitted = []

    def _submit(self, task_id, prompt, resume):
        self.submitted.append((task_id, prompt))


def test_normal_day_sends_template_only(env, tmp_path):
    r = DailyReport(env.app, ReportSettings(), FakeDispatcher())
    assert r.import_csv(write(tmp_path, history(1) + ["2026-10-16,tiktok:10001,A-01,50,1000,100,800,5000"])) == 8
    result = r.run(date(2026, 10, 16))
    assert "没有发现异常" in result["text"] and "ACOS 12.5%" in result["text"]
    assert "task_id" not in result and r.dispatcher.submitted == []
    assert env.feishu.sent[-1]["to"] == "ou_boss"
    assert r.run(date(2026, 10, 16)) is None                       # 同一天不重复发


def test_anomalies_create_task_for_agent(env, tmp_path):
    d = FakeDispatcher()
    r = DailyReport(env.app, ReportSettings(), d)
    # 当天：销售额腰斩、ACOS 75%、库存只够 2 天
    r.import_csv(write(tmp_path, history(1) + ["2026-10-16,tiktok:10001,A-01,50,400,300,400,100"]))
    result = r.run(date(2026, 10, 16))
    types = {a["type"] for a in result["anomalies"]}
    assert types == {"ACOS 偏高", "TACOS 偏高", "销售额下滑", "库存不足"}
    task = env.app.store.get_task(result["task_id"])
    assert task.scene == "异常巡检" and task.anchors == ["A-01"]
    assert "数字由代码算出" in d.submitted[0][1]


def test_column_mapping_for_other_erp(env, tmp_path):
    cfg = ReportSettings(columns={**ReportSettings().columns, "date": "Date", "shop": "Store", "sku": "Seller SKU",
                                  "revenue": "Sales"})
    header = "Date,Store,Seller SKU,销量,Sales,广告花费,广告销售额,可售库存\n"
    r = DailyReport(env.app, cfg)
    assert r.import_csv(write(tmp_path, ["2026/10/16,tiktok:10001,B-02,5,99.5,0,0,10"], header)) == 1
    assert env.app.store.metric_value("2026-10-16", "tiktok:10001", "B-02", "revenue") == 99.5
    with pytest.raises(ValueError, match="缺少列"):
        DailyReport(env.app, ReportSettings()).import_csv(write(tmp_path, ["x"], "a,b\n", "bad.csv"))


def test_due_uses_local_time():
    r = DailyReport(None, ReportSettings(send_hour=9, timezone="Asia/Shanghai"))
    assert r.due(datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc)) is None          # 上海 08:30
    assert r.due(datetime(2026, 10, 16, 1, 30, tzinfo=timezone.utc)) == date(2026, 10, 15)  # 上海 09:30
