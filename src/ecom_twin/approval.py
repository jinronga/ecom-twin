"""审批：推送审批卡片、解析审批回复、过期提醒（DESIGN.md 7.3、7.5）。

P0 的审批方式是「回复文本指令」：lark-cli 的事件流没有卡片按钮回调，
所以卡片上提示审批人回复「批准 P-0001」或「拒绝 P-0001 1 备注」。
回复里没写提案号时，按被回复的消息反查对应的卡片。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import Settings
from .feishu import FeishuClient
from .models import Proposal, ProposalStatus, RejectReason
from .store import Store, utcnow

REASONS = list(RejectReason)

_APPROVE = re.compile(r"^\s*(批准|同意|approve)\s*(P-\d+)?\s*$", re.I)
_REJECT = re.compile(r"^\s*(拒绝|reject)\s*(P-\d+)?\s*(\S+)?\s*(.*)$", re.I)


@dataclass
class Decision:
    approve: bool
    proposal_id: str | None
    reason: RejectReason | None = None
    note: str = ""


def parse_decision(text: str) -> Decision | None:
    """解析审批回复。拒绝必须带原因（编号 1~5 或原因文字），否则返回 None 让调用方提示用法。"""
    m = _APPROVE.match(text)
    if m:
        return Decision(True, m.group(2))
    m = _REJECT.match(text)
    if not m:
        return None
    pid, reason_token, note = m.group(2), m.group(3), m.group(4).strip()
    reason = None
    if reason_token:
        if reason_token.isdigit() and 1 <= int(reason_token) <= len(REASONS):
            reason = REASONS[int(reason_token) - 1]
        else:
            reason = next((r for r in REASONS if r.value == reason_token), None)
    if reason is None:
        return Decision(False, pid, None, note)
    return Decision(False, pid, reason, note)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2%}" if 0 < abs(value) < 1 else f"{value:.2f}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else "无"
    return "无" if value is None else str(value)


def render_card(p: Proposal) -> dict[str, Any]:
    """审批卡片。所有数字取自 evidence / consequences（代码计算），不使用模型生成的文字里的数字。"""
    lines = [
        f"**提案号**：{p.id}　**店铺**：{p.shop_key}　**风险**：{p.risk}",
        f"**操作**：{p.action}",
        f"**理由**：{p.rationale}",
    ]
    evidence = [f"- {k}：{_fmt(v)}" for k, v in p.evidence.items() if k != "定价明细"]
    details = p.evidence.get("定价明细") or []
    if details:
        evidence.append("- 定价明细：")
        for d in details:
            if "error" in d:
                evidence.append(f"  - {d['sku']}：定价失败（{d['error']}）")
            else:
                evidence.append(
                    f"  - {d['sku']}：售价 {d['price']:.2f} {d['currency']}，成本 {d['cost']:.2f}，"
                    f"毛利 {d['profit']:.2f}（{d['margin_rate']:.2%}）"
                )
    consequences = [f"- {k}：{_fmt(v)}" for k, v in p.consequences.items()]
    expires = p.expires_at.strftime("%Y-%m-%d %H:%M UTC") if p.expires_at else "无"
    reasons = "　".join(f"{i}={r.value}" for i, r in enumerate(REASONS, 1))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"【待审批】{p.id}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**依据（代码计算）**\n" + "\n".join(evidence)}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**后果**\n" + "\n".join(consequences)}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                f"有效期至 {expires}。回复本消息：「批准 {p.id}」或「拒绝 {p.id} 原因编号 备注」。"
                f"原因编号：{reasons}"
            )}]},
        ],
    }


class ApprovalService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        feishu: FeishuClient,
        on_approved: Callable[[Proposal, datetime], None] | None = None,
        on_change: Callable[[Proposal], None] | None = None,
    ):
        self.store = store
        self.settings = settings
        self.feishu = feishu
        self.on_approved = on_approved or (lambda p, now: None)
        self.on_change = on_change or (lambda p: None)
        # 审批被拒绝时的回调（调度器用它带着拒绝原因唤醒 agent），组装后再设置
        self.on_rejected: Callable[[Proposal], None] = lambda p: None

    def _approvers(self, p: Proposal) -> tuple[str, str]:
        a = self.settings.approver_for(p.shop_key)
        if a:
            return a.primary, a.delegate
        return self.settings.feishu.owner_open_id, ""

    def _all_approvers(self) -> set[str]:
        ids = {self.settings.feishu.owner_open_id}
        for a in self.settings.approvers.values():
            ids |= {a.primary, a.delegate}
        return {i for i in ids if i}

    def request(self, p: Proposal) -> Proposal:
        """推送审批卡片给主审批人。"""
        primary, _ = self._approvers(p)
        p.card_message_id = self.feishu.send_card(primary, render_card(p))
        self.store.log_message({"message_id": p.card_message_id, "chat_type": "p2p",
                                "content": f"【待审批】{p.id}"}, "outbound", p.task_id)
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "card_sent", "system", {"to": primary, "message_id": p.card_message_id})
        self.on_change(p)
        return p

    def _resolve(self, decision: Decision, message: dict[str, Any]) -> Proposal | None:
        if decision.proposal_id:
            return self.store.get_proposal(decision.proposal_id)
        parent = message.get("parent_id") or self.feishu.get_parent_id(message["message_id"])
        return self.store.find_proposal_by_card(parent) if parent else None

    def handle_message(self, message: dict[str, Any], now: datetime | None = None) -> str | None:
        """处理一条飞书消息。不是审批指令时返回 None；是审批指令时返回给审批人的回执文本。"""
        now = now or utcnow()
        text = (message.get("content") or "").strip()
        decision = parse_decision(text)
        if decision is None:
            return None
        # 群聊里只理会审批人的消息，避免别人随口一句「同意」触发回执刷屏
        if message.get("chat_type", "p2p") != "p2p" and message.get("sender_id") not in self._all_approvers():
            return None

        def reply(msg: str) -> str:
            out_id = self.feishu.reply_text(message["message_id"], msg)
            self.store.log_message({"message_id": out_id, "chat_type": message.get("chat_type", "p2p"),
                                    "content": msg}, "outbound", p.task_id)
            return msg

        p = self._resolve(decision, message)
        if p is None:
            return reply("没找到对应的提案。请在审批卡片下回复，或写明提案号，例如「批准 P-0001」。")
        sender = message.get("sender_id", "")
        if sender not in [a for a in self._approvers(p) if a]:
            self.store.audit("proposal", p.id, "unauthorized_decision", sender, {"text": text})
            return reply(f"你不是 {p.shop_key} 的审批人，无法审批 {p.id}。")
        if p.status != ProposalStatus.PENDING:
            return reply(f"{p.id} 当前状态是「{p.status.value}」，不能再审批。")
        if p.expires_at and now > p.expires_at:
            self._expire(p)
            return reply(f"{p.id} 已过期作废，需要的话请让分身重新生成。")
        if not decision.approve and decision.reason is None:
            reasons = "　".join(f"{i}={r.value}" for i, r in enumerate(REASONS, 1))
            return reply(f"拒绝需要选择原因：「拒绝 {p.id} 原因编号 备注」。原因编号：{reasons}")

        p.approver = sender
        p.decided_at = now
        if decision.approve:
            p.status = ProposalStatus.APPROVED
            self.store.save_proposal(p)
            self.store.audit("proposal", p.id, "approved", sender)
            self.on_change(p)
            msg = reply(f"已批准 {p.id}，开始执行。")
            self.on_approved(p, now)
            return msg
        p.status = ProposalStatus.REJECTED
        p.reject_reason = decision.reason
        p.reject_note = decision.note
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "rejected", sender, {"reason": decision.reason.value, "note": decision.note})
        self.on_change(p)
        msg = reply(f"已拒绝 {p.id}（{decision.reason.value}）。原因会用于改进分身。")
        self.on_rejected(p)
        return msg

    def _expire(self, p: Proposal) -> None:
        p.status = ProposalStatus.EXPIRED
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "expired", "system")
        self.on_change(p)

    def sweep(self, now: datetime | None = None) -> None:
        """过期作废；过期前提醒一次主审批人，同时通知代理审批人。"""
        now = now or utcnow()
        remind_before = timedelta(hours=self.settings.remind_before_hours)
        for p in self.store.list_proposals(ProposalStatus.PENDING):
            if not p.expires_at:
                continue
            if now > p.expires_at:
                self._expire(p)
            elif not p.reminded and p.expires_at - now <= remind_before:
                primary, delegate = self._approvers(p)
                text = f"提醒：提案 {p.id}（{p.shop_key}）将于 {p.expires_at:%m-%d %H:%M} UTC 过期，请审批。"
                msg_id = self.feishu.send_text(primary, text)
                self.store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": text},
                                       "outbound", p.task_id)
                if delegate:
                    delegate_text = f"[代理审批] {text} 回复「批准 {p.id}」即可。"
                    msg_id = self.feishu.send_text(delegate, delegate_text)
                    self.store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": delegate_text},
                                           "outbound", p.task_id)
                p.reminded = True
                self.store.save_proposal(p)
                self.store.audit("proposal", p.id, "reminded", "system")
