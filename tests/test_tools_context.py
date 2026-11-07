import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import NOW
from ecom_twin.browser import BrowserError, capture_page, check_url, find_chrome, page_text
from ecom_twin.config import BrowserSettings
from ecom_twin.memory import MemoryStore
from ecom_twin.mcp_server import ToolServer
from ecom_twin.sops import SopLibrary


def test_url_allowlist_and_seller_backend_block():
    s = BrowserSettings()
    assert check_url("https://www.amazon.com/dp/B0TEST", s)
    with pytest.raises(BrowserError, match="白名单"):
        check_url("https://evil.example.com/", s)
    with pytest.raises(BrowserError, match="sellercentral"):
        check_url("https://sellercentral.amazon.com/home", s)
    with pytest.raises(BrowserError, match="login"):
        check_url("https://www.tiktok.com/login", s)
    with pytest.raises(BrowserError, match="http"):
        check_url("file:///etc/passwd", s)


def test_page_text_strips_scripts():
    title, text = page_text("<html><title>Mat &amp; Pad</title><script>evil()</script><body><p>Hello</p></body></html>")
    assert title == "Mat & Pad" and text == "Mat & Pad Hello"


def _chrome_available():
    try:
        find_chrome()
        return True
    except BrowserError:
        return False


@pytest.mark.skipif(not _chrome_available(), reason="本机没有 Chrome")
def test_capture_page_with_real_headless_chrome(tmp_path):
    class Page(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = "<html><head><title>竞品页</title></head><body><h1>Yoga Mat 价格 19.99</h1></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cap = capture_page(f"http://127.0.0.1:{server.server_address[1]}/item", tmp_path,
                           BrowserSettings(allowed_domains=["127.0.0.1"], blocked_keywords=[]))
    finally:
        server.shutdown()
    assert cap.title == "竞品页" and "19.99" in cap.text
    assert cap.screenshot.exists() and cap.screenshot.stat().st_size > 1000


def make_skills(tmp_path):
    d = tmp_path / "skills" / "listing-optimization-expert"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: listing-optimization-expert\ndescription: Listing 优化\n---\n\n# 标题规则\n先写核心词")
    return tmp_path / "skills"


def test_sop_library_index_and_load(tmp_path):
    lib = SopLibrary([make_skills(tmp_path)])
    assert "listing-optimization-expert：Listing 优化" in lib.index_text()
    body = lib.load("listing-optimization-expert")
    assert "先写核心词" in body and "submit_proposal" in body
    with pytest.raises(ValueError):
        lib.load("nope")


def tools(env, tmp_path):
    env.app.settings.skill_dirs = [make_skills(tmp_path)]
    return ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id,
                      evidence_dir=tmp_path / "evidence")


def call(server, name, args):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]["isError"], r["result"]["content"][0]["text"]


def test_shop_context_tool(env, tmp_path):
    err, text = call(tools(env, tmp_path), "get_shop_context", {"shop_key": "tiktok:10001"})
    ctx = json.loads(text)
    assert not err and ctx["在可操作白名单内"] and ctx["妙手店铺信息"]["shopId"] == 10001
    assert ctx["审批人"] == "已配置主审批人和代理审批人"


def test_load_sop_tool(env, tmp_path):
    err, text = call(tools(env, tmp_path), "load_sop", {"name": "listing-optimization-expert"})
    assert not err and "先写核心词" in text


def test_capture_page_tool_records_evidence_step(env, tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    from ecom_twin import mcp_server
    from ecom_twin.browser import Capture
    monkeypatch.setattr(mcp_server, "capture_page", lambda url, out, s: Capture(url, "竞品", "文字", shot))
    err, text = call(tools(env, tmp_path), "capture_page", {"url": "https://www.amazon.com/dp/X", "purpose": "看竞品"})
    assert not err and json.loads(text)["screenshot"] == str(shot)
    step = env.app.store.list_steps(env.task.id)[-1]
    assert step.name == "网页取证：看竞品" and step.evidence == str(shot)


def test_capture_page_tool_rejects_seller_backend(env, tmp_path):
    err, text = call(tools(env, tmp_path), "capture_page", {"url": "https://sellercentral.amazon.com/", "purpose": "x"})
    assert err and "sellercentral" in text
    assert env.app.store.list_steps(env.task.id)[-1].status.value == "失败"


def test_task_card_includes_proposals_and_rejection(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher
    d, _ = dispatcher(env, tmp_path, {})
    p = env.app.approvals.request(env.submit())
    env.app.approvals.handle_message({"message_id": "om", "sender_id": "ou_boss", "content": f"拒绝 {p.id} 1 成本不对"},
                                     now=NOW)
    card = d.task_card(env.task.id)
    assert env.task.id in card and f"提案 {p.id}" in card and "数据不对" in card and "成本不对" in card


def test_system_prompt_has_layers(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher
    env.app.settings.skill_dirs = [make_skills(tmp_path)]
    d, _ = dispatcher(env, tmp_path, {})
    system = d._spec(env.task.id, "x").system_prompt
    for part in ("## 本任务", "listing-optimization-expert", "## 记忆与经验", "按你要做的动作取", env.task.id):
        assert part in system


def test_report_attaches_evidence_from_this_run(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher
    from ecom_twin.runtime import RunEvent
    from test_evidence import png_1x1
    shot = tmp_path / "e.png"
    shot.write_bytes(png_1x1())
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"png")  # 改了扩展名的假图

    def script():
        yield RunEvent("session", {"session_id": "s"})
        step = env.app.tasks.start_step(env.task.id, "网页取证")
        env.app.tasks.finish_step(step, evidence=str(shot))
        step = env.app.tasks.start_step(env.task.id, "后台截图")
        env.app.tasks.finish_step(step, evidence=str(fake))
        yield RunEvent("done", {"ok": True})

    d, _ = dispatcher(env, tmp_path, {"claude_code": [script()]})
    d.run_task(env.task.id, "看看竞品", resume=False)
    images = [m for m in env.feishu.sent if m["type"] == "image"]
    assert len(images) == 1 and images[0]["path"] == str(shot)
    # 交付前自检：坏图不悄悄少一张，汇报里写明哪张、为什么，留审计
    report = next(m["text"] for m in reversed(env.feishu.sent) if m["type"] in ("reply", "text") and "状态：" in m["text"])
    assert "1 张证据图没能附上" in report and "「后台截图」的 fake.png 不是有效的 PNG" in report
    assert "evidence_invalid" in env.app.store.audit_events(env.task.id)
