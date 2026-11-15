"""群消息的用词和长短（对照数字分身 0.3.1 人设）：禁用黑话、啰嗦检查；汇报的提示词里带禁用词清单。"""

import json

from ecom_twin.config import OutboundSettings, settings_from_raw
from ecom_twin.outbound import check, parse_banned_words, split_clauses, wording_problems, OutboundRejected

CFG = OutboundSettings()


def test_banned_words_with_fix_hint_and_quotes_are_exempt():
    found = wording_problems("库存没兜住，超卖了", CFG)
    assert found and "用了「没兜住」" in found[0] and "超卖了 3 单" in found[0]
    assert wording_problems("报错原文是 `接口没兜住空值`，已经修好", CFG) == []        # 反引号里的引用不算
    assert wording_problems("```\n上架闸门拦下了\n```\n已经处理", CFG) == []


def test_wordiness_clauses_length_and_repeats():
    too_many = "，".join(f"第{i}件事处理完了" for i in range(10))
    assert any("10 个分句" in p for p in wording_problems(too_many, CFG))
    long_clause = "这批货因为供应商那边临时调整了发货计划所以入库时间要往后推迟大概三到五个工作日左右才能完成"
    assert any("一句就" in p for p in wording_problems(long_clause, CFG))
    assert any("几乎是同样的字" in p for p in wording_problems("入库单建好了，入库单已经建好了", CFG))
    assert wording_problems("入库单建好了，单号 RK-1029，明早到仓", CFG) == []


def test_split_and_parse_helpers():
    assert split_clauses("嗯，入库单建好了。单号发你了？") == ["入库单建好了", "单号发你了"]
    rules = parse_banned_words(["# 注释", "兜住 没兜住 => 说实际发生了什么", "闭环"])
    assert rules == [(["兜住", "没兜住"], "说实际发生了什么"), (["闭环"], "换成这件事本来的说法")]


def test_check_rejects_group_messages_only_when_cfg_given():
    try:
        check("这个需求已经闭环了", to_group=True, cfg=CFG)
        raise AssertionError("应该被拒")
    except OutboundRejected as e:
        assert "改完重发" in str(e) and "闭环" in str(e)
    check("这个需求已经闭环了", to_group=True)                  # 不给 cfg：只查 @所有人、密钥、长度（兼容原调用）
    off = OutboundSettings(wordy_check=False, banned_words=[])
    check("，".join(["处理完了"] * 12), to_group=True, cfg=off)   # 关掉检查就放行


def test_settings_and_prompt(env, tmp_path):
    custom = settings_from_raw({"outbound": {"banned_words": ["对齐 => 说清楚谁和谁确认了什么"], "max_clauses": 4}})
    assert custom.outbound.max_clauses == 4 and parse_banned_words(custom.outbound.banned_words)[0][0] == ["对齐"]
    from test_mcp_and_dispatcher import dispatcher
    d, _ = dispatcher(env, tmp_path, {})
    prompt = d._spec(env.task.id, "x").system_prompt
    assert "## 后续的活也先认清是谁的" in prompt and "不问「要不要我来做」" in prompt
    assert "- 兜住、兜不住、没兜住、兜底：说实际发生了什么" in prompt


def test_notify_chat_rejects_jargon(env, tmp_path):
    from ecom_twin.config import SendPermission
    from ecom_twin.memory import MemoryStore
    from ecom_twin.mcp_server import ToolServer
    from test_intake import GROUP, NOW, gmsg, setup
    d, _ = setup(env, tmp_path, quiet_seconds=0)
    d.intake.accept(gmsg("@分身 入库单建一下", "om_1"), NOW)
    task = [t for t in env.app.store.list_tasks() if t.id != env.task.id][0]
    env.app.settings.send_permissions[GROUP] = SendPermission(notify=True)
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    text, err = server.call("notify_chat", {"text": "入库这块已经打通了，后面我来收口"})
    assert err and "打通" in text and "收口" in text
    text, err = server.call("notify_chat", {"text": "入库单建好了，单号 RK-1029"})
    assert not err and json.loads(text)["ok"]
