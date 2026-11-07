"""接话直接落到任务（对照数字分身 0.2.66）：接的那句话属于哪个任务是确定的，就由代码定落点，不交给模型猜。

数字分身 WS-1350：用户在群里说了一句，同事在下面接话，系统没判就开了新单，其实是 WS-1348 的事。
ecom-twin 以前只认「回复分身发的消息」，同事回复任务的触发消息、或回复已在任务里的消息，都认不出来。
"""

from conftest import NOW
from ecom_twin.models import TaskStatus
from test_intake import FakeJudge, GROUP, gmsg, setup


def reply(text, mid, parent="", root="", sender="ou_kai"):
    return {**gmsg(text, mid, sender=sender), "parent_id": parent, "root_id": root}


def only_task(env, keep_open=False):
    """本测试建出来的那个群任务（夹具自带的 T-0001 不算）。keep_open：模拟 agent 这一轮写了在等谁、任务没关。"""
    tasks = [t for t in env.app.store.list_tasks() if t.id != env.task.id]
    assert len(tasks) == 1, [t.id for t in tasks]
    task = tasks[0]
    if keep_open and task.status == TaskStatus.CLOSED:
        task.status = TaskStatus.WAITING
        env.app.store.save_task(task)
    return task


def test_colleague_reply_to_origin_message_merges_without_mention(env, tmp_path):
    d, calls = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 ampm 这批货入库单建一下", "msg_1"), NOW)
    task = only_task(env, keep_open=True)
    # 同事没 @分身，只是回复了那条触发消息：以前过滤这一步就丢了，现在收进来并落到原任务
    receipts = d.intake.accept(reply("入库单号是 RK-1029", "msg_2", parent="msg_1"), NOW)
    assert receipts and only_task(env).id == task.id and len(calls) == 2
    decision = env.app.store.merge_decisions()[0]  # 新的在前
    assert decision["layer"] == "回复" and "任务的触发消息" in decision["reason"]


def test_thread_reply_lands_by_thread_root(env, tmp_path):
    """thread 里接的是别人的回复（没进任务），但 thread 第一句是任务的触发消息：按第一句落。"""
    d, calls = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 ampm 这批货入库单建一下", "msg_1"), NOW)
    task = only_task(env, keep_open=True)
    d.intake.accept(reply("补充：是美西仓", "msg_3", parent="msg_unknown", root="msg_1"), NOW)
    assert only_task(env).id == task.id and len(calls) == 2


def test_reply_to_anchor_merged_message_routes_but_inferred_does_not(env, tmp_path):
    """锚点并进去的消息被接话 → 落到那个任务；模型推断（主题）并进去的不当落点，免得错一次一路错。"""
    judge = FakeJudge([{"task_id": None}, {"task_id": None}])
    d, calls = setup(env, tmp_path, judge=judge, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 工单 TMS-1042 电话格式不对", "msg_1"), NOW)
    task = only_task(env, keep_open=True)
    d.intake.accept(gmsg("TMS-1042 截图在这", "msg_2", sender="ou_lena"), NOW)        # 锚点并入
    assert env.app.store.merge_decisions()[0]["layer"] == "锚点"
    only_task(env, keep_open=True)  # 并入后 agent 又跑了一轮；模拟它还在等人
    env.app.store.add_task_message(task.id, {"message_id": "msg_topic", "chat_id": GROUP}, "主题", True)
    engine = d.intake.engine
    assert engine.reply_target(reply("收到", "msg_4", parent="msg_2"))[1] == "已经在任务里的消息"
    assert engine.reply_target(reply("收到", "msg_5", parent="msg_topic")) is None


def test_closed_task_origin_is_not_a_landing_but_bot_reply_reopens(env, tmp_path):
    d, calls = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 ampm 这批货入库单建一下", "msg_1"), NOW)
    task = only_task(env)
    task.status = TaskStatus.CLOSED
    env.app.store.save_task(task)
    engine = d.intake.engine
    # 老任务的触发消息被接话，可能是新的事：不直接落（交给后面的层或开新任务）
    assert engine.reply_target(reply("这个又出问题了", "msg_6", parent="msg_1")) is None
    # 回复的是分身发的消息：明确在接分身的话，已结束的任务照旧重新打开
    receipt_id = env.app.store.outbound_messages(task.id)[0]["message_id"]
    env.app.store.link_report(receipt_id, task.id)
    assert engine.reply_target(reply("还有一单", "msg_7", parent=receipt_id))[0] == task.id


# ---- 方案 B：模型认出接的是哪一句，那句确定属于某个任务就由代码落点 ----

def hist(mid, text, sender, minutes_ago, chat=GROUP):
    from datetime import timedelta
    return {"message_id": mid, "chat_id": chat, "sender_id": sender, "text": text, "parent_id": "", "root_id": "",
            "thread_id": "", "time": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def two_tasks(env, tmp_path, judge):
    """群里先后建了两个任务：T_a 问 ampm 入库单，T_b 问另一个运输商的电话。"""
    d, calls = setup(env, tmp_path, judge=judge, quiet_seconds=0, runs=5)
    d.intake.accept(gmsg("@分身 ampm 这批货入库单建一下", "msg_a"), NOW)
    d.intake.accept(gmsg("@分身 新增运输商 ABC 的电话导出不对", "msg_b", sender="ou_lena"), NOW)
    created = [t for t in env.app.store.list_tasks() if t.id != env.task.id]
    for t in created:
        t.status = TaskStatus.WAITING
        env.app.store.save_task(t)
    by_origin = {t.origin_message_id: t for t in created}
    env.feishu.history = [hist("msg_a", "@分身 ampm 这批货入库单建一下", "ou_lena", 30),
                          hist("msg_x", "今天中午吃啥", "ou_kai", 20),
                          hist("msg_b", "@分身 新增运输商 ABC 的电话导出不对", "ou_lena", 10)]
    return d, calls, by_origin["msg_a"], by_origin["msg_b"]


def test_pointer_to_a_task_line_overrides_the_models_task_choice(env, tmp_path):
    judge = FakeJudge([])
    d, calls, task_a, task_b = two_tasks(env, tmp_path, judge)
    # 模型认出接的是 #1（ampm 入库单那句），但任务选成了离得近的 task_b：落点以「接的那句」为准
    judge.answers = [{"reply_to": "#1", "task_id": task_b.id, "drive": True, "reason": "补充入库单信息"}]
    d.intake.accept(gmsg("@分身 单号 RK-1029，是美西仓", "msg_new", sender="ou_kai"), NOW)
    decision = env.app.store.merge_decisions()[0]
    assert decision["task_id"] == task_a.id and decision["layer"] == "接话"
    prompt = judge.prompts[-1]
    assert "#1 ou_lena：@分身 ampm 这批货入库单建一下   → " + task_a.id in prompt and "#2 ou_kai：今天中午吃啥" in prompt


def test_pointer_to_unowned_line_falls_back_to_topic(env, tmp_path):
    judge = FakeJudge([])
    d, calls, task_a, task_b = two_tasks(env, tmp_path, judge)
    judge.answers = [{"reply_to": "#2", "task_id": task_b.id, "drive": True, "reason": "同一个运输商"}]
    d.intake.accept(gmsg("@分身 ABC 的电话应该带区号", "msg_new2", sender="ou_kai"), NOW)
    decision = env.app.store.merge_decisions()[0]
    assert decision["task_id"] == task_b.id and decision["layer"] == "主题"


def test_bad_pointer_or_missing_history_is_ignored(env, tmp_path):
    judge = FakeJudge([])
    d, calls, task_a, task_b = two_tasks(env, tmp_path, judge)
    judge.answers = [{"reply_to": "#99", "task_id": None, "drive": True}]          # 编号越界：当没认出
    d.intake.accept(gmsg("@分身 另一件事：盘点表发我", "msg_new3", sender="ou_kai"), NOW)
    assert env.app.store.merge_decisions()[0]["layer"] == "新任务"

    def broken(*args, **kwargs):
        raise RuntimeError("飞书接口超时")
    env.feishu.chat_messages = broken                                                # 拉不到历史：退回只看候选任务
    judge.answers = [{"task_id": task_b.id, "drive": True, "reason": "同一个运输商"}]
    d.intake.accept(gmsg("@分身 ABC 那个电话改好没", "msg_new4", sender="ou_kai"), NOW)
    assert env.app.store.merge_decisions()[0]["task_id"] == task_b.id
    assert "会话历史（→ 表示那句属于哪个任务）：\n（取不到）" in judge.prompts[-1]
