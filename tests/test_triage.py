"""每轮结论第一行（对照数字分身的输出契约）：relevant / reason / waiting 存档，汇报里摘掉，waiting 给入口合并用。"""

from datetime import timedelta

from ecom_twin.dispatcher import parse_triage
from ecom_twin.models import TaskStatus
from test_intake import FakeJudge, GROUP, NOW, gmsg, setup
from test_mcp_and_dispatcher import dispatcher, dm, ok_script


def test_parse_triage_tolerates_fullwidth_and_missing_line():
    t, body = parse_triage("relevant: yes; reason: 负责人交办; waiting: 「等仓库同事给入库单号」\n查到 2 款")
    assert (t.relevant, t.reason, t.waiting, body) == (True, "负责人交办", "等仓库同事给入库单号", "查到 2 款")
    t, _ = parse_triage("Relevant：NO；reason：闲聊；waiting：无")
    assert (t.relevant, t.waiting) == (False, "")
    t, _ = parse_triage("relevant: yes; reason: 缺凭证; waiting: 等负责人补凭证，配好后回复「好了」")
    assert t.waiting == "等负责人补凭证，配好后回复「好了」"
    t, body = parse_triage("查到 2 款，没写第一行")
    assert t is None and body == "查到 2 款，没写第一行"   # 格式不对也不丢汇报


def test_report_drops_triage_line_and_waiting_keeps_task_open(env, tmp_path):
    out = "relevant: yes; reason: 负责人交办上架; waiting: 等负责人回复 P-0001 批不批\n提交了上架提案 P-0001"
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", out)]})
    receipt = d.handle_message(dm("把采集箱第一款上到测试店"))
    task = env.app.store.get_task(receipt.split("已建任务 ")[1].rstrip("。"))
    report = env.feishu.sent[-1]["text"]
    assert "relevant:" not in report and "提交了上架提案 P-0001" in report
    assert "等待：等负责人回复 P-0001 批不批" in report
    assert task.status == TaskStatus.WAITING and task.waiting == "等负责人回复 P-0001 批不批"
    assert "triage" in env.app.store.audit_events(task.id)


def test_not_waiting_closes_and_missing_line_is_audited(env, tmp_path):
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", "relevant: yes; reason: 查店铺; waiting: 无\n有 2 个店铺"),
                                                      ok_script("s2", "没写第一行")]})
    first = env.app.store.get_task(d.handle_message(dm("列一下店铺", "om_a")).split("已建任务 ")[1].rstrip("。"))
    second = env.app.store.get_task(d.handle_message(dm("再列一下", "om_b")).split("已建任务 ")[1].rstrip("。"))
    assert first.status == TaskStatus.CLOSED and first.waiting == ""
    assert second.status == TaskStatus.CLOSED and "triage_missing" in env.app.store.audit_events(second.id)


def test_irrelevant_group_message_gets_no_reply_in_group(env, tmp_path):
    from ecom_twin.config import SendPermission
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    d.runtime_factory("claude_code").scripts[:] = [ok_script("s0", "relevant: no; reason: 群里闲聊; waiting: 无")]
    env.app.settings.send_permissions[GROUP] = SendPermission(reply=True)
    d.intake.accept(gmsg("@分身 今天下午茶喝什么", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    assert task.status == TaskStatus.CLOSED and task.conclusion == "判断无需处理：群里闲聊"
    assert not [m for m in env.app.store.outbound_messages(task.id) if m["kind"] == "report"]


def test_waiting_feeds_merge_judge_and_opens_ask_window(env, tmp_path):
    judge = FakeJudge([{"is_answer": True, "is_new_request": False}])
    d, calls = setup(env, tmp_path, judge=judge, quiet_seconds=0)
    d.runtime_factory("claude_code").scripts[0] = ok_script(
        "s0", "relevant: yes; reason: 同事要备货单; waiting: 等 ou_lena 给入库单号\n备货单草稿已准备好")
    d.intake.accept(gmsg("@分身 帮忙建一下备货单", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    assert task.status == TaskStatus.WAITING
    # 同事不 @分身、也不回 thread，直接在群里给单号：问后窗口收进来，判断时带上「在等谁做什么」和发送人
    receipts = d.intake.accept(gmsg("入库单号 RK-20261029", "om_2"), NOW + timedelta(minutes=5))
    assert receipts and env.app.store.merge_decisions()[0]["layer"] == "等待唤醒" and len(calls) == 2
    assert "在等：等 ou_lena 给入库单号" in judge.prompts[0] and "发送人 ou_lena" in judge.prompts[0]


def test_no_needs_a_kind_otherwise_doubtful():
    """对照数字分身 0.2.66：判 no 必须写出类别，写不出的照样不处理，但标成存疑。"""
    t, _ = parse_triage("relevant: no; reason: 点了别人：仓库同事之间在交接; waiting: 无")
    assert (t.kind, t.doubtful) == ("点了别人", False)
    t, _ = parse_triage("relevant: no; reason: 群里闲聊; waiting: 无")
    assert (t.kind, t.doubtful) == ("闲聊", False)
    t, _ = parse_triage("relevant: no; reason: 看不出在说什么; waiting: 无")
    assert (t.kind, t.doubtful) == ("", True)
    t, _ = parse_triage("relevant: yes; reason: 负责人交办; waiting: 无")
    assert t.doubtful is False


def test_doubtful_no_is_listed_in_console(env, tmp_path):
    from ecom_twin.console import ConsoleApp
    from ecom_twin.memory import MemoryStore
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    d.runtime_factory("claude_code").scripts[:] = [ok_script("s0", "relevant: no; reason: 没点名我; waiting: 无")]
    d.intake.accept(gmsg("@分身 那个表谁来弄一下", "om_1"), NOW)
    task = [t for t in env.app.store.list_tasks() if t.id != env.task.id][0]
    assert "triage_doubtful" in env.app.store.audit_events(task.id)
    console = ConsoleApp(env.app.store, MemoryStore(env.app.store, tmp_path))
    status, body = console.route("GET", "/api/intake", {}, {})
    assert body["doubtful"][0]["task_id"] == task.id and body["doubtful"][0]["reason"] == "没点名我"
