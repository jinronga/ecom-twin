"""操作电脑：经代理使用本机 Computer Use（cua_repl），只放行 js / js_reset，禁止目标直接拦截，每次调用留审计。"""

import io
import json
import sys

from ecom_twin.computer_use import Proxy, availability, find_cua
from ecom_twin.config import ComputerUseSettings

FAKE_CUA = '''
import json, re, sys
pending = {}
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        assert "elicitation" in msg["params"]["capabilities"]  # 代理声明了能回答授权征询
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"serverInfo": {"name": "fake"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        tools = [{"name": n} for n in ("js", "js_reset", "js_add_node_module_dir", "turn_ended")]
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}}), flush=True)
    elif msg.get("method") == "tools/call":
        args = msg["params"]["arguments"]
        m = re.search(r'getApp\\("([^"]+)"\\)', args.get("code", ""))
        if not m:
            text = "ran:" + args.get("code", "") + ":timeout=" + str(args.get("timeout_ms"))
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}]}}),
                  flush=True)
            continue
        eid = "e" + str(msg["id"])
        pending[eid] = (msg["id"], m.group(1))
        meta = {"connector_id": "computer-use", "tool_name": "get_app_state", "tool_params": {"app": m.group(1)},
                "tool_params_display": [{"name": "app", "value": m.group(1)}]}
        print(json.dumps({"jsonrpc": "2.0", "id": eid, "method": "elicitation/create",
                          "params": {"message": "Allow?", "_meta": meta}}), flush=True)
    elif msg.get("id") in pending:  # 代理对授权征询的回答
        call_id, app = pending.pop(msg["id"])
        ok = msg["result"]["action"] == "accept"
        text = ("opened " + app) if ok else ("Computer Use was not approved to use " + app)
        print(json.dumps({"jsonrpc": "2.0", "id": call_id,
                          "result": {"content": [{"type": "text", "text": text}], "isError": not ok}}), flush=True)
'''


def call(msg_id, name, args):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": args}}


def test_proxy_end_to_end_filters_tools_blocks_targets_and_audits(tmp_path):
    fake = tmp_path / "fake_cua.py"
    fake.write_text(FAKE_CUA)
    audits = []
    cfg = ComputerUseSettings(enabled=True, timeout_ms=60_000, allowed_apps=["海外仓客户端"])
    proxy = Proxy([sys.executable, str(fake)], {}, cfg, audit=lambda action, detail: audits.append((action, detail)))
    requests = [
        {"jsonrpc": "2.0", "id": "probe", "method": "server/discover", "params": {}},
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"capabilities": {}}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        call(2, "js", {"code": "await cua.getState();", "timeout_ms": 999_999}),
        call(3, "js", {"code": 'await cua.createBrowserTab("chrome", "https://sellercentral.amazon.com")'}),
        call(4, "js_add_node_module_dir", {"path": "/tmp/evil"}),
        call(5, "js", {"code": 'let app = await cua.getApp("海外仓客户端");'}),
        call(6, "js", {"code": 'let app = await cua.getApp("Calculator");'}),
    ]
    out = io.StringIO()
    proxy.serve(io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), out)
    messages = list(map(json.loads, out.getvalue().splitlines()))
    assert not any(m.get("method") == "elicitation/create" for m in messages)  # 授权征询绝不转给 agent
    replies = {m["id"]: m for m in messages}
    assert replies["probe"]["error"]["code"] == -32601  # 新协议探测由代理直接回绝，不转给 cua_repl
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["js", "js_reset"]
    assert replies[2]["result"]["content"][0]["text"].endswith("timeout=60000")  # 超时被封顶
    assert replies[3]["result"]["isError"] and "sellercentral" in replies[3]["result"]["content"][0]["text"]
    assert replies[4]["result"]["isError"]
    assert replies[5]["result"]["content"][0]["text"] == "opened 海外仓客户端"  # 白名单里的 app 被批准
    assert replies[6]["result"]["isError"]  # 名单外的 app 被拒绝
    actions = [a for a, _ in audits]
    assert "computer_use_blocked" in actions
    assert ("computer_use_app_approved", "海外仓客户端") in [(a, d.get("app")) for a, d in audits]
    assert ("computer_use_app_declined", "Calculator") in [(a, d.get("app")) for a, d in audits]


def test_app_approval_rules():
    from ecom_twin.computer_use import app_allowed
    cfg = ComputerUseSettings(enabled=True, allowed_apps=["Calculator", "com.microsoft.Excel", "1Password"])
    assert app_allowed("com.apple.calculator", "Calculator", cfg) is None
    assert app_allowed("com.microsoft.Excel", "Microsoft Excel", cfg) is None
    assert "名单" in app_allowed("com.apple.Notes", "Notes", cfg)
    assert "禁止" in app_allowed("com.1password.1password", "1Password", cfg)  # 禁止项优先于白名单
    assert app_allowed("", "", cfg)


def write_plugin(root, version, command):
    path = root / ".codex" / "plugins/cache/openai-bundled/unified-computer-use" / version / ".mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"cua_repl": {"command": str(command), "args": ["cua.mjs"],
                                                            "env": {"SKY": "1"}}}}))
    return path


def test_find_cua_picks_newest_installed_plugin(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.write_text("")
    monkeypatch.setattr("ecom_twin.computer_use.Path.home", lambda: tmp_path)
    write_plugin(tmp_path, "26.900.1", node)
    write_plugin(tmp_path, "26.1013.2", node)
    found = find_cua(ComputerUseSettings(enabled=True))
    assert "26.1013.2" in found["source"] and found["env"] == {"SKY": "1"}
    assert availability(ComputerUseSettings(enabled=False))[0] is False


def test_find_cua_missing_or_broken(tmp_path, monkeypatch):
    monkeypatch.setattr("ecom_twin.computer_use.Path.home", lambda: tmp_path)
    assert find_cua(ComputerUseSettings(enabled=True)) is None
    write_plugin(tmp_path, "1.0.0", tmp_path / "does-not-exist")
    assert find_cua(ComputerUseSettings(enabled=True)) is None
    ok, message = availability(ComputerUseSettings(enabled=True))
    assert not ok and "辅助功能" in message


def test_dispatcher_mounts_computer_mcp_only_when_enabled_and_installed(env, tmp_path, monkeypatch):
    from test_mcp_and_dispatcher import dispatcher
    d, _ = dispatcher(env, tmp_path, {})
    spec = d._spec(env.task.id, "x")
    assert "computer" not in spec.mcp_servers and "操作电脑（负责人已开启）" not in spec.system_prompt

    env.app.settings.computer_use.enabled = True
    monkeypatch.setattr("ecom_twin.dispatcher.find_cua", lambda cfg: {"command": "node", "args": []})
    spec = d._spec(env.task.id, "x")
    assert spec.mcp_servers["computer"]["args"][:2] == ["-m", "ecom_twin.computer_use"]
    assert {"mcp__computer__js", "mcp__computer__js_reset"} <= set(spec.allowed_tools)
    assert "mcp__computer__js_add_node_module_dir" not in spec.allowed_tools
    assert "操作电脑（负责人已开启）" in spec.system_prompt and "ask_owner" in spec.system_prompt
    assert "还没有，任何软件都会被拒绝" in spec.system_prompt


def test_applescript_route_gives_only_listed_shell_commands(env, tmp_path):
    """数字分身的做法：Bash + AppleScript。只放开指定命令，其它 shell 命令拒绝；默认关闭。"""
    from test_mcp_and_dispatcher import dispatcher
    from ecom_twin.runtime import ClaudeCodeRuntime, CodexRuntime
    d, _ = dispatcher(env, tmp_path, {})
    spec = d._spec(env.task.id, "x")
    assert spec.shell_commands == [] and "用 AppleScript 操作电脑" not in spec.system_prompt
    assert not any("desktop_" in t for t in spec.allowed_tools)
    cmd = ClaudeCodeRuntime().build_command(spec)
    assert "Bash" in cmd[cmd.index("--disallowedTools") + 1].split(",")

    env.app.settings.computer_use.applescript = True
    spec = d._spec(env.task.id, "x")
    assert spec.shell_commands == ["osascript", "open", "cliclick"]  # 截图不给整屏 screencapture，走 desktop_capture
    assert "用 AppleScript 操作电脑" in spec.system_prompt and "do shell script" in spec.system_prompt
    assert "desktop_capture" in spec.system_prompt and "不要激活或前置任何窗口" in spec.system_prompt
    assert {"mcp__ecom__desktop_windows", "mcp__ecom__desktop_capture", "mcp__ecom__annotate_screenshot"} \
        <= set(spec.allowed_tools)
    cmd = ClaudeCodeRuntime().build_command(spec)
    assert "Bash" not in cmd[cmd.index("--disallowedTools") + 1].split(",")
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "Bash(osascript:*)" in allowed and "Bash(open:*)" in allowed and "Bash" not in allowed

    codex = CodexRuntime(home=tmp_path / "codex-home")  # Codex 沙箱拦 AppleScript：在说明里写明不可用
    codex.prepare_workdir(spec)
    assert "-10827" in (spec.workdir / "AGENTS.md").read_text(encoding="utf-8")


def test_cursor_and_opencode_shell_allowlists(tmp_path):
    import json
    from ecom_twin.runtime import CursorAgentRuntime, OpenCodeRuntime
    from ecom_twin.runtime.base import RunSpec
    spec = RunSpec(prompt="p", system_prompt="s", workdir=tmp_path, shell_commands=["osascript", "open"])
    CursorAgentRuntime().prepare_workdir(spec)
    perms = json.loads((tmp_path / ".cursor" / "cli.json").read_text())["permissions"]
    assert "Shell(osascript)" in perms["allow"] and "Shell(*)" not in perms["deny"]
    OpenCodeRuntime().prepare_workdir(spec)
    bash = json.loads((tmp_path / "opencode.json").read_text())["permission"]["bash"]
    assert bash == {"osascript *": "allow", "open *": "allow", "*": "deny"}
