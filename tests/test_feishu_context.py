import json

from ecom_twin.feishu import content_text, normalize_message
from ecom_twin.mcp_server import ToolServer
from ecom_twin.memory import MemoryStore

def msg(mid, minute, text, chat="oc_ops", sender="ou_lena", thread=""):
    return {"message_id": mid, "chat_id": chat, "sender_id": sender, "sender_type": "user",
            "time": f"2026-10-26T08:{minute:02d}:00+00:00", "text": text, "parent_id": "", "thread_id": thread,
            "deleted": False}


def setup(env, tmp_path):
    env.feishu.history = [
        msg("om_1", 10, "ampm 那个运输商的电话导出多了 .0"),
        msg("om_2", 12, "工单我开了，TMS-1042"),
        msg("om_3", 15, "@分身 这个今天能看一下吗", thread="omt_9"),
        msg("om_4", 18, "补充：只影响美国站", thread="omt_9", sender="ou_shen"),
        msg("om_x", 15, "别的群的消息", chat="oc_other"),
    ]
    task = env.app.store.get_task(env.task.id)
    task.origin_message_id, task.origin_chat_id = "om_3", "oc_ops"
    env.app.store.save_task(task)
    return ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id)


def call(server, name, args):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    text = r["result"]["content"][0]["text"]
    return r["result"]["isError"], (json.loads(text) if text.startswith("{") else text)


def test_context_pulls_earlier_messages_and_thread(env, tmp_path):
    err, data = call(setup(env, tmp_path), "feishu_context", {})
    assert not err
    texts = [m["text"] for m in data["messages"]]
    assert texts == ["ampm 那个运输商的电话导出多了 .0", "工单我开了，TMS-1042", "@分身 这个今天能看一下吗", "补充：只影响美国站"]
    assert [m["is_origin"] for m in data["messages"]] == [False, False, True, False]
    assert "别的群的消息" not in texts
    assert env.app.store.list_steps(env.task.id)[-1].summary == "4 条消息"


def test_context_window_is_respected(env, tmp_path):
    err, data = call(setup(env, tmp_path), "feishu_context", {"before_minutes": 4, "after_minutes": 0})
    assert [m["message_id"] for m in data["messages"]][:2] == ["om_2", "om_3"]


def test_chat_history_only_for_allowed_chats(env, tmp_path):
    server = setup(env, tmp_path)
    err, text = call(server, "feishu_chat_history", {"chat_id": "oc_other"})
    assert err and "不可读" in text
    env.app.settings.feishu.readable_chats = ["oc_other"]
    # 放行后可读。工具按「当前时间往前 N 小时」取数，这里替换成不看时间的版本，避免测试依赖真实时钟
    env.feishu.chat_messages = lambda chat, s, e, limit=50: [m for m in env.feishu.history if m["chat_id"] == chat]
    err, data = call(server, "feishu_chat_history", {"chat_id": "oc_other"})
    assert not err and data["messages"][0]["text"] == "别的群的消息"


def test_thread_requires_readable_chat(env, tmp_path):
    server = setup(env, tmp_path)
    err, data = call(server, "feishu_thread", {"message_id": "om_4"})
    assert not err and [m["message_id"] for m in data["messages"]] == ["om_3", "om_4"]
    err, text = call(server, "feishu_thread", {"message_id": "om_x"})
    assert err and "不属于可读的会话" in text


def test_search_is_off_by_default(env, tmp_path):
    server = setup(env, tmp_path)
    err, text = call(server, "feishu_search", {"query": "TMS-1042"})
    assert err and "allow_user_search" in text
    env.app.settings.feishu.allow_user_search = True
    err, data = call(server, "feishu_search", {"query": "TMS-1042"})
    assert not err and data["messages"][0]["message_id"] == "om_2"


def test_task_without_feishu_origin(env, tmp_path):
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id)
    err, text = call(server, "feishu_context", {})
    assert not err and "不是由飞书消息触发" in text


def test_content_parsing():
    assert content_text("text", json.dumps({"text": "你好"})) == "你好"
    post = {"zh_cn": {"title": "周报", "content": [[{"tag": "text", "text": "本周 "}, {"tag": "at", "user_name": "Lena"}]]}}
    assert content_text("post", json.dumps(post)) == "周报\n本周 Lena"
    assert content_text("image", "{}") == "[图片]"
    raw = {"message_id": "om_1", "msg_type": "text", "create_time": "1793000000000", "chat_id": "oc_1",
           "sender": {"id": "ou_1", "sender_type": "user"}, "body": {"content": json.dumps({"text": "hi"})},
           "thread_id": "omt_1"}
    m = normalize_message(raw)
    assert (m["text"], m["sender_id"], m["thread_id"]) == ("hi", "ou_1", "omt_1") and m["time"].startswith("2026")


def test_dispatcher_records_origin_chat(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher, ok_script
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s", "done")]})
    receipt = d.handle_message({"chat_type": "p2p", "sender_id": "ou_boss", "message_id": "om_dm", "chat_id": "oc_dm",
                                "content": "看看这个"})
    task_id = receipt.split("已建任务 ")[1].rstrip("。")
    assert env.app.store.get_task(task_id).origin_chat_id == "oc_dm"
