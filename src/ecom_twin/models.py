"""核心数据模型：任务、步骤、提案、执行记录。字段与 DESIGN.md 7.2、11.2、11.3 对应。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "待处理"
    QUEUED = "排队中"
    RUNNING = "进行中"
    WAITING = "等待回复"
    AWAITING_APPROVAL = "待审批"
    EXECUTING = "执行中"
    REVIEW = "待复盘"
    CLOSED = "已关闭"
    FAILED = "失败"


class TaskSource(str, Enum):
    MESSAGE = "飞书消息"
    SCHEDULE = "定时任务"
    SELF_DM = "本人私聊"


class StepStatus(str, Enum):
    RUNNING = "进行中"
    SUCCESS = "成功"
    FAILED = "失败"
    SKIPPED = "跳过"


class ProposalStatus(str, Enum):
    PENDING = "待审批"
    APPROVED = "已批准"
    REJECTED = "已拒绝"
    EXPIRED = "已过期"
    EXECUTING = "执行中"
    EXECUTED = "已执行"
    FAILED = "执行失败"
    ROLLED_BACK = "已回滚"


class RejectReason(str, Enum):
    DATA = "数据不对"
    JUDGEMENT = "判断不对"
    TIMING = "时机不对"
    PREFERENCE = "违反偏好"
    OTHER = "其他"


class ExecMethod(str, Enum):
    API = "API"
    IMPORT_FILE = "导入文件"
    CHECKLIST = "人工清单"


@dataclass
class Task:
    id: str
    title: str
    source: TaskSource
    scene: str
    status: TaskStatus = TaskStatus.PENDING
    shop_key: str = ""
    anchors: list[str] = field(default_factory=list)
    owner: str = ""
    current_step: str = ""
    runtime: str = ""
    conclusion: str = ""
    blocker: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_heartbeat: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    session_id: str = ""        # 底座会话 ID，用于续跑
    switch_log: str = ""        # 因失败切换底座的记录
    origin_message_id: str = ""  # 触发任务的飞书消息，汇报回复到这条消息的 thread
    origin_chat_id: str = ""    # 触发任务的飞书会话 ID，用于拉取上下文


@dataclass
class Step:
    id: int | None
    task_id: str
    name: str
    status: StepStatus
    summary: str = ""
    error: str = ""
    evidence: str = ""          # 证据文件路径（如截图）
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass
class Proposal:
    """操作提案。

    - target / desired：agent 给出的操作意图
    - current：提案生成时由代码读取的当前值，执行前用于漂移校验
    - evidence / consequences：全部由代码计算（DESIGN.md 7.3 硬性要求）
    """

    id: str
    task_id: str
    shop_key: str
    action: str
    target: dict[str, Any]
    desired: dict[str, Any]
    rationale: str
    current: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    consequences: dict[str, Any] = field(default_factory=dict)
    risk: str = "中"
    method: ExecMethod = ExecMethod.API
    status: ProposalStatus = ProposalStatus.PENDING
    expires_at: datetime | None = None
    approver: str = ""
    decided_at: datetime | None = None
    reject_reason: RejectReason | None = None
    reject_note: str = ""
    card_message_id: str = ""
    reminded: bool = False
    executed_at: datetime | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    @property
    def platform(self) -> str:
        return self.shop_key.split(":", 1)[0]

    @property
    def shop_id(self) -> str:
        return self.shop_key.split(":", 1)[1] if ":" in self.shop_key else ""
