import io
import json
import os
import subprocess
import sys

from conftest import NOW, publish_target
from ecom_twin.dispatcher import Dispatcher, is_retryable
from ecom_twin.mcp_server import ToolServer, serve, tool_names
from ecom_twin.memory import MemoryStore
from ecom_twin.models import ProposalStatus, TaskStatus
from ecom_twin.runtime import AgentRuntime, RunEvent


def tool_server(env, tmp_path):
    return ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id)


def call(server, name, args):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return resp["result"]


def test_mcp_lists_only_safe_tools(env, tmp_path):
    server = tool_server(env, tmp_path)
    init = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "ecom"
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == tool_names()
    assert not any("publish" in n or "claim" in n for n in names)  # 写接口从不暴露给 agent
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_submit_proposal_sends_card(env, tmp_path):
    result = call(tool_server(env, tmp_path), "submit_proposal", {
        "shop_key": "tiktok:10001", "action": "miaoshou.tiktok.publish", "target": publish_target(),
        "rationale": "目标毛利率 30%"})
    assert not result["isError"]
    pid = json.loads(result["content"][0]["text"])["proposal_id"]
    assert env.app.store.get_proposal(pid).card_message_id == env.feishu.sent[-1]["message_id"]


def test_mcp_rejected_proposal_returns_error_to_agent(env, tmp_path):
    result = call(tool_server(env, tmp_path), "submit_proposal", {
        "shop_key": "tiktok:10001", "action": "miaoshou.tiktok.publish", "target": publish_target(),
        "rationale": "预计日销 300 单"})
    assert result["isError"] and "300" in result["content"][0]["text"]


def test_mcp_report_step_and_ask_owner(env, tmp_path):
    server = tool_server(env, tmp_path)
    call(server, "report_step", {"name": "拉取采集箱", "status": "成功", "summary": "2 款"})
    assert env.app.store.list_steps(env.task.id)[0].summary == "2 款"
    call(server, "ask_owner", {"question": "目标毛利率按多少？"})
    task = env.app.store.get_task(env.task.id)
    assert task.status == TaskStatus.WAITING and task.blocker == "目标毛利率按多少？"
    assert env.app.store.task_for_message(env.feishu.sent[-1]["message_id"]) == env.task.id


def test_mcp_price_skus_and_unknown_tool(env, tmp_path):
    server = tool_server(env, tmp_path)
    priced = json.loads(call(server, "price_skus", {"params": publish_target()["pricing_params"],
                                                    "skus": publish_target()["skus"]})["content"][0]["text"])
    assert [p["price"] for p in priced] == [12.99, 16.99]
    assert call(server, "nope", {})["isError"]


def test_mcp_stdio_loop(env, tmp_path):
    out = io.StringIO()
    serve(tool_server(env, tmp_path), io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n'), out)
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_mcp_server_process_speaks_stdio(tmp_path):
    """真实子进程：用独立配置启动 MCP server，走一遍 initialize / tools/list / tools/call。"""
    config = tmp_path / "settings.toml"
    config.write_text(f'[storage]\ndb_path = "{tmp_path / "db.sqlite"}"\n')
    env_vars = {**os.environ, "ECOM_TWIN_FEISHU": "console", "MIAOSHOU_APP_KEY": "", "MIAOSHOU_APP_SECRET": ""}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "miaoshou_list_shops", "arguments": {"platform": "tiktok"}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "ecom_twin.mcp_server", "--task", "T-0001", "--config", str(config)],
                          input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True,
                          env=env_vars, timeout=30)
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert len(responses[1]["result"]["tools"]) == len(tool_names())
    assert responses[2]["result"]["isError"] and "missingCredentials" in responses[2]["result"]["content"][0]["text"]


class ScriptedRuntime(AgentRuntime):
    """按脚本依次返回事件，不启动子进程。"""

    def __init__(self, name, scripts, calls):
        super().__init__(binary=name)
        self.name, self.scripts, self.calls = name, scripts, calls

    def build_command(self, spec, resume_id=None):
        return []

    def parse_line(self, obj):
        return []

    def run(self, spec, resume_id=None):
        self.calls.append((self.name, resume_id, spec.prompt))
        yield from self.scripts.pop(0)


def ok_script(session, text):
    return [RunEvent("session", {"session_id": session}), RunEvent("tool_call", {"name": "x"}),
            RunEvent("usage", {"input_tokens": 100, "output_tokens": 20}), RunEvent("message", {"text": text}),
            RunEvent("done", {"ok": True})]


def fail_script(message):
    return [RunEvent("error", {"message": message}), RunEvent("done", {"ok": False})]


def dispatcher(env, tmp_path, scripts):
    calls = []
    runtimes = {k: ScriptedRuntime(k, v, calls) for k, v in scripts.items()}
    d = Dispatcher(env.app, MemoryStore(env.app.store, tmp_path), lambda key: runtimes[key], tmp_path / "ws",
                   sleep=lambda s: None)
    return d, calls


def dm(text, mid="om_dm1"):
    return {"chat_type": "p2p", "sender_id": "ou_boss", "message_id": mid, "content": text}


def test_self_dm_creates_task_runs_agent_and_reports_in_thread(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", "查到 2 款待上架商品")]})
    receipt = d.handle_message(dm("看看采集箱里有哪些待上架"))
    task_id = receipt.split("已建任务 ")[1].rstrip("。")
    task = env.app.store.get_task(task_id)
    assert task.status == TaskStatus.CLOSED and task.runtime == "claude_code" and task.session_id == "s1"
    assert (task.tokens_in, task.tokens_out) == (100, 20)
    report = env.feishu.sent[-1]
    assert report["reply_to"] == "om_dm1" and "查到 2 款" in report["text"]
    assert env.app.store.task_for_message(report["message_id"]) == task_id
    spec_prompt = calls[0][2]
    assert "看看采集箱" in spec_prompt


def test_post_image_message_downloads_all_attachments_and_keeps_log_redacted(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {"claude_code": [ok_script("s_img", "已读取图片")]})
    downloaded = []
    def download(message_id, image_key, destination):
        path = destination.with_suffix('.png')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'png')
        downloaded.append((message_id, image_key))
        return path
    env.feishu.download_image = download
    msg = {**dm("[飞书图文消息]", mid="om_img"), "message_type": "post",
           "image_keys": ["img_one", "img_two"],
           "prompt_content": "创建备货单 帐号：SECRET 密码：SECRET",
           "log_content": "[飞书图文消息，已隐藏正文和图片]"}
    receipt = d.handle_message(msg)
    task_id = receipt.split("已建任务 ")[1].rstrip("。")
    assert len(downloaded) == 2 and all(item[0] == "om_img" for item in downloaded)
    assert "SECRET" not in env.app.store.list_message_log()[0]["content"]
    assert "SECRET" in calls[0][2]
    assert str(tmp_path / "ws" / task_id / "attachments") in calls[0][2]


def test_dispatcher_automatically_recalls_and_injects_relevant_memory(env, tmp_path):
    from ecom_twin.memory import render_memory
    (tmp_path / "catalog-memory.md").write_text(
        render_memory("采集箱上品经验", "TikTok 上品与类目审核", "experience", "发布前应核对采集箱商品类目"),
        encoding="utf-8")
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", "已核对")]})
    d.run_task(env.task.id, "核对采集箱商品类目", resume=False)
    assert "采集箱上品经验" in d.memory.index_text()
    assert "发布前应核对采集箱商品类目" in d._spec(env.task.id, "检查", "### 采集箱上品经验\n发布前应核对采集箱商品类目").system_prompt
    records = env.app.store.list_recalls(env.task.id)
    assert len(records) == 1 and records[0]["hits"] == ["采集箱上品经验"]


def test_dispatcher_can_mount_isolated_chrome_mcp(env, tmp_path):
    env.app.settings.browser.enabled = True
    d, _ = dispatcher(env, tmp_path, {})
    spec = d._spec(env.task.id, "截图", "（无）")
    chrome = spec.mcp_servers["chrome-devtools"]
    assert chrome["args"][chrome["args"].index("--browserUrl") + 1] == "http://127.0.0.1:9222"
    assert "--workspace" in chrome["args"] and "mcp__chrome-devtools__*" in spec.allowed_tools


def test_reply_in_report_thread_resumes_same_session(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", "第一轮"), ok_script("s1", "第二轮")]})
    d.handle_message(dm("第一件事"))
    report_id = env.feishu.sent[-1]["message_id"]
    env.feishu.parents["om_dm2"] = report_id
    assert "已并回" in d.handle_message(dm("再看看第二款", mid="om_dm2"))
    assert calls[1][:2] == ("claude_code", "s1")


def test_messages_not_from_owner_dm_are_ignored(env, tmp_path):
    d, _ = dispatcher(env, tmp_path, {})
    assert d.handle_message({**dm("hi"), "sender_id": "ou_other"}) is None
    assert d.handle_message({**dm("hi"), "chat_type": "group"}) is None


def test_retryable_failure_retries_then_switches_to_fallback(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {
        "claude_code": [fail_script("model at capacity"), fail_script("model at capacity")],
        "codex": [ok_script("th-1", "codex 完成")],
    })
    result = d.run_task(env.task.id, "做点事", resume=False)
    assert result.ok and [c[0] for c in calls] == ["claude_code", "claude_code", "codex"]
    task = env.app.store.get_task(env.task.id)
    assert task.runtime == "codex" and "切换到 codex" in task.switch_log


def test_non_retryable_failure_switches_immediately_and_reports_failure(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {"claude_code": [fail_script("invalid api key")],
                                          "codex": [fail_script("invalid config")]})
    result = d.run_task(env.task.id, "做点事", resume=False)
    assert not result.ok and [c[0] for c in calls] == ["claude_code", "codex"]
    assert env.app.store.get_task(env.task.id).status == TaskStatus.FAILED
    assert "处理失败" in env.feishu.sent[-1]["text"]


def test_rejection_wakes_agent_with_reason(env, tmp_path):
    d, calls = dispatcher(env, tmp_path, {"claude_code": [ok_script("s9", "已调整")]})
    env.app.approvals.on_rejected = d.on_proposal_rejected
    p = env.app.approvals.request(env.submit())
    env.app.approvals.handle_message(
        {"message_id": "om_r", "sender_id": "ou_boss", "content": f"拒绝 {p.id} 3 等大促后"}, now=NOW)
    assert env.app.store.get_proposal(p.id).status == ProposalStatus.REJECTED
    assert "时机不对" in calls[0][2] and "等大促后" in calls[0][2]


def test_awaiting_approval_status_survives_end_of_run(env, tmp_path):
    def script_that_submits():  # agent 在运行中途提交提案
        yield RunEvent("session", {"session_id": "s1"})
        env.app.approvals.request(env.submit())
        yield RunEvent("message", {"text": "已提交提案"})
        yield RunEvent("done", {"ok": True})

    d, _ = dispatcher(env, tmp_path, {"claude_code": [script_that_submits()]})
    d.run_task(env.task.id, "上架", resume=False)
    assert env.app.store.get_task(env.task.id).status == TaskStatus.AWAITING_APPROVAL


def test_retryable_markers():
    assert is_retryable("Selected model is at capacity") and is_retryable("HTTP 429")
    assert not is_retryable("invalid api key")
