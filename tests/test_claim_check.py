"""汇报核对：agent 说做了的，系统里要有对应记录（提案、证据），对不上就在汇报里写明、留审计。"""

from ecom_twin.runtime import RunEvent
from ecom_twin.store import utcnow
from test_mcp_and_dispatcher import dispatcher, ok_script


def report_text(env):
    return next(m["text"] for m in reversed(env.feishu.sent) if m["type"] in ("reply", "text") and "状态：" in m["text"])


def run(env, tmp_path, text, during=None):
    def script():
        yield RunEvent("session", {"session_id": "s"})
        if during:
            during()
        yield RunEvent("message", {"text": text})
        yield RunEvent("done", {"ok": True})
    d, _ = dispatcher(env, tmp_path, {"claude_code": [script()]})
    d.run_task(env.task.id, "做事", resume=False)
    return report_text(env)


def test_claimed_proposal_that_does_not_exist(env, tmp_path):
    report = run(env, tmp_path, "relevant: yes; reason: 交办; waiting: 无\n已提交上架提案 P-9999，等你审批")
    assert "系统核对：汇报提到的 P-9999 在系统里不存在" in report
    assert "claim_mismatch" in env.app.store.audit_events(env.task.id)


def test_claimed_proposal_without_any_new_proposal(env, tmp_path):
    report = run(env, tmp_path, "relevant: yes; reason: 交办; waiting: 无\n已经提交了上架提案，等你审批")
    assert "汇报说提交了提案，但这一轮没有新建任何提案" in report


def test_real_proposal_passes(env, tmp_path):
    holder = {}
    def submit():
        holder["p"] = env.submit(now=utcnow())
    d_text = "relevant: yes; reason: 交办; waiting: 无\n已提交上架提案 {pid}"
    def script():
        yield RunEvent("session", {"session_id": "s"})
        submit()
        yield RunEvent("message", {"text": d_text.format(pid=holder["p"].id)})
        yield RunEvent("done", {"ok": True})
    d, _ = dispatcher(env, tmp_path, {"claude_code": [script()]})
    d.run_task(env.task.id, "做事", resume=False)
    assert "系统核对" not in report_text(env)


def test_claimed_screenshot_without_evidence_and_negations_are_not_claims(env, tmp_path):
    report = run(env, tmp_path, "relevant: yes; reason: 交办; waiting: 无\n查完了，截图已上报")
    assert "汇报说附了截图或证据，但这一轮没有上报任何证据" in report
    env.feishu.sent.clear()
    report = run(env, tmp_path, "relevant: yes; reason: 交办; waiting: 无\n本轮没有提交提案，也没有截图")
    assert "系统核对" not in report
