"""消息入口与驱动：过滤、静默攒批、四层合并、驱动 / 静音、代发权限、问后窗口。"""

import json
from datetime import timedelta

from conftest import NOW
from ecom_twin.config import SendPermission
from ecom_twin.mcp_server import ToolServer
from ecom_twin.memory import MemoryStore
from ecom_twin.models import TaskStatus
from test_mcp_and_dispatcher import ScriptedRuntime, ok_script

GROUP = "oc_ops"


class FakeJudge:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


def setup(env, tmp_path, judge=None, runs=3, **intake):
    from ecom_twin.dispatcher import Dispatcher
    cfg = env.app.settings.intake
    cfg.listen_chats = [GROUP]
    cfg.anchor_patterns = cfg.anchor_patterns + [r"\bTMS-\d+\b"]
    for k, v in intake.items():
        setattr(cfg, k, v)
    calls = []
    rt = ScriptedRuntime("claude_code", [ok_script(f"s{i}", f"第{i}轮") for i in range(runs)], calls)
    d = Dispatcher(env.app, MemoryStore(env.app.store, tmp_path), lambda key: rt, tmp_path / "ws",
                   sleep=lambda s: None, judge=judge)
    return d, calls


def gmsg(text, mid, sender="ou_lena", chat=GROUP):
    return {"chat_type": "group", "chat_id": chat, "sender_id": sender, "message_id": mid, "content": text}


def dm(text, mid):
    return {"chat_type": "p2p", "chat_id": "oc_dm", "sender_id": "ou_boss", "message_id": mid, "content": text}


def test_group_filter(env, tmp_path):
    d, _ = setup(env, tmp_path)
    assert not d.intake.wanted(gmsg("随便聊聊", "om_1"), NOW)
    assert not d.intake.wanted(gmsg("@分身 看下", "om_2", chat="oc_unlisted"), NOW)
    assert d.intake.wanted(gmsg("@分身 ampm 这个今天能看一下吗", "om_3"), NOW)


def test_quiet_batching_then_new_group_task(env, tmp_path):
    d, calls = setup(env, tmp_path)
    assert d.intake.accept(gmsg("@分身 ampm 电话导出多了 .0", "om_1"), NOW) == []
    assert d.intake.accept(gmsg("@分身 工单 TMS-1042", "om_2"), NOW + timedelta(seconds=20)) == []
    assert d.intake.flush(NOW + timedelta(seconds=50)) == []          # 还没静默够 60 秒
    receipts = d.intake.flush(NOW + timedelta(seconds=90))
    assert len(receipts) == 1 and len(calls) == 1                     # 两条攒成一批，只建一个任务、跑一次
    task = env.app.store.list_tasks()[0]
    assert task.origin_chat_type == "group" and "TMS-1042" in task.anchors
    assert len(env.app.store.task_messages(task.id)) == 2
    # 群没开代发权限：回执、汇报都改为私聊负责人
    outs = env.app.store.outbound_messages(task.id)
    assert {o["delivered_to"] for o in outs} == {"私聊负责人"}
    assert env.feishu.sent[-1]["to"] == "ou_boss" and "该群未开放" in env.feishu.sent[-1]["text"]


def test_reply_permission_allows_thread_reply(env, tmp_path):
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    env.app.settings.send_permissions[GROUP] = SendPermission(reply=True)
    d.intake.accept(gmsg("@分身 看下 TMS-7", "om_1"), NOW)
    outs = env.app.store.outbound_messages(env.app.store.list_tasks()[0].id)
    assert {o["delivered_to"] for o in outs} == {"群 thread"}


def test_anchor_merges_without_mention_and_closed_tasks_are_skipped(env, tmp_path):
    d, calls = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 处理 TMS-1042", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    task.status = TaskStatus.WAITING
    env.app.store.save_task(task)
    receipts = d.intake.accept(gmsg("TMS-1042 补充：只影响美国站", "om_2", sender="ou_shen"), NOW)
    assert receipts == [f"已并回 {task.id}，继续处理。"] and len(calls) == 2
    assert env.app.store.merge_decisions()[0]["layer"] == "锚点"
    task = env.app.store.get_task(task.id)
    task.status = TaskStatus.CLOSED
    env.app.store.save_task(task)
    assert not d.intake.wanted(gmsg("TMS-1042 又有问题", "om_3"), NOW)  # 已告一段落：锚点不再拉进来


def test_owner_ack_is_silent(env, tmp_path):
    d, calls = setup(env, tmp_path)
    d.handle_message(dm("帮我看看采集箱", "om_1"))
    task = env.app.store.list_tasks()[0]
    task.status = TaskStatus.WAITING  # 本轮结束后仍未关闭
    env.app.store.save_task(task)
    report_id = env.app.store.outbound_messages(task.id)[-1]["message_id"]
    env.feishu.parents["om_2"] = report_id
    assert d.handle_message(dm("好的", "om_2")) is None and len(calls) == 1   # 静音：不唤醒
    assert env.app.store.task_messages(task.id)[-1]["drive"] == 0


def test_waiting_answer_in_ask_window(env, tmp_path):
    judge = FakeJudge([{"is_answer": True, "is_new_request": False}])
    d, calls = setup(env, tmp_path, judge=judge, quiet_seconds=0)
    env.app.settings.send_permissions[GROUP] = SendPermission(reply=True, ask=True)
    d.intake.accept(gmsg("@分身 新增运输商 ABC", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "ask_owner", "arguments": {"question": "ABC 的电话是多少？"}}})
    assert json.loads(r["result"]["content"][0]["text"])["提问位置"] == "来源群的 thread"
    # 问后窗口内，没有 @、没有回复 thread 的顶层消息也会被收进来
    receipts = d.intake.accept(gmsg("电话是 400-800-1234", "om_9"), NOW + timedelta(minutes=1))
    assert receipts and env.app.store.merge_decisions()[0]["layer"] == "等待唤醒" and len(calls) == 2


def test_topic_merge_can_be_silent(env, tmp_path):
    d, calls = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 运输商 ampm 电话格式不对", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    task.status = TaskStatus.WAITING
    env.app.store.save_task(task)
    d.intake.engine.judge = FakeJudge([{"task_id": task.id, "drive": False, "reason": "旁白补充"}])
    assert d.intake.accept(gmsg("@分身 对了 ampm 是美西的", "om_2"), NOW) == []
    assert env.app.store.merge_decisions()[0]["layer"] == "主题" and len(calls) == 1


def test_same_class_merge_with_whitelist(env, tmp_path):
    d, calls = setup(env, tmp_path, quiet_seconds=0, same_class_whitelist=["新增运输商"])
    d.intake.accept(gmsg("@分身 新增运输商 ABC", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    task.scene, task.status = "新增运输商", TaskStatus.WAITING
    env.app.store.save_task(task)
    d.intake.engine.judge = FakeJudge([{"task_id": None}, {"category": "新增运输商"}])
    d.intake.accept(gmsg("@分身 再加一个运输商 XYZ", "om_2", sender="ou_other"), NOW)
    assert env.app.store.merge_decisions()[0]["layer"] == "同类" and len(env.app.store.list_tasks(chat_id=GROUP)) == 1


def test_without_judge_falls_back_to_new_task(env, tmp_path):
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 事情一", "om_1"), NOW)
    d.intake.accept(gmsg("@分身 事情二", "om_2"), NOW)
    assert len(env.app.store.list_tasks(chat_id=GROUP)) == 2


def test_notify_requires_permission(env, tmp_path):
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 周报发一下", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)

    def notify():
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "notify_chat", "arguments": {"text": "周报已发"}}})
        return r["result"]
    assert notify()["isError"]
    env.app.settings.send_permissions[GROUP] = SendPermission(notify=True)
    assert not notify()["isError"] and env.feishu.sent[-1]["to_chat"] == GROUP
    assert env.app.store.outbound_messages(task.id)[-1]["kind"] == "notify"
