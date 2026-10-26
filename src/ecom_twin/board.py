"""任务看板回写（DESIGN.md 11.5）。

- 本地数据库为准，多维表格只是展示视图；写表失败只让看板延迟，不影响任务执行
- 更新先进本地队列，按记录合并，再按每秒上限批量写入；失败指数退避重试
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .config import FeishuSettings
from .feishu import FeishuClient, FeishuError
from .models import Proposal, Step, Task
from .store import Store, utcnow


def _ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def task_fields(task: Task) -> dict[str, Any]:
    """任务表字段（中文列名与 DESIGN.md 11.2 一致）。"""
    fields = {
        "任务编号": task.id,
        "标题": task.title,
        "状态": task.status.value,
        "当前步骤": task.current_step,
        "来源": task.source.value,
        "场景": task.scene,
        "店铺": task.shop_key,
        "锚点": ", ".join(task.anchors),
        "运行时 / 模型": task.runtime,
        "token 用量": (task.tokens_in + task.tokens_out) or None,
        "切换记录": task.switch_log,
        "结论": task.conclusion,
        "卡点": task.blocker,
        "开始时间": _ms(task.created_at),
        "最后心跳": _ms(task.last_heartbeat),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}


def step_fields(step: Step) -> dict[str, Any]:
    fields = {
        "所属任务": step.task_id,
        "步骤名": step.name,
        "状态": step.status.value,
        "摘要": step.summary,
        "错误信息": step.error,
        "开始时间": _ms(step.started_at),
        "结束时间": _ms(step.ended_at),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}


def proposal_fields(p: Proposal) -> dict[str, Any]:
    fields = {
        "提案号": p.id,
        "所属任务": p.task_id,
        "店铺": p.shop_key,
        "操作类型": p.action,
        "状态": p.status.value,
        "风险等级": p.risk,
        "执行方式": p.method.value,
        "理由": p.rationale,
        "审批人": p.approver,
        "拒绝原因": p.reject_reason.value if p.reject_reason else "",
        "拒绝备注": p.reject_note,
        "有效期": _ms(p.expires_at),
        "执行结果": (p.result or {}).get("reason", ""),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}


class BoardSync:
    def __init__(self, store: Store, feishu: FeishuClient, settings: FeishuSettings):
        self.store = store
        self.feishu = feishu
        self.settings = settings
        self.tables = {
            "task": settings.tasks_table_id,
            "step": settings.steps_table_id,
            "proposal": settings.proposals_table_id,
        }

    def enqueue_task(self, task: Task) -> None:
        self.store.enqueue_board("task", task.id, task_fields(task))

    def enqueue_step(self, step: Step) -> None:
        self.store.enqueue_board("step", str(step.id), step_fields(step))

    def enqueue_proposal(self, p: Proposal) -> None:
        self.store.enqueue_board("proposal", p.id, proposal_fields(p))

    def flush(self, now: datetime | None = None, seconds: float = 1.0) -> int:
        """写入到期的队列项，本次最多写 base_max_writes_per_sec × seconds 条。返回成功条数。"""
        if not self.settings.board_enabled:
            return 0
        now = now or utcnow()
        budget = max(1, int(self.settings.base_max_writes_per_sec * seconds))
        written = 0
        for item in self.store.due_board_items(now, budget):
            entity, entity_id, fields = item["entity"], item["entity_id"], item["fields"]
            table = self.tables.get(entity)
            if not table:
                self.store.board_done(entity, entity_id)  # 该表未配置，直接丢弃
                continue
            try:
                record_id = self.store.board_record_id(entity, entity_id)
                if record_id:
                    self.feishu.base_update(self.settings.base_app_token, table, record_id, fields)
                else:
                    record_id = self.feishu.base_create(self.settings.base_app_token, table, fields)
                    self.store.set_board_record_id(entity, entity_id, record_id)
                self.store.board_done(entity, entity_id)
                written += 1
            except FeishuError:
                backoff = min(300, 2 ** (item["attempts"] + 1))
                self.store.board_retry_later(entity, entity_id, now + timedelta(seconds=backoff))
        return written
