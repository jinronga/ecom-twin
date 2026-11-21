"""照数字分身「一件事一个 thread」：任务往来以话题形式回复；会话不支持就退回普通引用回复；可配置关掉。"""

from ecom_twin.models import TaskSource
from ecom_twin.outbound import Outbound


def dm_task(env):
    task = env.app.tasks.create("帮我看看美国市场", TaskSource.SELF_DM, "选品")
    task.origin_message_id, task.origin_chat_type, task.origin_chat_id = "om_q1", "p2p", "oc_dm"
    env.app.store.save_task(task)
    return task


def test_reports_go_into_one_thread(env):
    task = dm_task(env)
    out = Outbound(env.app)
    out.to_task(task.id, "第一轮结论")
    out.to_task(task.id, "第二轮结论")
    replies = [m for m in env.feishu.sent if m["type"] == "reply"]
    assert [m["reply_to"] for m in replies] == ["om_q1", "om_q1"] and all(m["in_thread"] for m in replies)
    assert {o["delivered_to"] for o in env.app.store.outbound_messages(task.id)} == {"私聊话题"}


def test_falls_back_to_quote_reply_when_thread_unsupported(env):
    task = dm_task(env)
    env.feishu.thread_unsupported = True
    msg_id = Outbound(env.app).to_task(task.id, "结论")
    reply = env.feishu.sent[-1]
    assert msg_id == reply["message_id"] and reply["in_thread"] is False            # 回复没丢
    events = [a["event"] for a in env.app.store.audit_log(("task",))]
    assert "thread_reply_fallback" in events


def test_thread_reply_can_be_turned_off(env):
    task = dm_task(env)
    env.app.settings.feishu.reply_in_thread = False
    Outbound(env.app).to_task(task.id, "结论")
    assert env.feishu.sent[-1]["in_thread"] is False


def test_evidence_image_goes_into_thread(env, monkeypatch):
    task = dm_task(env)
    seen = []
    original = env.feishu.reply_image
    def reply_image(mid, path, in_thread=False):
        seen.append(in_thread)
        return original(mid, path, in_thread)
    monkeypatch.setattr(env.feishu, "reply_image", reply_image)
    Outbound(env.app).image_to(task.id, "om_q1", "a.png")
    assert seen == [True]
