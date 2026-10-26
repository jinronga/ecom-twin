import stat
import threading

from conftest import NOW
from ecom_twin.config import Limits, Settings, validate_settings
from ecom_twin.feishu import consume_messages
from ecom_twin.models import TaskSource
from ecom_twin.runtime import RUNTIMES
from ecom_twin.store import Store


def test_validate_settings_flags_blocking_problems(tmp_path):
    s = Settings(db_path=tmp_path / "db")
    levels = {text.split("：")[0]: level for level, text in validate_settings(s, list(RUNTIMES))}
    assert levels["feishu.owner_open_id 未填"] == "错误"
    assert levels["limits.allowed_shops 为空"] == "错误"


def test_validate_settings_passes_complete_config(settings):
    assert [i for i in validate_settings(settings, list(RUNTIMES)) if i[0] == "错误"] == []
    settings.limits = Limits(allowed_shops=["10001"])
    assert any("格式" in text for _, text in validate_settings(settings, list(RUNTIMES)))


def test_group_messages_from_non_approvers_are_ignored(env):
    msg = {"message_id": "om_g", "sender_id": "ou_someone", "chat_type": "group", "content": "同意"}
    assert env.app.approvals.handle_message(msg, now=NOW) is None
    assert env.feishu.sent == []


def test_store_is_safe_under_concurrent_writes(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    ids, errors = [], []

    def worker():
        try:
            for _ in range(50):
                ids.append(store.next_id("T"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(set(ids)) == 200


def test_consume_messages_reconnects_after_exit(tmp_path):
    exe = tmp_path / "fake-lark"
    counter = tmp_path / "n"
    exe.write_text(f'#!/bin/sh\necho x >> {counter}\necho \'{{"message_id":"om_1"}}\'\nexit 1\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    sleeps = []
    gen = consume_messages(str(exe), sleep=sleeps.append)
    first, second = next(gen), next(gen)
    gen.close()
    assert first == second == {"message_id": "om_1"}
    assert sleeps == [5]


def test_same_task_runs_are_serialized(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher, ok_script
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s", "a"), ok_script("s", "b")]})
    task = env.app.tasks.create("t", TaskSource.SELF_DM, "待定")
    lock = d._task_lock(task.id)
    assert lock is d._task_lock(task.id)
    lock.acquire()
    t = threading.Thread(target=d.run_task, args=(task.id, "x", False))
    t.start()
    t.join(timeout=0.2)
    assert t.is_alive()  # 被锁住，等前一轮结束
    lock.release()
    t.join(timeout=5)
    assert not t.is_alive()
