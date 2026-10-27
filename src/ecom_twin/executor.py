"""执行器：唯一能调用写接口的组件，只执行审批通过的提案（DESIGN.md 3、7.1）。

执行顺序：有效期校验 → 漂移校验 → 硬上限校验 → snapshot → apply → verify → 留痕；
verify 失败时尝试 rollback。硬上限在这里强制，agent 无法绕过（7.4）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .actions import ActionHandler
from .adapters.base import AdapterError
from .config import Limits
from .models import Proposal, ProposalStatus
from .store import Store, utcnow


@dataclass
class ExecutionOutcome:
    proposal_id: str
    status: ProposalStatus
    reason: str = ""
    details: list[str] = field(default_factory=list)


def check_common_limits(proposal: Proposal, limits: Limits, store: Store, now: datetime) -> list[str]:
    """所有写操作通用的硬上限：店铺白名单、单店每日写操作次数。"""
    violations = []
    # 内部平台（platform:<key>）的白名单是 [platforms] 配置本身，由 BrowserFlow.check_limits 检查
    if not proposal.shop_key.startswith("platform:") and proposal.shop_key not in limits.allowed_shops:
        violations.append(f"店铺 {proposal.shop_key} 不在可操作白名单内")
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used = store.count_writes_since(proposal.shop_key, since)
    if used >= limits.max_daily_writes_per_shop:
        violations.append(f"店铺 {proposal.shop_key} 今日写操作已达上限 {limits.max_daily_writes_per_shop}")
    return violations


class Executor:
    def __init__(
        self,
        store: Store,
        limits: Limits,
        handlers: dict[str, ActionHandler],
        on_change: Callable[[Proposal], None] | None = None,
    ):
        self.store = store
        self.limits = limits
        self.handlers = handlers
        self.on_change = on_change or (lambda p: None)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _finish(self, p: Proposal, status: ProposalStatus, reason: str, details: list[str] | None = None,
                **result: Any) -> ExecutionOutcome:
        p.status = status
        p.result = {"reason": reason, "details": details or [], **result}
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, f"execution_{reason}", "executor", p.result)
        self.on_change(p)
        return ExecutionOutcome(p.id, status, reason, details or [])

    def execute(self, proposal_id: str, now: datetime | None = None) -> ExecutionOutcome:
        now = now or utcnow()
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != ProposalStatus.APPROVED:
            return ExecutionOutcome(p.id, p.status, "not_approved")

        handler = self.handlers[p.action]
        # 同一店铺串行执行（DESIGN.md 14.1 一致性）
        with self._lock_for(p.shop_key):
            if p.expires_at and now > p.expires_at:
                return self._finish(p, ProposalStatus.EXPIRED, "expired")

            try:
                snapshot = handler.snapshot(p)
            except AdapterError as e:
                return self._finish(p, ProposalStatus.FAILED, "snapshot_error", [str(e)])

            drift = handler.drifted(p, snapshot)
            if drift:
                self.store.add_execution(p.id, p.shop_key, snapshot=snapshot, request=None, outcome="drift")
                return self._finish(p, ProposalStatus.FAILED, "drift", drift)

            violations = check_common_limits(p, self.limits, self.store, now) + handler.check_limits(p, self.limits)
            if violations:
                self.store.add_execution(p.id, p.shop_key, snapshot=snapshot, request=None, outcome="limit")
                return self._finish(p, ProposalStatus.FAILED, "limit", violations)

            p.status = ProposalStatus.EXECUTING
            self.store.save_proposal(p)
            self.on_change(p)

            try:
                request, response = handler.apply(p)
            except AdapterError as e:
                self.store.add_execution(p.id, p.shop_key, snapshot=snapshot, request={"error": True},
                                         response={"error": str(e)}, outcome="apply_error")
                return self._finish(p, ProposalStatus.FAILED, "apply_error", [str(e)],
                                    retryable=e.category.retryable)

            verified = handler.verify(p, response)
            rollback = None
            if not verified:
                try:
                    rollback = handler.rollback(p, snapshot)
                except AdapterError as e:
                    rollback = {"error": str(e)}
            self.store.add_execution(p.id, p.shop_key, snapshot=snapshot, request=request, response=response,
                                     verified=verified, rollback=rollback,
                                     outcome="ok" if verified else "verify_failed")
            p.executed_at = now
            if verified:
                return self._finish(p, ProposalStatus.EXECUTED, "ok")
            if rollback and "error" not in rollback:
                return self._finish(p, ProposalStatus.ROLLED_BACK, "verify_failed", rollback=rollback)
            return self._finish(p, ProposalStatus.FAILED, "verify_failed",
                                ["回读校验失败，且无法自动回滚，需要人工处理"], rollback=rollback)
