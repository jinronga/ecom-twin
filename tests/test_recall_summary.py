"""召回记录：按任务汇总（对照数字分身「按轮次」）、超过保留期自动清理（默认 90 天）。"""

from datetime import timedelta

from ecom_twin.config import settings_from_raw
from ecom_twin.store import Store, utcnow


def test_summary_counts_reads_searches_and_misses():
    store = Store(":memory:")
    store.log_recall("T-1", "大促日历", ["大促日历"], command="read")
    store.log_recall("T-1", "TikTok 标题禁用词", [], command="search")
    store.log_recall("T-2", "改价节奏", ["改价节奏"], command="read")
    by_task = {s["task_id"]: s for s in store.recall_summary()}
    assert (by_task["T-1"]["calls"], by_task["T-1"]["reads"], by_task["T-1"]["searches"], by_task["T-1"]["misses"]) == (2, 1, 1, 1)
    assert by_task["T-2"]["misses"] == 0


def test_prune_keeps_recent_and_zero_means_forever():
    store = Store(":memory:")
    store.log_recall("T-old", "x", [], command="read")
    store.log_recall("T-new", "y", [], command="read")
    store.conn.execute("UPDATE recall_log SET created_at=? WHERE task_id='T-old'",
                       ((utcnow() - timedelta(days=100)).isoformat(),))
    assert store.prune_recalls(0) == 0 and len(store.list_recalls()) == 2      # 0 = 不删
    assert store.prune_recalls(90) == 1
    assert [r["task_id"] for r in store.list_recalls()] == ["T-new"]


def test_retention_setting_defaults_to_90_days():
    assert settings_from_raw({}).recall_retention_days == 90
    assert settings_from_raw({"storage": {"recall_retention_days": 0}}).recall_retention_days == 0


def test_prune_job_is_scheduled(env):
    from ecom_twin.scheduler import build_jobs
    assert "recall_prune" in [j.name for j in build_jobs(env.app, memory=None, miaoshou=None)]
