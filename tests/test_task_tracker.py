"""测试功能2：运行时任务跟踪器。"""

from ecom_twin.task_tracker import TaskTracker, TaskRun


def test_tracker_start_and_finish():
    tracker = TaskTracker()
    run = tracker.start("task1", "测试提示", resume=False, primary_runtime="claude")
    assert run.task_id == "task1"
    assert run.prompt == "测试提示"
    assert run.resume is False
    assert run.primary_runtime == "claude"
    assert run.retries == 0
    assert run.switches == 0
    assert run.outcome is None

    tracker.finish(run, outcome="success", conclusion="完成了")
    assert run.outcome == "success"
    assert run.conclusion == "完成了"
    assert run.end_time is not None
    assert run.duration_sec() > 0


def test_tracker_records_retries_and_switches():
    tracker = TaskTracker()
    run = tracker.start("task2", "测试重试", resume=False, primary_runtime="claude")
    run.retries = 2
    run.switches = 1
    run.runtime_used = "cursor"
    run.run_id = "session123"

    tracker.finish(run, outcome="success")
    assert run.retries == 2
    assert run.switches == 1
    assert run.runtime_used == "cursor"
    assert run.run_id == "session123"


def test_tracker_get_task_runs():
    tracker = TaskTracker()
    run1 = tracker.start("task3", "第一轮", resume=False, primary_runtime="claude")
    tracker.finish(run1, outcome="success")

    run2 = tracker.start("task3", "第二轮", resume=True, primary_runtime="claude")
    tracker.finish(run2, outcome="failed", error="超时")

    runs = tracker.get_task_runs("task3")
    assert len(runs) == 2
    assert runs[0].prompt == "第一轮"
    assert runs[1].prompt == "第二轮"


def test_tracker_summary():
    tracker = TaskTracker()

    run1 = tracker.start("task4", "成功", resume=False, primary_runtime="claude")
    run1.runtime_used = "claude"
    tracker.finish(run1, outcome="success")

    run2 = tracker.start("task5", "失败", resume=False, primary_runtime="cursor")
    run2.runtime_used = "cursor"
    run2.retries = 2
    run2.switches = 1
    tracker.finish(run2, outcome="failed")

    summary = tracker.summary()
    assert summary["total"] == 2
    assert summary["outcomes"]["success"] == 1
    assert summary["outcomes"]["failed"] == 1
    assert summary["total_retries"] == 2
    assert summary["total_switches"] == 1
    assert "claude" in summary["runtime_distribution"]
    assert "cursor" in summary["runtime_distribution"]


def test_task_run_to_dict():
    tracker = TaskTracker()
    run = tracker.start("task6", "测试序列化" * 50, resume=False, primary_runtime="claude")
    run.runtime_used = "claude"
    run.run_id = "session456"
    run.retries = 1
    tracker.finish(run, outcome="success", conclusion="完成" * 200)

    d = run.to_dict()
    assert d["task_id"] == "task6"
    assert len(d["prompt"]) <= 200  # 应该被截断
    assert d["runtime_used"] == "claude"
    assert d["run_id"] == "session456"
    assert d["retries"] == 1
    assert d["outcome"] == "success"
    assert len(d["conclusion"]) <= 500  # 应该被截断
    assert d["duration_sec"] > 0
