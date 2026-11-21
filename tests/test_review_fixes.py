"""代码审查发现的问题：每条一个回归测试。"""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ecom_twin.memory import MemoryStore
from ecom_twin.models import TaskSource
from ecom_twin.outbound import Outbound
from ecom_twin.tasks import title_from_text

SEED = Path(__file__).resolve().parents[1] / "memory"


def prompt_of(store, task_id):
    return json.loads(Path(store.get_task_queue(task_id)["prompt_path"]).read_text(encoding="utf-8"))["prompt"]


# 1. 跨进程排队：还没领走就合并；领走之后另起一轮，不把上一轮重跑
def test_enqueue_merges_before_claim_and_starts_new_round_after(env, tmp_path):
    from ecom_twin.dispatcher import enqueue_prompt
    store = env.app.store
    task = env.app.tasks.create("对话", TaskSource.CHAT, "待定")
    enqueue_prompt(env.app, tmp_path, task.id, "第一句", False)
    enqueue_prompt(env.app, tmp_path, task.id, "第二句", True)
    assert "第一句" in prompt_of(store, task.id) and "第二句" in prompt_of(store, task.id)
    claimed = store.claim_next_task()
    first_file = claimed["prompt_path"]
    enqueue_prompt(env.app, tmp_path, task.id, "第三句", True)                     # 已经开跑了
    row = store.get_task_queue(task.id)
    assert row["state"] == "queued" and row["prompt_path"] != first_file
    assert prompt_of(store, task.id) == "第三句"                                  # 不带上已经在跑的那两句
    assert "第一句" in json.loads(Path(first_file).read_text(encoding="utf-8"))["prompt"]   # 正在读的文件没被改


# 2. 私聊锚点那一条回复不了了：改发新的一条
def test_dm_anchor_lost_falls_back_to_new_message(env):
    task = env.app.tasks.create("群里来的", TaskSource.MESSAGE, "选品")
    task.origin_message_id, task.origin_chat_type, task.origin_chat_id = "om_g1", "group", "oc_g"
    env.app.store.save_task(task)
    out = Outbound(env.app)
    first = out.to_task(task.id, "第一轮")
    env.feishu.reply_text = lambda *a, **k: (_ for _ in ()).throw(__import__("ecom_twin.feishu", fromlist=["x"]).FeishuError("230011 message recalled"))
    second = out.to_task(task.id, "第二轮")
    assert second != first and env.feishu.sent[-1]["type"] == "text"
    assert env.app.store.kv_get(f"dm_anchor:{task.id}") == second                # 新的一条成了锚点
    assert "dm_anchor_lost" in [a["event"] for a in env.app.store.audit_log(("task",))]


# 7. 「问分身」里分身提问：写回对话框，不发飞书
def test_chat_task_outbound_stays_in_console(env):
    task = env.app.tasks.create("问分身", TaskSource.CHAT, "待定")
    sent = len(env.feishu.sent)
    mid = Outbound(env.app).to_task(task.id, "平台做 TikTok 还是 Amazon？", "ask")
    assert mid.startswith("chat:") and len(env.feishu.sent) == sent
    assert env.app.store.list_chat_messages(task.id)[-1]["text"] == "平台做 TikTok 还是 Amazon？"


# 8. 草稿夹带别的节：不自动采纳
def test_auto_adopt_refuses_drafts_with_extra_sections(env, tmp_path):
    shutil.copy(SEED / "selection-middle-east.md", tmp_path / "selection-middle-east.md")
    mem = MemoryStore(env.app.store, tmp_path)
    did = mem.propose({"mode": "append", "target_name": "选品剧本-中东",
                       "body": "## 重点方向\n- 斋月灯饰\n\n## 市场经验\n- 偷偷改了经验",
                       "why": "调研", "evidence": "R-1"})
    assert env.app.store.get_memory_draft(did)["status"] == "待审核"
    assert "偷偷改了经验" not in mem.by_name("选品剧本-中东").body


# 10. 整句在引号里被截断：不留半个引号
def test_title_with_leading_quote():
    title = title_from_text("「My order still hasn't arrived after twelve days and I want a refund now please」")
    assert not title.startswith("「") and title.endswith("…") and "「" not in title


# 4、5、6. 线索：只能转一次；普通改状态不能设成已转选品；转的时候原因保留
def test_lead_conversion_is_atomic_and_keeps_reason(env, tmp_path):
    from ecom_twin.radar import convert_lead
    store = env.app.store
    lid = store.add_lead({"rule": "r", "region": "US", "title": "瑜伽", "evidence": "R-1"})
    with pytest.raises(ValueError, match="转选品"):
        store.update_lead(lid, "已转选品")
    store.update_lead(lid, "在看", "等货源报价")
    r = convert_lead(env.app, lid, tmp_path)
    lead = store.get_lead(lid)
    assert lead["status"] == "已转选品" and lead["selection_task_id"] == r["task_id"] and lead["reason"] == "等货源报价"
    with pytest.raises(ValueError, match="已经转过了"):
        convert_lead(env.app, lid, tmp_path)
    results = []

    def go():
        try:
            results.append(convert_lead(env.app, lid2, tmp_path)["task_id"])
        except ValueError:
            results.append(None)
    lid2 = store.add_lead({"rule": "r", "region": "US", "title": "蜡烛", "evidence": "R-1"})
    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([x for x in results if x]) == 1                                    # 同时点四次只转一次


# 3. 控制台：输入格式不对返回 400 JSON，不断连接
def test_console_bad_input_returns_json_400(env, tmp_path):
    from ecom_twin.console import make_server
    for p in SEED.glob("selection-*.md"):
        shutil.copy(p, tmp_path / p.name)
    server, token = make_server(env.app.store, MemoryStore(env.app.store, tmp_path), "127.0.0.1", 0, token="t-owner",
                                settings_path=tmp_path / "settings.toml")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/selection-defaults?slug=selection-us&region=US&weight=abc",
                                     headers={"X-Console-Token": token, "Host": f"127.0.0.1:{port}"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 400 and "输入不对" in json.loads(e.value.read())["error"]
    finally:
        server.shutdown()
