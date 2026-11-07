"""发送出口内容闸门（对照数字分身 slack-send 的三条硬规则）：不 @所有人、不带凭证、群里单条不超过 500 字。"""

import json

from ecom_twin.config import SendPermission
from ecom_twin.memory import MemoryStore
from ecom_twin.mcp_server import ToolServer
from ecom_twin.outbound import MASK, Outbound, problems, sanitize
from test_intake import GROUP, NOW, gmsg, setup


def test_problems_explain_how_to_fix(monkeypatch):
    monkeypatch.setenv("MIAOSHOU_APP_SECRET", "s3cr3t-value-123")
    assert problems("@所有人 周报发了", to_group=True)[0].startswith("用了 @所有人")
    assert problems('<at user_id="all">所有人</at> 看下', to_group=True)
    assert problems("@allen 看下", to_group=True) == []                      # 点具体的人可以
    assert "密钥" in problems("签名用的 s3cr3t-value-123", to_group=False)[0]   # .env 里的原值
    assert "密钥" in problems("token: abcdefgh1234", to_group=False)[0]
    assert "现在 501 字" in problems("字" * 501, to_group=True)[0]
    assert problems("字" * 501, to_group=False) == []                         # 私聊负责人不限字数


def test_sanitize_masks_and_strips(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_SECRET", "feishu-secret-xyz")
    text, fixed = sanitize("@所有人 配置 feishu-secret-xyz 已更新")
    assert "feishu-secret-xyz" not in text and MASK in text and "@所有人" not in text
    assert fixed == ["去掉 @所有人", "隐藏疑似密钥"]


def test_notify_chat_rejects_with_fix_hint(env, tmp_path):
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 周报发一下", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    env.app.settings.send_permissions[GROUP] = SendPermission(notify=True)
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    sent_before = len(env.feishu.sent)
    text, err = server.call("notify_chat", {"text": "@所有人 周报已发"})
    assert err and "改完重发" in text and "@所有人" in text and len(env.feishu.sent) == sent_before
    text, err = server.call("notify_chat", {"text": "周报已发，大家看下"})
    assert not err and json.loads(text)["ok"]


def test_long_group_report_goes_full_to_owner(env, tmp_path):
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    env.app.settings.send_permissions[GROUP] = SendPermission(reply=True)
    d.intake.accept(gmsg("@分身 盘点一下", "om_1"), NOW)
    task = env.app.store.list_tasks()[0]
    Outbound(env.app).to_task(task.id, "结论" + "细" * 600)
    group_reply = env.feishu.sent[-1]
    to_owner = env.feishu.sent[-2]
    assert group_reply["type"] == "reply" and len(group_reply["text"]) <= 500 and "已私聊负责人" in group_reply["text"]
    assert to_owner["type"] == "text" and to_owner["to"] == env.app.settings.feishu.owner_open_id
    assert "细" * 600 in to_owner["text"]
