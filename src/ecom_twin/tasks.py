"""任务服务：建单、状态流转、步骤上报、心跳。每次变化都进看板回写队列。

agent 不直接写多维表格，而是通过 report_step 上报，由运行时统一转写（DESIGN.md 11.5）。
"""

from __future__ import annotations

from datetime import datetime

from .board import BoardSync
from .models import Step, StepStatus, Task, TaskSource, TaskStatus
from .store import Store, utcnow


class TaskService:
    def __init__(self, store: Store, board: BoardSync):
        self.store = store
        self.board = board

    def create(self, title: str, source: TaskSource, scene: str, shop_key: str = "",
               anchors: list[str] | None = None, owner: str = "") -> Task:
        task = Task(id=self.store.next_id("T"), title=title, source=source, scene=scene,
                    shop_key=shop_key, anchors=anchors or [], owner=owner)
        self.store.save_task(task)
        self.store.audit("task", task.id, "created", "system", {"source": source.value})
        self.board.enqueue_task(task)
        return task

    def _save(self, task: Task) -> Task:
        self.store.save_task(task)
        self.board.enqueue_task(task)
        return task

    def set_status(self, task_id: str, status: TaskStatus, *, conclusion: str | None = None,
                   blocker: str | None = None) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        task.status = status
        if conclusion is not None:
            task.conclusion = conclusion
        if blocker is not None:
            task.blocker = blocker
        self.store.audit("task", task_id, f"status:{status.value}", "system")
        return self._save(task)

    def start_step(self, task_id: str, name: str) -> Step:
        step = self.store.add_step(Step(id=None, task_id=task_id, name=name, status=StepStatus.RUNNING))
        task = self.store.get_task(task_id)
        if task is not None:
            task.current_step = name
            task.last_heartbeat = utcnow()
            self._save(task)
        self.board.enqueue_step(step)
        return step

    def finish_step(self, step: Step, status: StepStatus = StepStatus.SUCCESS, summary: str = "",
                    error: str = "", evidence: str = "") -> Step:
        step.status = status
        step.summary = summary
        step.error = error
        if evidence:
            step.evidence = evidence
        step.ended_at = utcnow()
        self.store.update_step(step)
        self.board.enqueue_step(step)
        return step

    def heartbeat(self, task_id: str, now: datetime | None = None) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            # MCP server 可能被单独拉起（任务还没建），此时没有心跳可更新。
            return
        task.last_heartbeat = now or utcnow()
        self._save(task)
