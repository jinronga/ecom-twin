"""任务服务：建单、状态流转、步骤上报、心跳。每次变化都进看板回写队列。

agent 不直接写多维表格，而是通过 report_step 上报，由运行时统一转写（DESIGN.md 11.5）。
"""

from __future__ import annotations

import re
from datetime import datetime

from .board import BoardSync
from .models import Step, StepStatus, Task, TaskSource, TaskStatus
from .store import Store, utcnow

PENDING_SCENE = "待定"
TITLE_MAX = 40
# 截断时优先断在这些字后面。不含半角冒号和句点：「tiktok:7300」「a.com」里的不是句子边界
_TITLE_BREAKS = "，。；：！？、,;!? "
_TITLE_OPENERS = "「『“‘\"'（([【《<"


def title_from_text(text: str, limit: int = TITLE_MAX) -> str:
    """用消息原话做任务标题。

    超长时断在最近的标点或空格上，并以「…」收尾，不留半截词和没闭合的引号
    （原来直接取前 40 个字，会出现「买家用英文写的：「My or」这种标题）。
    """
    flat = re.sub(r"\s+", " ", (text or "").strip())
    if len(flat) <= limit:
        return flat
    head = flat[:limit]
    cut = max(head.rfind(ch) for ch in _TITLE_BREAKS)
    if cut >= limit // 2:  # 边界太靠前就不按边界断，免得标题只剩几个字
        head = head[:cut]
    else:  # 没有合适的边界：去掉末尾那个被截断的英文单词
        head = re.sub(r"[A-Za-z0-9_-]+$", "", head) or head
    head = head.rstrip(_TITLE_BREAKS + _TITLE_OPENERS)
    # 截断后还有没闭合的开引号，就退到引号前面
    for opener, closer in (("「", "」"), ("『", "』"), ("“", "”"), ("（", "）"), ("(", ")"), ("【", "】"), ("《", "》")):
        if head.count(opener) > head.count(closer):
            cut_at = head.rfind(opener)
            # 引号在最前面（整句都在引号里）：去掉这个开引号，不留半个引号
            head = head[:cut_at].rstrip(_TITLE_BREAKS + _TITLE_OPENERS) if cut_at > 0 else head[cut_at + 1:]
    return head + "…"


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

    def fill_context(self, task_id: str, *, shop_key: str = "", scene: str = "") -> Task | None:
        """回填建单时还不知道的店铺和场景。

        从消息建单时只有一句原话，店铺为空、场景是「待定」；agent 干活时调了带店铺的工具、加载了场景 SOP，
        代码就从这些调用里认出来补上，看板、任务详情、成本按店铺 / 场景统计才有值。只补空缺，不改已有的。
        """
        task = self.store.get_task(task_id)
        if task is None:
            return None
        filled = {}
        if shop_key and not task.shop_key:
            task.shop_key = filled["shop_key"] = shop_key
        if scene and task.scene in ("", PENDING_SCENE):
            task.scene = filled["scene"] = scene
        if not filled:
            return task
        self.store.audit("task", task_id, "context_filled", "system", filled)
        return self._save(task)

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
