"""任务看板回写（DESIGN.md 11.5）。

- 本地数据库为准，多维表格只是展示视图；写表失败只让看板延迟，不影响任务执行
- 更新先进本地队列，按记录合并，再按每秒上限批量写入；失败指数退避重试
"""

from __future__ import annotations

import json
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
        "transcript": task.transcript,
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


# 增量导出的日志表：本地表名 -> (看板实体名, 字段映射函数)
def _message_fields(r: dict) -> dict:
    return {"所属任务": r["task_id"], "消息ID": r["message_id"], "会话": r["chat_id"], "发送人": r["sender_id"],
            "内容": (r["text"] or "")[:1000], "合并方式": r["layer"], "是否唤醒": "唤醒" if r["drive"] else "静音",
            "时间": r["created_at"]}


def _execution_fields(r: dict) -> dict:
    return {"提案号": r["proposal_id"], "店铺": r["shop_key"], "结果": r["outcome"],
            "回读校验": {1: "通过", 0: "未通过"}.get(r["verified"], ""), "请求": (r["request"] or "")[:1000],
            "响应": (r["response"] or "")[:1000], "时间": r["created_at"]}


def _outbound_fields(r: dict) -> dict:
    return {"所属任务": r["task_id"], "类型": r["kind"], "发到": r["delivered_to"], "会话": r["chat_id"],
            "内容": (r["text"] or "")[:1000], "时间": r["created_at"]}


def _recall_fields(r: dict) -> dict:
    return {"所属任务": r["task_id"] or "", "方式": "读取" if r.get("command") == "read" else "检索",
            "关键词": r["query"], "命中": r["hits"], "时间": r["created_at"]}


def _review_fields(r: dict) -> dict:
    return {"提案号": r["proposal_id"], "类型": r["kind"], "计划时间": r["due_at"],
            "状态": "已完成" if r["done"] else "待处理", "备注": r["note"] or ""}


LOG_TABLES = {
    "task_messages": ("message", _message_fields),
    "executions": ("execution", _execution_fields),
    "outbound_messages": ("outbound", _outbound_fields),
    "recall_log": ("recall", _recall_fields),
    "followups": ("review", _review_fields),
}

# 人在看板「状态」列里可以改成这些值，分身会照做（DESIGN.md 11.5 反向操作、11.6）
REVERSE_STATUS = ("已关闭", "重试")


class BoardSync:
    def __init__(self, store: Store, feishu: FeishuClient, settings: FeishuSettings):
        self.store = store
        self.feishu = feishu
        self.settings = settings
        self.tables = {
            "task": settings.tasks_table_id,
            "step": settings.steps_table_id,
            "proposal": settings.proposals_table_id,
            "message": settings.messages_table_id,
            "execution": settings.executions_table_id,
            "outbound": settings.outbound_table_id,
            "recall": settings.recalls_table_id,
            "review": settings.reviews_table_id,
        }
        # 人在看板上把任务改成「重试」时的回调（调度器用它续跑任务），组装后再设置
        self.on_retry = lambda task_id: None

    def enqueue_task(self, task: Task) -> None:
        self.store.enqueue_board("task", task.id, task_fields(task))

    def enqueue_step(self, step: Step) -> None:
        self.store.enqueue_board("step", str(step.id), step_fields(step))

    def enqueue_proposal(self, p: Proposal) -> None:
        self.store.enqueue_board("proposal", p.id, proposal_fields(p))

    def export_logs(self) -> int:
        """把消息、执行记录、代发、召回、复盘这几张本地日志表的新行放进回写队列（按游标增量）。"""
        if not self.settings.board_enabled:
            return 0
        queued = 0
        for table, (entity, to_fields) in LOG_TABLES.items():
            if not self.tables.get(entity):
                continue
            if table == "followups":
                # 复盘行创建后还会被标记完成：按内容变化导出，而不是按游标
                for r in self.store.rows_after(table, 0, limit=100000):
                    if not r["kind"].startswith("review_"):
                        continue
                    fields = to_fields(r)
                    digest = json.dumps(fields, ensure_ascii=False, sort_keys=True)
                    if self.store.kv_get(f"board_hash:review:{r['id']}") != digest:
                        self.store.enqueue_board(entity, str(r["id"]), fields)
                        self.store.kv_set(f"board_hash:review:{r['id']}", digest)
                        queued += 1
                continue
            cursor = int(self.store.kv_get(f"board_cursor:{table}") or 0)
            rows = self.store.rows_after(table, cursor)
            for r in rows:
                self.store.enqueue_board(entity, str(r["id"]), to_fields(r))
                queued += 1
            if rows:
                self.store.kv_set(f"board_cursor:{table}", str(rows[-1]["id"]))
        return queued

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
                if entity == "task" and "状态" in fields:  # 记下我们写过的状态，用来识别人在表里的改动
                    self.store.kv_set(f"board_written:task:{entity_id}", fields["状态"])
                self.store.board_done(entity, entity_id)
                written += 1
            except FeishuError:
                backoff = min(300, 2 ** (item["attempts"] + 1))
                self.store.board_retry_later(entity, entity_id, now + timedelta(seconds=backoff))
        return written

    def pull_changes(self, tasks: Any) -> list[str]:
        """读任务表，找出人改过的状态（和我们最后写入的不同），照做：
        已关闭 → 关闭任务；重试 → 续跑任务。返回处理过的任务号。"""
        if not self.settings.board_enabled or not self.tables["task"]:
            return []
        handled = []
        try:
            records = self.feishu.base_list(self.settings.base_app_token, self.tables["task"])
        except FeishuError:
            return []
        for rec in records:
            mapped = self.store.board_entity_for_record(rec["record_id"])
            if not mapped or mapped[0] != "task":
                continue
            task_id = mapped[1]
            remote = rec["fields"].get("状态")
            remote = remote[0] if isinstance(remote, list) and remote else remote
            written = self.store.kv_get(f"board_written:task:{task_id}")
            if not remote or remote == written or remote not in REVERSE_STATUS:
                continue
            self.store.kv_set(f"board_written:task:{task_id}", remote)
            self.store.audit("task", task_id, "board_reverse", "board", {"状态": remote, "之前": written})
            if remote == "已关闭":
                from .models import TaskStatus
                tasks.set_status(task_id, TaskStatus.CLOSED, conclusion="负责人在看板上关闭了任务")
            elif remote == "重试":
                self.on_retry(task_id)
            handled.append(task_id)
        return handled
