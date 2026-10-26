"""提案服务：agent 只提交操作意图和理由，证据和后果由代码计算（DESIGN.md 7.3）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .actions import ActionHandler
from .config import Settings
from .models import Proposal, ProposalStatus
from .numbers import check_rationale_numbers
from .store import Store, utcnow


class ProposalRejected(Exception):
    """提案在提交阶段就被拒收（不进入审批）。"""


class ProposalService:
    def __init__(self, store: Store, settings: Settings, handlers: dict[str, ActionHandler]):
        self.store = store
        self.settings = settings
        self.handlers = handlers

    def submit(
        self,
        task_id: str,
        shop_key: str,
        action: str,
        target: dict[str, Any],
        desired: dict[str, Any],
        rationale: str,
        now: datetime | None = None,
    ) -> Proposal:
        if action not in self.handlers:
            raise ProposalRejected(f"不支持的操作类型：{action}")
        handler = self.handlers[action]
        now = now or utcnow()
        proposal = Proposal(
            id=self.store.next_id("P"),
            task_id=task_id,
            shop_key=shop_key,
            action=action,
            target=target,
            desired=desired,
            rationale=rationale,
            method=handler.method,
            status=ProposalStatus.PENDING,
            expires_at=now + timedelta(hours=self.settings.expire_hours),
        )
        handler.prepare(proposal)

        unknown = check_rationale_numbers(rationale, {"evidence": proposal.evidence, "consequences": proposal.consequences, "target": target})
        if unknown:
            self.store.audit("proposal", proposal.id, "rejected_at_submit", "system", {"unknown_numbers": unknown})
            raise ProposalRejected(f"理由中的数字找不到数据出处：{', '.join(unknown)}")

        # 提交时先按硬上限筛一遍，注定执行不了的提案不打扰审批人；执行前执行器还会再查一次
        violations = handler.check_limits(proposal, self.settings.limits)
        if violations:
            self.store.audit("proposal", proposal.id, "rejected_at_submit", "system", {"violations": violations})
            raise ProposalRejected("超出硬上限：" + "；".join(violations))

        self.store.save_proposal(proposal)
        self.store.audit("proposal", proposal.id, "submitted", "agent", {"action": action})
        return proposal
