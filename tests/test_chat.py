"""问分身：控制台对话建任务排队、选的底座第一轮就用、回复写回对话不发飞书、对话里建雷达只出草稿、管理工具。"""

import json
import shutil
from pathlib import Path

from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
from ecom_twin.mcp_server import ToolServer
from ecom_twin.memory import MemoryStore
from ecom_twin.models import TaskSource, TaskStatus

from test_mcp_and_dispatcher import dispatcher, ok_script

SEED = Path(__file__).resolve().parents[1] / "memory"


def console(env, tmp_path):
    for p in SEED.glob("selection-*.md"):
        shutil.copy(p, tmp_path / p.name)
    return ConsoleApp(env.app.store, MemoryStore(env.app.store, tmp_path), tmp_path / "settings.toml")


def test_chat_send_creates_conversation_and_queues(env, tmp_path, monkeypatch):
    import ecom_twin.paths as paths
    monkeypatch.setattr(paths, "WORKSPACES_DIR", tmp_path / "ws")
    app = console(env, tmp_path)
    status, r = app.route("POST", "/api/chat", {}, {"message": "今天有什么要我处理的？", "runtime": "codex"})
    assert status == 200 and r["serve_alive"] is False
    cid = r["conversation_id"]
    task = env.app.store.get_task(cid)
    assert task.source == TaskSource.CHAT and task.runtime == "codex" and task.status == TaskStatus.QUEUED
    q = env.app.store.get_task_queue(cid)
    assert q["state"] == "queued" and not q["resume"]
    prompt = json.loads(Path(q["prompt_path"]).read_text(encoding="utf-8"))["prompt"]
    assert "今天有什么要我处理的？" in prompt and "workspace_overview" in prompt
    status, d = app.route("GET", f"/api/chat/{cid}", {}, {})
    assert d["busy"] and d["messages"][0] == {**d["messages"][0], "role": "user", "text": "今天有什么要我处理的？"}
    # 第一轮还没跑就又说一句：并进同一轮（队列原有行为）
    app.route("POST", "/api/chat", {}, {"conversation_id": cid, "message": "顺便看看线索"})
    merged = json.loads(Path(env.app.store.get_task_queue(cid)["prompt_path"]).read_text(encoding="utf-8"))["prompt"]
    assert "顺便看看线索" in merged and "今天有什么要我处理的？" in merged
    # 第一轮跑完后接着说：续跑同一个底座会话（resume）
    with env.app.store.conn:
        env.app.store.conn.execute("UPDATE task_queue SET state='done' WHERE task_id=?", (cid,))
    app.route("POST", "/api/chat", {}, {"conversation_id": cid, "message": "那把第一个线索转选品"})
    assert env.app.store.get_task_queue(cid)["resume"]
    convs = app.route("GET", "/api/chat", {}, {})[1]
    assert convs["conversations"][0]["id"] == cid and any(x["key"] == "codex" for x in convs["runtimes"])
    assert app.route("POST", "/api/chat", {}, {"message": "  "})[0] == 400
    assert app.route("POST", "/api/chat", {}, {"message": "hi", "runtime": "gpt-9"})[0] == 400
    assert not allowed(ROLE_REVIEWER, "POST", "/api/chat")                         # 对话能改东西，只给负责人


def test_chat_reply_goes_to_conversation_not_feishu_and_uses_chosen_runtime(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {"codex": [ok_script("s1", "relevant: yes; reason: 负责人问概况; waiting: 无\n今天没有要处理的")]})
    task = env.app.tasks.create("今天有什么要处理", TaskSource.CHAT, "待定")
    task.runtime = "codex"
    env.app.store.save_task(task)
    sent_before = len(env.feishu.sent)
    d.run_task(task.id, "今天有什么要处理", resume=False)
    assert calls[0][0] == "codex"                                                 # 第一轮就用选的底座
    msgs = env.app.store.list_chat_messages(task.id)
    assert msgs[-1]["role"] == "assistant" and "今天没有要处理的" in msgs[-1]["text"]
    assert len(env.feishu.sent) == sent_before                                    # 不发飞书


def test_chat_tools_overview_leads_and_radar_draft(env, tmp_path):
    mem = MemoryStore(env.app.store, tmp_path)
    task = env.app.tasks.create("问分身", TaskSource.CHAT, "待定")
    server = ToolServer(env.app, mem, env.miaoshou, task.id)
    lid = env.app.store.add_lead({"rule": "r", "region": "US", "title": "儿童加热眼罩", "evidence": "R-1"})
    overview = json.loads(server.call("workspace_overview", {})[0])
    assert overview["新线索"] == 1 and "没在运行" in overview["任务服务"]
    assert json.loads(server.call("lead_list", {"status": "新"})[0])[0]["id"] == lid
    text, err = server.call("lead_set_status", {"lead_id": lid, "status": "放弃"})
    assert err and "原因" in text
    # 对话里建雷达：只生成草稿，写错当场报
    text, err = server.call("radar_rule_propose", {"name": "雷达-美国眼罩", "schedule": "每天九点", "regions": ["US"],
                                                   "watch": ["heated eye mask"]})
    assert err and "5 段" in text
    text, err = server.call("radar_rule_propose", {"name": "雷达-美国眼罩", "schedule": "0 9 * * *", "regions": ["us"],
                                                   "watch": ["heated eye mask"], "modifiers": ["for kids"]})
    r = json.loads(text)
    assert not err and r["什么时候跑"] == "每天 09:00（Asia/Shanghai）"
    assert mem.by_name("雷达-美国眼罩") is None                                    # 还没生效
    drafts = env.app.store.drafts_for_task(task.id)
    assert drafts[0]["type"] == "radar" and drafts[0]["status"] == "待审核"
    mem.approve(r["draft_id"])
    assert mem.by_name("雷达-美国眼罩").type == "radar"
    listed = json.loads(server.call("radar_list", {})[0])["规则"]
    assert listed[0]["名称"] == "雷达-美国眼罩" and listed[0]["什么时候跑"] == "每天 09:00（Asia/Shanghai）"
    text, err = server.call("radar_trigger", {"name": "没有这条"})
    assert err
    assert not json.loads(server.call("radar_trigger", {"name": "雷达-美国眼罩"})[0]).get("error")
    assert env.app.store.kv_get("radar:trigger:雷达-美国眼罩")


def test_holiday_calendar_tool(env, tmp_path):
    task = env.app.tasks.create("问分身", TaskSource.CHAT, "待定")
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, task.id)
    r = json.loads(server.call("holiday_calendar", {"region": "sa", "weeks": 60})[0])
    names = [h["name"] for h in r["节日"]]
    assert r["国家"] == "SA" and "斋月" in names and any("开斋节" in n for n in names)


def test_task_detail_reply_continues_task(env, tmp_path, monkeypatch):
    import ecom_twin.paths as paths
    monkeypatch.setattr(paths, "WORKSPACES_DIR", tmp_path / "ws")
    app = console(env, tmp_path)
    chat = env.app.tasks.create("建雷达", TaskSource.CHAT, "选品")
    feishu_task = env.app.tasks.create("飞书来的选品", TaskSource.SELF_DM, "选品")
    status, r = app.route("POST", f"/api/tasks/{chat.id}/reply", {}, {"text": "停止日期改成 3 月 12 日"})
    q = env.app.store.get_task_queue(chat.id)
    prompt = json.loads(Path(q["prompt_path"]).read_text(encoding="utf-8"))["prompt"]
    assert status == 200 and q["resume"] and "3 月 12 日" in prompt and "问分身" in prompt
    status, r = app.route("POST", f"/api/tasks/{feishu_task.id}/reply", {}, {"text": "再加两个候选"})
    prompt = json.loads(Path(env.app.store.get_task_queue(feishu_task.id)["prompt_path"]).read_text(encoding="utf-8"))["prompt"]
    assert status == 200 and "任务详情里补充" in prompt
    d = app.route("GET", f"/api/tasks/{feishu_task.id}", {}, {})[1]
    assert d["busy"] and d["messages"][0]["text"] == "再加两个候选"
    assert app.route("POST", f"/api/tasks/{chat.id}/reply", {}, {"text": " "})[0] == 400
    assert not allowed(ROLE_REVIEWER, "POST", f"/api/tasks/{chat.id}/reply")


def test_console_reply_on_feishu_task_is_recorded_and_still_reported(env, tmp_path):
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", "relevant: yes; reason: 补充; waiting: 无\n加好了两个候选")]})
    task = env.app.tasks.create("飞书来的选品", TaskSource.SELF_DM, "选品")
    env.app.store.add_chat_message(task.id, "user", "再加两个候选")
    sent = len(env.feishu.sent)
    d.run_task(task.id, "再加两个候选", resume=False)
    assert "加好了两个候选" in env.app.store.list_chat_messages(task.id)[-1]["text"]    # 详情里看得到
    assert len(env.feishu.sent) > sent                                               # 飞书照常汇报
