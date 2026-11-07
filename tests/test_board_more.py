"""看板其余表的增量导出，以及人在看板上改状态后反向生效。"""

from conftest import NOW
from ecom_twin.models import TaskStatus


def enable_all(env):
    fs = env.app.settings.feishu
    fs.messages_table_id, fs.executions_table_id, fs.outbound_table_id = "tbl_msg", "tbl_exec", "tbl_out"
    fs.recalls_table_id, fs.reviews_table_id = "tbl_recall", "tbl_review"
    env.app.board.tables.update({"message": "tbl_msg", "execution": "tbl_exec", "outbound": "tbl_out",
                                 "recall": "tbl_recall", "review": "tbl_review"})


def table_rows(env, table):
    return [r for r in env.feishu.records.values() if r["table"] == table]


def test_logs_exported_incrementally(env):
    enable_all(env)
    env.app.store.add_task_message(env.task.id, {"message_id": "om_1", "chat_id": "oc", "sender_id": "ou_a",
                                                 "text": "你好"}, "新任务", True)
    env.app.store.log_recall(env.task.id, "大促", ["大促日历"])
    env.app.store.add_outbound(env.task.id, "oc", "om_1", "receipt", "私聊负责人", "收到", "om_2")
    assert env.app.board.export_logs() == 3
    assert env.app.board.export_logs() == 0                     # 游标：不会重复导出
    env.app.board.flush(NOW, seconds=100)
    assert table_rows(env, "tbl_msg")[0]["是否唤醒"] == "唤醒"
    assert table_rows(env, "tbl_recall")[0]["关键词"] == "大促"
    assert table_rows(env, "tbl_out")[0]["发到"] == "私聊负责人"


def test_execution_and_review_rows(env):
    enable_all(env)
    p = env.app.approvals.request(env.submit())
    env.app.approvals.handle_message({"message_id": "om_a", "sender_id": "ou_boss", "content": f"批准 {p.id}"}, now=NOW)
    env.app.board.export_logs()
    env.app.board.flush(NOW, seconds=100)
    assert table_rows(env, "tbl_exec")[0]["结果"] == "ok"
    reviews = table_rows(env, "tbl_review")
    assert sorted(r["类型"] for r in reviews) == ["review_3d", "review_7d"]
    # 复盘完成后状态变化会再导出一次
    from datetime import timedelta
    env.miaoshou.published_list = [{"collectBoxDetailId": "901"}, {"collectBoxDetailId": "902"}]
    env.app.followups.run_due(NOW + timedelta(days=3, minutes=1))
    assert env.app.board.export_logs() == 1
    env.app.board.flush(NOW, seconds=100)
    assert "已完成" in [r["状态"] for r in table_rows(env, "tbl_review")]


def test_human_close_and_retry_on_board(env):
    env.app.board.enqueue_task(env.task)
    env.app.board.flush(NOW, seconds=10)
    record = table_rows(env, "tbl_tasks")[0]
    assert env.app.board.pull_changes(env.app.tasks) == []        # 没人改过：什么都不做
    record["状态"] = "已关闭"
    assert env.app.board.pull_changes(env.app.tasks) == [env.task.id]
    assert env.app.store.get_task(env.task.id).status == TaskStatus.CLOSED
    assert env.app.board.pull_changes(env.app.tasks) == []        # 只处理一次
    retried = []
    env.app.board.on_retry = retried.append
    record["状态"] = ["重试"]                                      # 单选字段可能是列表
    env.app.board.pull_changes(env.app.tasks)
    assert retried == [env.task.id]
    assert "board_reverse" in env.app.store.audit_events(env.task.id)


def test_status_written_by_us_is_not_a_human_change(env):
    env.app.tasks.set_status(env.task.id, TaskStatus.CLOSED)
    env.app.board.flush(NOW, seconds=10)
    assert env.app.board.pull_changes(env.app.tasks) == []
