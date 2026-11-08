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
    # 对外生效的写接口（发布）从不暴露给 agent；按用户决定，只有采集箱暂存区写入以 staging_ 前缀开放
    assert not any("publish" in n for n in names)
    write_verbs = ("claim", "edit", "save", "create", "delete", "update", "staging_collect")
    assert all(n.startswith("staging_") for n in names if any(v in n for v in write_verbs))
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


def test_dispatcher_injects_memory_index_but_not_bodies(env, tmp_path):
    """开局只注入索引（名称 — 触发动作），正文由 agent 按动作自己读；调度器不替它召回。"""
    from ecom_twin.memory import render_memory
    (tmp_path / "catalog-memory.md").write_text(
        render_memory("采集箱上品经验", "提交上架提案前打开：类目核对要点", "experience", "发布前应核对采集箱商品类目"),
        encoding="utf-8")
    d, _ = dispatcher(env, tmp_path, {"claude_code": [ok_script("s1", "已核对")]})
    d.run_task(env.task.id, "核对采集箱商品类目", resume=False)
    system = d._spec(env.task.id, "检查").system_prompt
    assert "- 采集箱上品经验 — 提交上架提案前打开：类目核对要点" in system
    assert "发布前应核对采集箱商品类目" not in system
    assert env.app.store.list_recalls(env.task.id) == []


def test_dispatcher_can_mount_isolated_chrome_mcp(env, tmp_path):
    """只读模式（[browser] interactive = false）：直接挂 chrome-devtools-mcp，只给截图和读页面。"""
    env.app.settings.browser.enabled = True
    env.app.settings.browser.interactive = False
    d, _ = dispatcher(env, tmp_path, {})
    d.chrome_check = lambda browser: None  # 端口上是专用 Chrome
    spec = d._spec(env.task.id, "截图")
    assert "不能跳转、点击、填写" in spec.system_prompt
    chrome = spec.mcp_servers["chrome-devtools"]
    assert chrome["args"][chrome["args"].index("--browserUrl") + 1] == "http://127.0.0.1:9222"
    assert "--workspace" in chrome["args"] and "mcp__chrome-devtools__*" not in spec.allowed_tools
    chrome_tools = {t.split("__")[2] for t in spec.allowed_tools if t.startswith("mcp__chrome-devtools__")}
    assert "take_screenshot" in chrome_tools and "take_snapshot" in chrome_tools
    assert not chrome_tools & {"navigate_page", "new_page", "click", "fill", "fill_form", "evaluate_script"}


def test_auto_login_policy_is_scoped_to_enabled_interactive_browser(env, tmp_path):
    d, _ = dispatcher(env, tmp_path, {})
    browser = env.app.settings.browser
    browser.auto_login_domains = ['sxsd.topwms.com']
    assert 'sxsd.topwms.com' not in d._browser_mode(None)
    browser.enabled = True
    policy = d._browser_mode(None)
    assert 'sxsd.topwms.com' in policy and '主机名与名单完全一致' in policy
    assert '此授权只允许登录' in policy and '不必再次询问' in policy
    assert '不输密码、不替人登录' not in policy
    browser.interactive = False
    assert 'sxsd.topwms.com' not in d._browser_mode(None)
    browser.interactive = True
    assert 'sxsd.topwms.com' not in d._browser_mode('专用浏览器未启动')


def test_dispatcher_mounts_chrome_through_guard_when_interactive(env, tmp_path):
    """默认放开（和数字分身一样）：经 chrome_guard 代理挂载，给跳转、点击、填写，不给上传本机文件。"""
    env.app.settings.browser.enabled = True
    d, _ = dispatcher(env, tmp_path, {})
    d.chrome_check = lambda browser: None
    spec = d._spec(env.task.id, "查入库单")
    chrome = spec.mcp_servers["chrome-devtools"]
    assert chrome["args"][:2] == ["-m", "ecom_twin.chrome_guard"]
    assert chrome["args"][chrome["args"].index("--workdir") + 1] == str(tmp_path / "ws" / env.task.id)
    chrome_tools = {t.split("__")[2] for t in spec.allowed_tools if t.startswith("mcp__chrome-devtools__")}
    assert {"navigate_page", "click", "fill", "evaluate_script"} <= chrome_tools and "upload_file" not in chrome_tools
    assert "background: true" in spec.system_prompt and "停在最后一步" in spec.system_prompt


def test_browser_not_mounted_when_port_is_not_the_dedicated_chrome(env, tmp_path):
    """端口上不是专用 Chrome（比如负责人的日常 Chrome）：不挂浏览器，提示词写明原因，留审计。"""
    env.app.settings.browser.enabled = True
    d, _ = dispatcher(env, tmp_path, {})
    d.chrome_check = lambda browser: "端口 9222 上的 Chrome 不是专用浏览器"
    spec = d._spec(env.task.id, "查入库单")
    assert "chrome-devtools" not in spec.mcp_servers
    assert not any(t.startswith("mcp__chrome-devtools__") for t in spec.allowed_tools)
    assert "专用浏览器这一轮不可用：端口 9222 上的 Chrome 不是专用浏览器" in spec.system_prompt
    assert "browser_unavailable" in env.app.store.audit_events(env.task.id)


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


class InlinePool:
    """同步执行的线程池替身：走队列路径（pool 不为 None），但不开线程。"""

    def submit(self, fn, *args):
        fn(*args)


def queued_dispatcher(env, tmp_path, scripts):
    calls = []
    runtime = ScriptedRuntime("claude_code", scripts, calls)
    d = Dispatcher(env.app, MemoryStore(env.app.store, tmp_path), lambda key: runtime, tmp_path / "ws",
                   pool=InlinePool(), sleep=lambda s: None)
    return d, calls


def test_reply_arriving_during_run_is_not_dropped(env, tmp_path):
    """旧轮次运行中收到新回复：旧轮次结束后不能把新入队的消息标成已完成。"""
    d, calls = None, None

    def first_run():
        yield RunEvent("session", {"session_id": "s1"})
        d._submit(env.task.id, "第二条要求", resume=True)  # 运行期间到达的新回复
        yield RunEvent("message", {"text": "第一轮完成"})
        yield RunEvent("done", {"ok": True})

    d, calls = queued_dispatcher(env, tmp_path, [first_run(), ok_script("s1", "第二轮完成")])
    d._submit(env.task.id, "第一条要求", resume=False)
    assert [c[2] for c in calls] == ["第一条要求", "第二条要求"]
    assert env.app.store.get_task_queue(env.task.id)["state"] == "done"


def test_messages_queued_before_start_are_merged_not_overwritten(env, tmp_path):
    d, calls = queued_dispatcher(env, tmp_path, [ok_script("s1", "完成")])
    env.app.store.set_queue_paused(True)  # 暂停队列，让两条都停在排队状态
    d._submit(env.task.id, "第一条", resume=False)
    d._submit(env.task.id, "第二条", resume=True)
    env.app.store.set_queue_paused(False)
    d.start_queue_worker()
    assert len(calls) == 1 and "第一条" in calls[0][2] and "第二条" in calls[0][2]
