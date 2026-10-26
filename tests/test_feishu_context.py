"""测试飞书上下文拉取功能。"""

from datetime import datetime, timedelta
from ecom_twin.feishu import FakeFeishu, normalize_message


def test_fake_feishu_get_message():
    """测试单条消息获取。"""
    feishu = FakeFeishu()
    msg = {
        "message_id": "om_123",
        "chat_id": "oc_abc",
        "sender": {"id": "ou_user1", "name": "张三"},
        "content": json.dumps({"text": "测试消息"}),
        "create_time": "1609459200000",  # 2021-01-01 00:00:00
        "msg_type": "text",
    }
    feishu.history = [normalize_message(msg)]

    result = feishu.get_message("om_123")
    assert result is not None
    assert result["message_id"] == "om_123"
    assert result["text"] == "测试消息"
    assert result["sender_name"] == "张三"

    assert feishu.get_message("om_nonexist") is None


def test_fake_feishu_chat_messages():
    """测试会话消息拉取（时间范围）。"""
    import json
    feishu = FakeFeishu()
    now = datetime.now()

    # 生成测试历史消息
    for i in range(10):
        msg_time = now - timedelta(hours=10-i)
        feishu.history.append({
            "message_id": f"om_{i}",
            "chat_id": "oc_abc",
            "thread_id": "",
            "sender_id": f"ou_user{i % 3}",
            "sender_name": f"用户{i % 3}",
            "text": f"消息 {i}",
            "time": msg_time.isoformat(),
            "msg_type": "text",
        })

    # 拉取最近 5 小时的消息
    start_ts = int((now - timedelta(hours=5)).timestamp())
    end_ts = int(now.timestamp())
    msgs = feishu.chat_messages("oc_abc", start_ts, end_ts, limit=50)

    assert len(msgs) >= 5
    assert all(m["chat_id"] == "oc_abc" for m in msgs)
    # 验证按时间排序
    times = [datetime.fromisoformat(m["time"]) for m in msgs]
    assert times == sorted(times)


def test_fake_feishu_thread_messages():
    """测试 thread 消息拉取。"""
    feishu = FakeFeishu()
    now = datetime.now()

    # 主消息
    feishu.history.append({
        "message_id": "om_root",
        "chat_id": "oc_abc",
        "thread_id": "om_root",
        "sender_id": "ou_user1",
        "sender_name": "用户1",
        "text": "主消息",
        "time": (now - timedelta(hours=2)).isoformat(),
        "msg_type": "text",
    })

    # Thread 回复
    for i in range(3):
        feishu.history.append({
            "message_id": f"om_reply_{i}",
            "chat_id": "oc_abc",
            "thread_id": "om_root",
            "sender_id": f"ou_user{i}",
            "sender_name": f"用户{i}",
            "text": f"回复 {i}",
            "time": (now - timedelta(hours=1, minutes=i*10)).isoformat(),
            "msg_type": "text",
        })

    msgs = feishu.thread_messages("om_root", limit=50)
    assert len(msgs) == 4
    assert all(m["thread_id"] == "om_root" for m in msgs)
    assert msgs[0]["message_id"] == "om_root"


def test_fake_feishu_search_messages():
    """测试消息搜索。"""
    feishu = FakeFeishu()
    now = datetime.now()

    for i, text in enumerate(["商品上架", "价格调整", "商品下架", "商品库存补充"]):
        feishu.history.append({
            "message_id": f"om_{i}",
            "chat_id": "oc_abc",
            "thread_id": "",
            "sender_id": "ou_user1",
            "sender_name": "用户1",
            "text": text,
            "time": (now - timedelta(hours=i)).isoformat(),
            "msg_type": "text",
        })

    # 搜索"商品"
    msgs = feishu.search_messages("商品", 0, int(now.timestamp()), limit=20)
    assert len(msgs) == 3
    assert all("商品" in m["text"] for m in msgs)


def test_normalize_message():
    """测试消息规范化。"""
    import json

    # 标准格式
    msg1 = {
        "message_id": "om_123",
        "chat_id": "oc_abc",
        "sender": {"id": "ou_user1", "name": "张三"},
        "content": json.dumps({"text": "测试"}),
        "create_time": "1609459200000",
        "msg_type": "text",
    }
    norm1 = normalize_message(msg1)
    assert norm1["message_id"] == "om_123"
    assert norm1["sender_name"] == "张三"
    assert norm1["text"] == "测试"

    # 处理缺失字段
    msg2 = {
        "message_id": "om_456",
        "content": "简单文本",
    }
    norm2 = normalize_message(msg2)
    assert norm2["message_id"] == "om_456"
    assert norm2["sender_name"] == "未知"
    assert norm2["text"] == ""


import json
