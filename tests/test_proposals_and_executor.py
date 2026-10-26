from datetime import timedelta

import pytest

from conftest import NOW, SHOP, publish_target
from ecom_twin.adapters.base import AdapterError, ErrorCategory
from ecom_twin.models import ProposalStatus, TaskStatus
from ecom_twin.proposals import ProposalRejected


def approve(env, p, now=NOW):
    p.status = ProposalStatus.APPROVED
    env.app.store.save_proposal(p)
    return env.app.executor.execute(p.id, now=now)


def test_evidence_and_consequences_are_computed_by_code(env):
    p = env.submit()
    assert p.evidence["SKU 数"] == 2
    assert [d["price"] for d in p.evidence["定价明细"]] == [12.99, 16.99]
    assert p.consequences["售价区间"] == "12.99~16.99 USD"
    assert p.current["titles"] == {"901": "Pet Hair Remover", "902": "Feeding Mat"}


def test_fabricated_number_in_rationale_is_rejected(env):
    with pytest.raises(ProposalRejected, match="120"):
        env.submit(rationale="预计每天卖 120 单")


def test_margin_below_floor_is_rejected_at_submit(env):
    target = publish_target(pricing_params={"commission_rate": 0.08, "target_margin_rate": 0.05,
                                            "exchange_rate": 0.14, "rounding": "none"})
    with pytest.raises(ProposalRejected, match="毛利率"):
        env.submit(target=target, rationale="")


def test_executes_approved_proposal(env):
    outcome = approve(env, env.submit())
    assert outcome.status == ProposalStatus.EXECUTED
    assert env.miaoshou.published == [([901, 902], [10001])]
    assert env.app.store.get_task(env.task.id).status == TaskStatus.REVIEW


def test_unapproved_proposal_is_not_executed(env):
    p = env.submit()
    assert env.app.executor.execute(p.id, now=NOW).reason == "not_approved"
    assert env.miaoshou.published == []


def test_expired_proposal_is_not_executed(env):
    outcome = approve(env, env.submit(), now=NOW + timedelta(hours=25))
    assert outcome.status == ProposalStatus.EXPIRED
    assert env.miaoshou.published == []


def test_drift_stops_execution(env):
    p = env.submit()
    env.miaoshou.titles["901"] = "Changed by someone"
    outcome = approve(env, p)
    assert outcome.status == ProposalStatus.FAILED and outcome.reason == "drift"
    assert env.miaoshou.published == []


def test_shop_outside_whitelist_is_blocked_at_execution(env):
    p = env.submit()
    env.app.executor.limits.allowed_shops = []
    outcome = approve(env, p)
    assert outcome.reason == "limit" and SHOP in outcome.details[0]


def test_daily_write_limit(env):
    env.app.executor.limits.max_daily_writes_per_shop = 1
    assert approve(env, env.submit()).status == ProposalStatus.EXECUTED
    outcome = approve(env, env.submit())
    assert outcome.reason == "limit" and "今日写操作已达上限" in outcome.details[0]


def test_apply_error_is_recorded_with_retryable_flag(env):
    env.miaoshou.publish_error = AdapterError(ErrorCategory.RATE_LIMIT, "apiQpsRateLimit")
    p = env.submit()
    outcome = approve(env, p)
    assert outcome.status == ProposalStatus.FAILED
    assert env.app.store.get_proposal(p.id).result["retryable"] is True
    assert env.app.store.list_executions(p.id)[0]["outcome"] == "apply_error"
