"""组装各组件。提案状态变化 → 回写看板、同步任务状态；审批通过 → 执行器执行 → 回报结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .actions import ActionHandler, BrowserFlow, TiktokPublishViaMiaoshou
from .approval import ApprovalService
from .board import BoardSync
from .config import Settings
from .evidence import EVIDENCE_ROOT
from .executor import Executor
from .feishu import FeishuClient
from .followups import FollowupService
from .models import Proposal, ProposalStatus, TaskStatus
from .proposals import ProposalService
from .store import Store
from .tasks import TaskService

# 提案状态 -> 所属任务状态
_TASK_STATUS = {
    ProposalStatus.PENDING: TaskStatus.AWAITING_APPROVAL,
    ProposalStatus.APPROVED: TaskStatus.EXECUTING,
    ProposalStatus.EXECUTING: TaskStatus.EXECUTING,
    ProposalStatus.EXECUTED: TaskStatus.REVIEW,
    ProposalStatus.REJECTED: TaskStatus.CLOSED,
    ProposalStatus.EXPIRED: TaskStatus.CLOSED,
    ProposalStatus.FAILED: TaskStatus.FAILED,
    ProposalStatus.ROLLED_BACK: TaskStatus.FAILED,
    ProposalStatus.SHADOW: TaskStatus.CLOSED,
}


@dataclass
class App:
    settings: Settings
    store: Store
    feishu: FeishuClient
    board: BoardSync
    tasks: TaskService
    proposals: ProposalService
    approvals: ApprovalService
    executor: Executor
    followups: FollowupService
    handlers: dict[str, ActionHandler]
    miaoshou: Any = None
    staging: Any = None
    compliance: Any = None
    shadow: Any = None


def default_browser_factory(settings: Settings):
    from .platform_browser import PlatformBrowser
    return lambda platform: PlatformBrowser(platform, chrome_path=settings.browser.chrome_path)


def build_app(settings: Settings, store: Store, feishu: FeishuClient, miaoshou: Any,
              browser_factory: Any = None) -> App:
    from .compliance import ComplianceChecker
    from .staging import StagingService
    handlers: dict[str, ActionHandler] = {}
    compliance = ComplianceChecker()
    staging = StagingService(store, miaoshou, settings, compliance) if miaoshou is not None else None
    if miaoshou is not None:
        h = TiktokPublishViaMiaoshou(miaoshou, staging, compliance)
        handlers[h.action] = h

    # 功能6：内部平台浏览器流程。browser_factory 让执行器和 MCP server
    # 用同一套浏览器资料目录（也就是同一份登录状态）。
    if settings.platforms:
        flow = BrowserFlow(settings, browser_factory or default_browser_factory(settings), EVIDENCE_ROOT)
        handlers[flow.action] = flow

    board = BoardSync(store, feishu, settings.feishu)
    tasks = TaskService(store, board)

    def on_change(p: Proposal) -> None:
        board.enqueue_proposal(p)
        task = store.get_task(p.task_id)
        if task and p.status in _TASK_STATUS:
            reason = (p.result or {}).get("reason", "")
            details = "；".join((p.result or {}).get("details", []))
            tasks.set_status(
                p.task_id, _TASK_STATUS[p.status],
                conclusion=f"{p.id} {p.status.value}" + (f"（{reason}）" if reason else ""),
                blocker=details or None,
            )

    executor = Executor(store, settings.limits, handlers, on_change=on_change,
                        shop_timezones=settings.shop_timezones)

    followups = FollowupService(store, feishu, handlers, settings.feishu.owner_open_id)

    def on_approved(p: Proposal, now: datetime) -> None:
        outcome = executor.execute(p.id, now=now)
        if outcome.status == ProposalStatus.EXECUTED:
            followups.schedule(store.get_proposal(p.id), now)
        owner = p.approver or settings.feishu.owner_open_id
        text = f"{p.id} 执行结果：{outcome.status.value}"
        if outcome.reason and outcome.reason != "ok":
            text += f"（{outcome.reason}）"
        if outcome.details:
            text += "\n" + "\n".join(f"- {d}" for d in outcome.details)
        msg_id = feishu.send_text(owner, text)
        store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": text},
                          "outbound", p.task_id)

    approvals = ApprovalService(store, settings, feishu, on_approved=on_approved, on_change=on_change)
    approvals.handlers = handlers
    from .shadow import ShadowService
    shadow = ShadowService(store, settings, handlers)
    approvals.shadow = shadow
    proposals = ProposalService(store, settings, handlers)
    return App(settings, store, feishu, board, tasks, proposals, approvals, executor, followups, handlers,
               miaoshou, staging, compliance, shadow)
