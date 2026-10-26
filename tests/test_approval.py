from datetime import timedelta

from conftest import NOW
from ecom_twin.approval import parse_decision
from ecom_twin.models import ProposalStatus, RejectReason


def msg(text, sender="ou_boss", mid="om_r1"):
    return {"message_id": mid, "sender_id": sender, "content": text}


def test_parse_decision_variants():
    assert parse_decision("批准 P-0003").approve
    assert parse_decision("同意").proposal_id is None
    d = parse_decision("拒绝 P-0003 2 类目选错了")
    assert (d.approve, d.proposal_id, d.reason, d.note) == (False, "P-0003", RejectReason.JUDGEMENT, "类目选错了")
    assert parse_decision("拒绝 P-0003 时机不对").reason == RejectReason.TIMING
    assert parse_decision("拒绝 P-0003").reason is None
    assert parse_decision("今天天气不错") is None


def test_card_is_sent_to_primary_approver(env):
    p = env.app.approvals.request(env.submit())
    sent = env.feishu.sent[-1]
    assert sent["to"] == "ou_boss" and p.card_message_id == sent["message_id"]
    assert env.app.store.get_task(env.task.id).status.value == "待审批"


def test_approve_triggers_execution_and_reports(env):
    p = env.app.approvals.request(env.submit())
    receipt = env.app.approvals.handle_message(msg(f"批准 {p.id}"), now=NOW)
    assert "已批准" in receipt
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.EXECUTED
    assert env.feishu.sent[-1]["text"].startswith(f"{p.id} 执行结果：已执行")


def test_reply_without_id_resolves_card_by_parent(env):
    p = env.app.approvals.request(env.submit())
    env.feishu.parents["om_r1"] = p.card_message_id
    env.app.approvals.handle_message(msg("拒绝 1 成本算错了"), now=NOW)
    p = env.app.store.get_proposal(p.id)
    assert p.status == ProposalStatus.REJECTED and p.reject_reason == RejectReason.DATA
    assert p.reject_note == "成本算错了"


def test_reject_requires_reason(env):
    p = env.app.approvals.request(env.submit())
    receipt = env.app.approvals.handle_message(msg(f"拒绝 {p.id}"), now=NOW)
    assert "需要选择原因" in receipt
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.PENDING


def test_only_approvers_can_decide(env):
    p = env.app.approvals.request(env.submit())
    receipt = env.app.approvals.handle_message(msg(f"批准 {p.id}", sender="ou_stranger"), now=NOW)
    assert "不是" in receipt
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.PENDING
    env.app.approvals.handle_message(msg(f"批准 {p.id}", sender="ou_backup"), now=NOW)
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.EXECUTED


def test_cannot_decide_twice(env):
    p = env.app.approvals.request(env.submit())
    env.app.approvals.handle_message(msg(f"拒绝 {p.id} 5"), now=NOW)
    receipt = env.app.approvals.handle_message(msg(f"批准 {p.id}"), now=NOW)
    assert "不能再审批" in receipt


def test_sweep_reminds_once_then_expires(env):
    p = env.app.approvals.request(env.submit())
    before = len(env.feishu.sent)
    env.app.approvals.sweep(now=NOW + timedelta(hours=21))
    reminders = env.feishu.sent[before:]
    assert [r["to"] for r in reminders] == ["ou_boss", "ou_backup"]
    env.app.approvals.sweep(now=NOW + timedelta(hours=22))
    assert len(env.feishu.sent) == before + 2
    env.app.approvals.sweep(now=NOW + timedelta(hours=25))
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.EXPIRED
    assert env.app.store.get_task(env.task.id).status.value == "已关闭"
