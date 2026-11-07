"""审批增强：合并卡片逐条勾选、修改后批准、高风险双人审批。"""

from datetime import timedelta

from conftest import NOW, publish_target
from ecom_twin.approval import parse_decision
from ecom_twin.models import ProposalStatus


def msg(text, sender="ou_boss", mid="om_r"):
    return {"message_id": mid, "sender_id": sender, "content": text}


def test_parse_new_forms():
    d = parse_decision("批准 1 3")
    assert d.approve and d.indices == [1, 3]
    assert parse_decision("批准 全部").all
    assert parse_decision("批准 P-0001 P-0003").proposal_ids == ["P-0001", "P-0003"]
    assert parse_decision("同意吗") is None
    d = parse_decision("修改 P-0001 毛利率=35% 尾数=0.95")
    assert d.kind == "modify" and d.edits == {"毛利率": "35%", "尾数": "0.95"}
    assert parse_decision("修改 P-0001 随便说说") is None


def test_batch_card_and_select_by_index(env):
    env.app.settings.batch_window_seconds = 60
    ps = [env.app.approvals.request(env.submit()) for _ in range(3)]
    assert all(not p.card_message_id for p in ps)                  # 先排队，不发
    assert env.app.approvals.flush_cards(NOW) == 0                  # 窗口没到
    assert env.app.approvals.flush_cards(NOW + timedelta(hours=1)) == 1
    card = env.feishu.sent[-1]
    assert card["type"] == "card" and "3 个" in card["card"]["header"]["title"]["content"]
    env.feishu.parents["om_r"] = card["message_id"]
    assert "这张卡上有 3 个提案" in env.app.approvals.handle_message(msg("批准"), now=NOW)
    receipt = env.app.approvals.handle_message(msg("批准 1 3"), now=NOW)
    statuses = [env.app.store.get_proposal(p.id).status for p in ps]
    assert statuses == [ProposalStatus.EXECUTED, ProposalStatus.PENDING, ProposalStatus.EXECUTED]
    assert receipt.count("已批准") == 2


def test_modify_then_approve_recomputes(env):
    p = env.app.approvals.request(env.submit())
    old_prices = [d["price"] for d in p.evidence["定价明细"]]
    receipt = env.app.approvals.handle_message(msg(f"修改 {p.id} 毛利率=40%"), now=NOW)
    p = env.app.store.get_proposal(p.id)
    assert "已按你的修改重新计算" in receipt and p.status == ProposalStatus.EXECUTED
    assert p.target["pricing_params"]["target_margin_rate"] == 0.4
    assert [d["price"] for d in p.evidence["定价明细"]] > old_prices      # 售价由代码重算
    assert "modified" in env.app.store.audit_events(p.id)


def test_modify_rejected_when_violating_limits(env):
    p = env.app.approvals.request(env.submit())
    receipt = env.app.approvals.handle_message(msg(f"修改 {p.id} 毛利率=5%"), now=NOW)
    assert "超出硬上限" in receipt
    p = env.app.store.get_proposal(p.id)
    assert p.status == ProposalStatus.PENDING and p.target["pricing_params"]["target_margin_rate"] == 0.3
    assert "不能修改" in env.app.approvals.handle_message(msg(f"修改 {p.id} 售价=1"), now=NOW)


def test_high_risk_needs_two_approvers(env):
    p = env.submit()
    p.risk = "高"
    env.app.store.save_proposal(p)
    env.app.approvals.request(p)
    r1 = env.app.approvals.handle_message(msg(f"批准 {p.id}"), now=NOW)
    assert "还需要另一位审批人" in r1 and env.app.store.get_proposal(p.id).status == ProposalStatus.PENDING
    assert any(m.get("to") == "ou_backup" and "需要第二人批准" in m.get("text", "") for m in env.feishu.sent)
    r2 = env.app.approvals.handle_message(msg(f"批准 {p.id}"), now=NOW)          # 同一个人再批一次不算
    assert "还需要另一位审批人" in r2
    env.app.approvals.handle_message(msg(f"批准 {p.id}", sender="ou_backup"), now=NOW)
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.EXECUTED


def test_reject_needs_single_target_on_batch_card(env):
    env.app.settings.batch_window_seconds = 1
    ps = [env.app.approvals.request(env.submit()) for _ in range(2)]
    env.app.approvals.flush_cards(NOW + timedelta(hours=1))
    env.feishu.parents["om_r"] = env.feishu.sent[-1]["message_id"]
    assert "提案号" in env.app.approvals.handle_message(msg("拒绝 2 不需要"), now=NOW)
    env.app.approvals.handle_message(msg(f"拒绝 {ps[1].id} 2 不需要"), now=NOW)
    assert env.app.store.get_proposal(ps[1].id).status == ProposalStatus.REJECTED
    assert publish_target
