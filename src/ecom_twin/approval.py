"""审批：推送审批卡片、解析审批回复、过期提醒（DESIGN.md 7.3、7.5）。

P0 的审批方式是「回复文本指令」：lark-cli 的事件流没有卡片按钮回调，
所以卡片上提示审批人回复「批准 P-0001」或「拒绝 P-0001 1 备注」。
回复里没写提案号时，按被回复的消息反查对应的卡片。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import Settings
from .feishu import FeishuClient
from .models import Proposal, ProposalStatus, RejectReason
from .store import Store, utcnow

REASONS = list(RejectReason)

_APPROVE = re.compile(r"^\s*(批准|同意|approve)\s*(.*)$", re.I)
_REJECT = re.compile(r"^\s*(拒绝|reject)\s*(P-\d+)?\s*(\S+)?\s*(.*)$", re.I)
_MODIFY = re.compile(r"^\s*(修改|modify)\s*(P-\d+)?\s+(.+)$", re.I)


@dataclass
class Decision:
    kind: str                                   # approve / reject / modify
    proposal_ids: list[str] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)   # 合并卡片里的序号（1 开始）
    all: bool = False
    reason: RejectReason | None = None
    note: str = ""
    edits: dict[str, str] = field(default_factory=dict)

    @property
    def approve(self) -> bool:
        return self.kind == "approve"

    @property
    def proposal_id(self) -> str | None:
        return self.proposal_ids[0] if self.proposal_ids else None


def parse_decision(text: str) -> Decision | None:
    """解析审批回复。

    批准：「批准」「批准 P-0001」「批准 P-0001 P-0003」「批准 全部」「批准 1 3」（合并卡片里的序号）
    拒绝：「拒绝 P-0001 原因编号 备注」，必须带原因（编号 1~5 或原因文字）
    修改后批准：「修改 P-0001 毛利率=35% 尾数=0.99」
    """
    m = _MODIFY.match(text)
    if m:
        edits = {}
        for token in re.split(r"[\s,，;；]+", m.group(3).strip()):
            key, sep, value = token.replace("＝", "=").partition("=")
            if not sep or not key or not value:
                return None
            edits[key] = value
        return Decision("modify", [m.group(2)] if m.group(2) else [], edits=edits)
    m = _APPROVE.match(text)
    if m:
        d = Decision("approve")
        for token in re.split(r"[\s,，、]+", m.group(2).strip()):
            if not token:
                continue
            if token in ("全部", "所有", "all", "ALL"):
                d.all = True
            elif re.fullmatch(r"P-\d+", token):
                d.proposal_ids.append(token)
            elif token.isdigit():
                d.indices.append(int(token))
            else:
                return None  # 「同意吗」这类不是审批指令
        return d
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
    return Decision("reject", [pid] if pid else [], reason=reason, note=note)


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
    evidence = [f"- {k}：{_fmt(v)}" for k, v in p.evidence.items() if k != "定价明细" and not k.startswith("_")]
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


def render_batch_card(proposals: list[Proposal]) -> dict[str, Any]:
    """同一审批人、同一类提案合并成一张卡（DESIGN.md 7.5），逐条列出，回复序号勾选。"""
    blocks = []
    for i, p in enumerate(proposals, 1):
        lines = [f"**{i}. {p.id}**　店铺 {p.shop_key}　风险 {p.risk}", f"理由：{p.rationale}"]
        lines += [f"- {k}：{_fmt(v)}" for k, v in p.consequences.items()][:4]
        blocks.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        blocks.append({"tag": "hr"})
    reasons = "　".join(f"{i}={r.value}" for i, r in enumerate(REASONS, 1))
    action = proposals[0].action
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": f"【待审批】{len(proposals)} 个 {action} 提案"}},
        "elements": blocks + [{"tag": "note", "elements": [{"tag": "plain_text", "content": (
            "回复本消息：「批准 全部」或「批准 1 3」（按序号）；拒绝某一个：「拒绝 P-xxxx 原因编号 备注」；"
            f"改了再批：「修改 P-xxxx 字段=值」。原因编号：{reasons}")}]}],
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
        # 「修改后批准」要用提案类型重新计算后果、重查硬上限，组装后再设置
        self.handlers: dict[str, Any] = {}
        self.shadow: Any = None  # ShadowService：影子模式的操作类型只记录，不推送审批

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
        """推送审批卡片给主审批人。开启合并窗口时先排队，由 flush_cards 合并发出。"""
        if self.shadow is not None and self.shadow.is_shadow(p.action):
            p = self.shadow.record(p)
            self.on_change(p)
            return p
        if self.settings.batch_window_seconds > 0:
            self.store.audit("proposal", p.id, "card_queued", "system")
            self.on_change(p)
            return p
        return self._send_card(p)

    def _send_card(self, p: Proposal) -> Proposal:
        primary, _ = self._approvers(p)
        p.card_message_id = self.feishu.send_card(primary, render_card(p))
        self.store.log_message({"message_id": p.card_message_id, "chat_type": "p2p",
                                "content": f"【待审批】{p.id}"}, "outbound", p.task_id)
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "card_sent", "system", {"to": primary, "message_id": p.card_message_id})
        self.on_change(p)
        return p

    def flush_cards(self, now: datetime | None = None) -> int:
        """把排队超过合并窗口的提案，按（主审批人, 操作类型）合并成卡片发出。返回发出的卡片数。"""
        now = now or utcnow()
        window = timedelta(seconds=self.settings.batch_window_seconds)
        groups: dict[tuple, list[Proposal]] = {}
        for p in self.store.list_proposals(ProposalStatus.PENDING):
            if p.card_message_id or (p.created_at and now - p.created_at < window):
                continue
            groups.setdefault((self._approvers(p)[0], p.action), []).append(p)
        for (primary, _), items in groups.items():
            if len(items) == 1:
                self._send_card(items[0])
                continue
            card_id = self.feishu.send_card(primary, render_batch_card(items))
            for p in items:
                p.card_message_id = card_id
                self.store.save_proposal(p)
                self.store.audit("proposal", p.id, "card_sent", "system", {"to": primary, "batch": len(items)})
                self.on_change(p)
        return len(groups)

    def _resolve_all(self, decision: Decision, message: dict[str, Any]) -> list[Proposal] | str:
        if decision.proposal_ids:
            found = [self.store.get_proposal(pid) for pid in decision.proposal_ids]
            return [p for p in found if p] or "没找到对应的提案。"
        parent = self.feishu.get_parent_id(message["message_id"])
        on_card = self.store.proposals_by_card(parent) if parent else []
        if not on_card:
            return "没找到对应的提案。请在审批卡片下回复，或写明提案号，例如「批准 P-0001」。"
        if decision.indices:
            picked = [on_card[i - 1] for i in decision.indices if 1 <= i <= len(on_card)]
            return picked or f"序号超出范围，这张卡上有 {len(on_card)} 个提案。"
        if len(on_card) > 1 and not decision.all:
            ids = "、".join(p.id for p in on_card)
            return f"这张卡上有 {len(on_card)} 个提案（{ids}）：请回复「批准 全部」「批准 1 3」，或写明提案号。"
        return on_card

    def _resolve(self, decision: Decision, message: dict[str, Any]) -> Proposal | None:
        found = self._resolve_all(decision, message)
        return found[0] if isinstance(found, list) and found else None

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

        found = self._resolve_all(decision, message)

        def reply(msg: str) -> str:
            out_id = self.feishu.reply_text(message["message_id"], msg)
            task_id = found[0].task_id if isinstance(found, list) and found else None
            self.store.log_message({"message_id": out_id, "chat_type": message.get("chat_type", "p2p"),
                                    "content": msg}, "outbound", task_id)
            return msg

        if isinstance(found, str):
            return reply(found)
        if decision.kind != "approve" and len(found) > 1:
            return reply("拒绝和修改一次只能针对一个提案，请写明提案号。")
        sender = message.get("sender_id", "")
        approved: list[Proposal] = []
        receipt = reply("\n".join(self._decide_one(p, decision, sender, text, now, approved) for p in found))
        for p in approved:  # 先回执，再执行（执行结果另发一条）
            self.on_approved(p, now)
        return receipt

    def _decide_one(self, p: Proposal, decision: Decision, sender: str, text: str, now: datetime,
                    approved: list[Proposal]) -> str:
        if sender not in [a for a in self._approvers(p) if a]:
            self.store.audit("proposal", p.id, "unauthorized_decision", sender, {"text": text})
            return f"你不是 {p.shop_key} 的审批人，无法审批 {p.id}。"
        if p.status != ProposalStatus.PENDING:
            return f"{p.id} 当前状态是「{p.status.value}」，不能再审批。"
        if p.expires_at and now > p.expires_at:
            self._expire(p)
            return f"{p.id} 已过期作废，需要的话请让分身重新生成。"

        if decision.kind == "reject":
            if decision.reason is None:
                reasons = "　".join(f"{i}={r.value}" for i, r in enumerate(REASONS, 1))
                return f"拒绝需要选择原因：「拒绝 {p.id} 原因编号 备注」。原因编号：{reasons}"
            p.approver, p.decided_at = sender, now
            p.status, p.reject_reason, p.reject_note = ProposalStatus.REJECTED, decision.reason, decision.note
            self.store.save_proposal(p)
            self.store.audit("proposal", p.id, "rejected", sender,
                             {"reason": decision.reason.value, "note": decision.note})
            self.on_change(p)
            self.on_rejected(p)
            return f"已拒绝 {p.id}（{decision.reason.value}）。原因会用于改进分身。"

        prefix = ""
        if decision.kind == "modify":
            modified = self._modify(p, decision.edits, sender)
            if isinstance(modified, str):
                return modified
            p, prefix = modified, f"已按你的修改重新计算 {p.id}：{self._brief(modified)}\n"
            p.approvals = []  # 内容变了，之前的批准作废

        # 批准：高风险要两个不同的人
        if sender not in p.approvals:
            p.approvals.append(sender)
        need = 2 if p.risk in self.settings.double_approval_risks else 1
        if len(p.approvals) < need:
            self.store.save_proposal(p)
            self.store.audit("proposal", p.id, "first_approval", sender)
            others = [a for a in self._approvers(p) if a and a not in p.approvals]
            for other in others:
                self.feishu.send_text(other, f"[需要第二人批准] {p.id}（{p.shop_key}，风险{p.risk}）已由 {sender} 批准，"
                                             f"还需要你确认。回复「批准 {p.id}」即可。")
            return prefix + f"已记录你对 {p.id} 的批准。风险{p.risk}，还需要另一位审批人批准才会执行。"
        p.approver, p.decided_at, p.status = sender, now, ProposalStatus.APPROVED
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "approved", sender, {"approvals": p.approvals})
        self.on_change(p)
        approved.append(p)
        return prefix + f"已批准 {p.id}，开始执行。"

    @staticmethod
    def _brief(p: Proposal) -> str:
        return "；".join(f"{k} {_fmt(v)}" for k, v in list(p.consequences.items())[:3])

    def _modify(self, p: Proposal, edits: dict[str, str], sender: str) -> Proposal | str:
        """修改后批准：改目标值 → 由代码重新计算证据和后果 → 重查硬上限。不通过就保持原提案不变。"""
        handler = self.handlers.get(p.action)
        if handler is None or not hasattr(handler, "apply_edits"):
            return f"{p.id}（{p.action}）不支持直接修改，请拒绝并说明原因，让分身重新提交。"
        before = json.loads(json.dumps(p.target))
        try:
            handler.apply_edits(p, edits)
            handler.prepare(p)
        except (ValueError, KeyError) as e:
            return f"修改没有生效：{e}"
        violations = handler.check_limits(p, self.settings.limits)
        if violations:
            return f"修改后超出硬上限，没有生效：{'；'.join(violations)}"
        self.store.save_proposal(p)
        self.store.audit("proposal", p.id, "modified", sender, {"before": before, "after": p.target, "edits": edits})
        return p

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
