"""专用 Chrome 操作代理：放开跳转、点击、填写（和数字分身一样），卖货后台网址和脚本拦截，写类操作留审计。"""

import io
import json
import sys

import subprocess

from ecom_twin.chrome_guard import ChromeGuard, upstream_command, verify_dedicated
from ecom_twin.config import BrowserSettings

FAKE_CHROME_MCP = '''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"serverInfo": {"name": "fake-chrome"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        tools = [{"name": n} for n in ("navigate_page", "click", "fill", "take_snapshot", "upload_file", "lighthouse_audit")]
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}}), flush=True)
    elif msg.get("method") == "tools/call":
        p = msg["params"]
        text = "did:" + p["name"] + ":" + json.dumps(p.get("arguments", {}), ensure_ascii=False)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}]}}),
              flush=True)
'''


def run_guard(tmp_path, requests):
    script = tmp_path / "fake_chrome_mcp.py"
    script.write_text(FAKE_CHROME_MCP)
    audits = []
    guard = ChromeGuard([sys.executable, str(script)], {}, BrowserSettings(), lambda a, d: audits.append((a, d)), "T-1")
    out = io.StringIO()
    guard.serve(io.StringIO("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)), out)
    return {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}, audits


def call(i, name, args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


def test_guard_forwards_actions_blocks_seller_backends_and_audits(tmp_path):
    replies, audits = run_guard(tmp_path, [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        call(2, "navigate_page", {"url": "https://wms.example.com/inbound?page=2"}),
        call(3, "click", {"uid": "1_23"}),
        call(4, "navigate_page", {"url": "https://sellercentral.amazon.com/inventory"}),
        call(5, "evaluate_script", {"function": "() => location.href='https://seller.tiktok.com/product'"}),
        call(6, "upload_file", {"uid": "1_2", "filePath": "/Users/x/.env"}),
        call(7, "take_snapshot", {}),
    ])
    names = [t["name"] for t in replies[1]["result"]["tools"]]
    assert names == ["navigate_page", "click", "fill", "take_snapshot"]       # 上传本机文件、性能分析不给
    assert replies[2]["result"]["content"][0]["text"].startswith("did:navigate_page")
    assert replies[3]["result"]["content"][0]["text"].startswith("did:click")
    assert replies[4]["result"]["isError"] and "sellercentral" in replies[4]["result"]["content"][0]["text"]
    assert replies[5]["result"]["isError"] and "seller.tiktok" in replies[5]["result"]["content"][0]["text"]
    assert replies[6]["result"]["isError"] and "没有开放" in replies[6]["result"]["content"][0]["text"]
    assert not replies[7]["result"].get("isError")
    events = [(a, d.get("tool")) for a, d in audits]
    assert ("browser_action", "navigate_page") in events and ("browser_action", "click") in events
    assert events.count(("browser_blocked", "navigate_page")) == 1 and ("browser_blocked", "evaluate_script") in events
    assert ("browser_action", "take_snapshot") not in events                 # 只读的不记


def test_upstream_command_only_connects_to_running_chrome(tmp_path):
    """--userDataDir / --executablePath 和 --browserUrl 同时给，chrome-devtools-mcp 会直接退出（2026-10-30 实测）。"""
    cmd = upstream_command(BrowserSettings(user_data_dir="/tmp/agent-chrome", chrome_path="/x/Chrome"), tmp_path)
    assert cmd[cmd.index("--browserUrl") + 1] == "http://127.0.0.1:9222"
    assert cmd[cmd.index("--workspace") + 1] == str(tmp_path)
    assert "--userDataDir" not in cmd and "--executablePath" not in cmd


def test_upstream_exit_answers_waiting_requests_at_once(tmp_path):
    """上游启动就退出时，在等的请求立刻回错误，不让 agent 干等到连接超时。"""
    script = tmp_path / "dies.py"
    script.write_text("import sys; sys.stdin.readline(); sys.exit(3)")
    guard = ChromeGuard([sys.executable, str(script)], {}, BrowserSettings(timeout_sec=5), lambda a, d: None, "T-1")
    out = io.StringIO()
    reqs = [{"jsonrpc": "2.0", "id": 0, "method": "server/discover"},
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    guard.serve(io.StringIO("".join(json.dumps(r) + "\n" for r in reqs)), out)
    replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert replies[0]["error"]["code"] == -32601                         # 新协议探测由代理直接答
    assert "退出码 3" in replies[1]["error"]["message"]


def fake_ps(listener_args):
    def run(cmd, **kwargs):
        if cmd[0].endswith("lsof"):
            return subprocess.CompletedProcess(cmd, 0, "" if listener_args is None else "4242\n", "")
        return subprocess.CompletedProcess(cmd, 0, listener_args or "", "")
    return run


def test_verify_dedicated_only_accepts_the_configured_profile(tmp_path):
    browser = BrowserSettings(browser_url="http://127.0.0.1:9666", user_data_dir=str(tmp_path / "agent profile"))
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9666"
    ok = f"{chrome} --user-data-dir={tmp_path}/agent profile --no-first-run"   # 目录里带空格也要认得
    assert verify_dedicated(browser, run=fake_ps(ok)) is None
    daily = verify_dedicated(browser, run=fake_ps(chrome))                       # 没指定目录 = 日常 Chrome
    assert "不是专用浏览器" in daily and "日常 Chrome" in daily
    other = verify_dedicated(browser, run=fake_ps(f"{chrome} --user-data-dir=/Users/x/other"))
    assert "/Users/x/other" in other
    assert "没启动" in verify_dedicated(browser, run=fake_ps(None))
