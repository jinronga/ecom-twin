"""运行时任务跟踪器——自动记录每次任务运行的详细数据。

DESIGN.md 功能2：在每次 run_task 进入和退出时自动记录启动时间、结束时间、使用的底座、
run_id（会话 ID）、重试次数、切底座次数、最终结果（成功/失败/待审批/等待回复）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import TaskStatus


@dataclass
class TaskRun:
    """单次任务运行的记录。"""
    task_id: str
    start_time: float  # Unix timestamp
    prompt: str
    resume: bool
    primary_runtime: str
    retries: int = 0
    switches: int = 0
    runtime_used: str = ""
    run_id: str = ""
    end_time: Optional[float] = None
    outcome: Optional[str] = None  # "success", "failed", "awaiting_approval", "waiting_reply"
    error: Optional[str] = None
    conclusion: Optional[str] = None

    def duration_sec(self) -> float:
        """运行时长（秒），如果未结束返回 0。"""
        if self.end_time is None:
            return 0.0
        return self.end_time - self.start_time

    def to_dict(self) -> dict:
        """转为字典以便存储或导出。"""
        return {
            "task_id": self.task_id,
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "end_time": datetime.fromtimestamp(self.end_time).isoformat() if self.end_time else None,
            "duration_sec": self.duration_sec(),
            "prompt": self.prompt[:200],  # 截断避免过长
            "resume": self.resume,
            "primary_runtime": self.primary_runtime,
            "runtime_used": self.runtime_used,
            "run_id": self.run_id,
            "retries": self.retries,
            "switches": self.switches,
            "outcome": self.outcome,
            "error": self.error[:500] if self.error else None,
            "conclusion": self.conclusion[:500] if self.conclusion else None,
        }


@dataclass
class TaskTracker:
    """任务运行跟踪器。

    用法：
        tracker = TaskTracker()
        run = tracker.start(task_id, prompt, resume, primary_runtime)
        try:
            # ... 执行任务 ...
            run.retries += 1  # 重试时增加计数
            run.switches += 1  # 切底座时增加计数
            run.run_id = session_id  # 记录会话 ID
            run.runtime_used = actual_runtime  # 记录实际使用的底座
        finally:
            tracker.finish(run, outcome="success", conclusion="...")
    """
    runs: list[TaskRun] = field(default_factory=list)
    current_runs: dict[str, TaskRun] = field(default_factory=dict)  # task_id -> TaskRun

    def start(self, task_id: str, prompt: str, resume: bool, primary_runtime: str) -> TaskRun:
        """开始记录一次新的任务运行。"""
        run = TaskRun(
            task_id=task_id,
            start_time=time.time(),
            prompt=prompt,
            resume=resume,
            primary_runtime=primary_runtime,
        )
        self.current_runs[task_id] = run
        self.runs.append(run)
        return run

    def finish(
        self,
        run: TaskRun,
        outcome: str,
        error: Optional[str] = None,
        conclusion: Optional[str] = None,
    ) -> None:
        """结束一次任务运行的记录。"""
        run.end_time = time.time()
        run.outcome = outcome
        run.error = error
        run.conclusion = conclusion
        # 清理 current_runs
        if run.task_id in self.current_runs and self.current_runs[run.task_id] is run:
            del self.current_runs[run.task_id]

    def get_task_runs(self, task_id: str) -> list[TaskRun]:
        """获取某任务的所有运行记录。"""
        return [r for r in self.runs if r.task_id == task_id]

    def get_recent_runs(self, limit: int = 10) -> list[TaskRun]:
        """获取最近的运行记录（按开始时间倒序）。"""
        return sorted(self.runs, key=lambda r: r.start_time, reverse=True)[:limit]

    def summary(self) -> dict:
        """生成运行统计摘要。"""
        total = len(self.runs)
        if total == 0:
            return {"total": 0}

        outcomes = {}
        total_retries = 0
        total_switches = 0
        total_duration = 0.0
        runtime_counts = {}

        for run in self.runs:
            outcomes[run.outcome or "unknown"] = outcomes.get(run.outcome or "unknown", 0) + 1
            total_retries += run.retries
            total_switches += run.switches
            total_duration += run.duration_sec()
            if run.runtime_used:
                runtime_counts[run.runtime_used] = runtime_counts.get(run.runtime_used, 0) + 1

        return {
            "total": total,
            "outcomes": outcomes,
            "total_retries": total_retries,
            "total_switches": total_switches,
            "avg_duration_sec": total_duration / total if total > 0 else 0.0,
            "runtime_distribution": runtime_counts,
        }
