from datetime import timedelta

from ecom_twin.chat_context import automatic_context, MAX_CONTEXT
from ecom_twin.store import utcnow
from ecom_twin.models import TaskStatus
from test_mcp_and_dispatcher import dispatcher, ok_script
from test_intake import setup, dm, FakeJudge


def history(mid, text, chat="oc_dm", **extra):
    return {"message_id": mid, "text": text, "chat_id": chat, "sender_id": "ou_boss",
            "time": utcnow().isoformat(), "thread_id": "", "parent_id": "", **extra}


def source_task(env):
    task = env.app.store.get_task(env.task.id)
    task.origin_message_id, task.origin_chat_id = "om_current", "oc_dm"
    env.app.store.save_task(task)
    return task


def test_every_turn_injects_history_references_and_thread_before_current_request(env, tmp_path):
    task = source_task(env)
    env.feishu.history = [
        history("om_old", "原商品是美国站的蓝色款", time=(utcnow()-timedelta(days=2)).isoformat()),
        history("om_current", "继续", parent_id="om_old", thread_id="omt_1"),
        history("om_update", "更正：只做标题，不改价", thread_id="omt_1"),
        history("om_other", "其他会话机密", chat="oc_other"),
    ]
    d, calls = dispatcher(env, tmp_path, {"claude_code": [ok_script("s", "done"), ok_script("s", "done")]})
    d.run_task(task.id, "本轮请求", resume=False)
    prompt = calls[-1][2]
    assert "自动读取的聊天上下文" in prompt and "原商品是美国站的蓝色款" in prompt
    assert "更正：只做标题，不改价" in prompt and "其他会话机密" not in prompt
    assert prompt.endswith("## 当前请求\n本轮请求")
    env.feishu.history.append(history("om_new", "刚才那项取消"))
    d.run_task(task.id, "再看一下", resume=True)
    assert "刚才那项取消" in calls[-1][2]
    assert calls[-1][1] == "s"


def test_auto_context_failure_is_explicit_and_keeps_local_task_data(env, monkeypatch):
    task = source_task(env)
    env.app.store.add_task_message(task.id, {"message_id": "om_local", "chat_id": "oc_dm",
                                           "text": "已确认的商品 SKU-ABC"}, "回复", True)
    def fail(*a, **k):
        raise RuntimeError("secret-token-should-not-leak")
    monkeypatch.setattr(env.feishu, "chat_messages", fail)
    monkeypatch.setattr(env.feishu, "get_message", fail)
    text = automatic_context(env.app, task.id)
    assert "读取失败" in text and "SKU-ABC" in text
    assert "secret-token-should-not-leak" not in text
    assert "context_auto_loaded" in env.app.store.audit_events(task.id)


def test_auto_context_bounds_redacts_and_does_not_mix_chats(env, monkeypatch):
    task = source_task(env)
    monkeypatch.setenv("MIAOSHOU_APP_SECRET", "private-secret-value")
    rows = [history(f"om_{i}", "长消息" * 2000) for i in range(100)]
    rows += [history("om_foreign", "不应读取", chat="oc_other"), history("om_deleted", "已删除", deleted=True)]
    rows += [history("om_secret", "private-secret-value")]
    monkeypatch.setattr(env.feishu, "chat_messages", lambda *a, **k: rows)
    monkeypatch.setattr(env.feishu, "get_message", lambda _: history("om_current", "private-secret-value"))
    text = automatic_context(env.app, task.id)
    assert len(text) < MAX_CONTEXT + 1000
    assert "private-secret-value" not in text and "不应读取" not in text and "已删除" not in text
    assert "om_current" in text


def test_non_feishu_task_does_not_fetch_history(env, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("must not call Feishu")
    monkeypatch.setattr(env.feishu, "chat_messages", fail)
    assert automatic_context(env.app, env.task.id) == ""


def test_explicit_continuation_uses_only_open_task_without_judge(env, tmp_path):
    d, calls = setup(env, tmp_path)
    d.handle_message(dm("给 Noon 首页截图", "om_start"))
    task = env.app.store.list_tasks(chat_id="oc_dm")[0]
    env.app.tasks.set_status(task.id, TaskStatus.WAITING)
    before = len(env.app.store.list_tasks())
    receipt = d.handle_message(dm("结合上面的聊天记录，继续处理", "om_continue"))
    assert receipt == f"已并回 {task.id}，继续处理。"
    assert len(env.app.store.list_tasks()) == before
    assert calls[-1][1] == "s0"
    assert env.app.store.merge_decisions()[0]["layer"] == "明确续接"


def test_ambiguous_continuation_clarifies_without_creating_task(env, tmp_path):
    d, calls = setup(env, tmp_path, judge=FakeJudge([]))
    for i in range(2):
        d.handle_message(dm(f"不同的事情{i}", f"om_start{i}"))
        task = env.app.store.list_tasks(chat_id="oc_dm")[0]
        env.app.tasks.set_status(task.id, TaskStatus.WAITING)
    before = len(env.app.store.list_tasks())
    receipt = d.handle_message(dm("继续处理", "om_continue"))
    assert "多个未完成任务" in receipt
    assert len(env.app.store.list_tasks()) == before and len(calls) == 2
    assert env.feishu.sent[-1]["reply_to"] == "om_continue"


def test_continue_with_new_object_is_not_forced_into_old_task(env, tmp_path):
    d, calls = setup(env, tmp_path)
    d.handle_message(dm("处理库存", "om_inventory"))
    task = env.app.store.list_tasks(chat_id="oc_dm")[0]
    env.app.tasks.set_status(task.id, TaskStatus.WAITING)
    d.handle_message(dm("继续检查另一个店铺的售价", "om_price"))
    assert len(env.app.store.list_tasks(chat_id="oc_dm")) == 2


def test_continuation_does_not_use_another_chat_or_closed_task(env, tmp_path):
    d, calls = setup(env, tmp_path)
    d.handle_message(dm("处理库存", "om_inventory"))
    before = len(env.app.store.list_tasks())
    receipt = d.handle_message(dm("继续", "om_continue"))
    assert "没有找到" in receipt and len(calls) == 1
    assert len(env.app.store.list_tasks()) == before


def test_runtime_judge_is_tool_free_and_rejects_failed_or_invalid_results():
    from ecom_twin.intake import RuntimeJudge
    from ecom_twin.runtime.base import RunEvent
    class Runtime:
        text, ok = '```json\n{"task_id":"T-0011"}\n```', True
        def run(self, spec):
            assert spec.mcp_servers == {} and spec.allowed_tools == []
            assert "只返回" in spec.system_prompt
            yield RunEvent('message', {'text': self.text})
            yield RunEvent('done', {'ok': self.ok})
    rt = Runtime()
    judge = RuntimeJudge(lambda: rt)
    assert judge.ask('继续') == {'task_id': 'T-0011'}
    rt.ok = False
    assert judge.ask('继续') is None
    rt.ok, rt.text = True, '不是 JSON'
    assert judge.ask('继续') is None


def test_codex_default_uses_same_runtime_for_routing(env):
    from ecom_twin.cli import _judge
    from ecom_twin.intake import RuntimeJudge
    env.app.settings.runtime.default = 'codex'
    assert isinstance(_judge(env.app), RuntimeJudge)
    env.app.settings.intake.judge_enabled = False
    assert _judge(env.app) is None
