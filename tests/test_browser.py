"""测试功能5：只读网页取证"""

import pytest

from ecom_twin.browser import BrowserError, check_url, page_text, find_chrome
from ecom_twin.config import BrowserSettings


def test_check_url_allows_whitelisted_domain():
    """测试白名单域名通过"""
    settings = BrowserSettings()
    result = check_url("https://www.amazon.com/dp/B08N5WRWNW", settings)
    assert result == "https://www.amazon.com/dp/B08N5WRWNW"


def test_check_url_allows_subdomain():
    """测试子域名通过"""
    settings = BrowserSettings()
    result = check_url("https://www.amazon.co.uk/gp/product/B08N5WRWNW", settings)
    assert result is not None


def test_check_url_rejects_non_whitelisted():
    """测试非白名单域名被拒绝"""
    settings = BrowserSettings()
    with pytest.raises(BrowserError, match="不在白名单内"):
        check_url("https://example.com/page", settings)


def test_check_url_rejects_non_http():
    """测试非 http(s) 协议被拒绝"""
    settings = BrowserSettings()
    with pytest.raises(BrowserError, match="只能打开 http"):
        check_url("file:///etc/passwd", settings)
    with pytest.raises(BrowserError, match="只能打开 http"):
        check_url("ftp://amazon.com/x", settings)


def test_check_url_rejects_blocked_keywords():
    """测试含登录/结算关键词的 URL 被拒绝"""
    settings = BrowserSettings()
    with pytest.raises(BrowserError, match="拒绝打开"):
        check_url("https://www.amazon.com/login?x=1", settings)
    with pytest.raises(BrowserError, match="拒绝打开"):
        check_url("https://www.amazon.com/ap/signin", settings)
    with pytest.raises(BrowserError, match="拒绝打开"):
        check_url("https://www.amazon.com/gp/cart", settings)


def test_check_url_rejects_store_backend():
    """测试卖货店铺后台域名被拒绝"""
    settings = BrowserSettings()
    # 后台域名通常也不在白名单内，两道防线任一命中即拒绝
    with pytest.raises(BrowserError) as error:
        check_url("https://seller.tiktokshop.com/home", settings)
    assert "不在白名单内" in str(error.value) or "拒绝打开" in str(error.value)

    # 即使把后台域名加进白名单，关键词/后台标记仍然拒绝
    settings.allowed_domains = ["seller.tiktokshop.com"]
    with pytest.raises(BrowserError, match="拒绝打开"):
        check_url("https://seller.tiktokshop.com/home", settings)


def test_page_text_extracts_title_and_content():
    """测试从 DOM 提取标题和正文"""
    dom = """
    <html>
      <head><title>  测试商品页面  </title></head>
      <body>
        <h1>商品名称</h1>
        <p>价格 99.99 元</p>
        <script>var x = 1;</script>
        <style>body { color: red; }</style>
      </body>
    </html>
    """
    title, text = page_text(dom)
    assert title == "测试商品页面"
    assert "商品名称" in text
    assert "99.99" in text
    # script/style 内容被剥离
    assert "var x" not in text
    assert "color: red" not in text


def test_page_text_truncates():
    """测试正文超长时被截断"""
    dom = "<html><body>" + "A" * 10000 + "</body></html>"
    _, text = page_text(dom, limit=100)
    assert len(text) == 100


def test_page_text_handles_missing_title():
    """测试没有 title 时返回空字符串"""
    title, text = page_text("<html><body>内容</body></html>")
    assert title == ""
    assert "内容" in text


def test_find_chrome_returns_path():
    """测试能找到 Chrome（当前环境是 macOS）"""
    try:
        path = find_chrome()
        assert path
        assert path.endswith("Chrome") or "chrome" in path.lower() or "Chromium" in path
    except BrowserError:
        pytest.skip("当前环境没有安装 Chrome")


def test_find_chrome_raises_when_missing():
    """测试找不到 Chrome 时抛出异常"""
    import ecom_twin.browser as browser_module
    original = browser_module.CHROME_CANDIDATES
    browser_module.CHROME_CANDIDATES = ()
    try:
        with pytest.raises(BrowserError, match="没有找到 Chrome"):
            browser_module.find_chrome("/nonexistent/path/to/chrome")
    finally:
        browser_module.CHROME_CANDIDATES = original


def test_capture_page_rejects_bad_url(tmp_path):
    """测试 capture_page 拒绝非法 URL"""
    from ecom_twin.browser import capture_page
    settings = BrowserSettings()
    with pytest.raises(BrowserError):
        capture_page("https://evil.com/x", tmp_path, settings)


def test_report_step_accepts_evidence(tmp_path, env):
    """测试 report_step 支持 evidence 参数"""
    from ecom_twin.mcp_server import ToolServer
    from ecom_twin.memory import MemoryStore

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, env.task.id)

    result = server.call("report_step", {
        "name": "取证",
        "status": "成功",
        "summary": "已抓取竞品页面",
        "evidence": "/tmp/evidence/capture-123.png",
    })
    assert not result[1]

    # 验证 evidence 写入了 step
    steps = env.app.store.list_steps(env.task.id)
    assert len(steps) == 1
    assert steps[0].evidence == "/tmp/evidence/capture-123.png"


def test_capture_page_tool_integration(tmp_path, env):
    """测试 capture_page 工具在列表中且被拒绝时返回错误"""
    from ecom_twin.mcp_server import ToolServer
    from ecom_twin.memory import MemoryStore

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, env.task.id)

    tools = [t["name"] for t in server.list_tools()]
    assert "capture_page" in tools

    # 非白名单域名应返回错误而不是抛异常
    result, is_error = server.call("capture_page", {"url": "https://evil.com/x"})
    assert not is_error
    assert "不在白名单内" in result


def test_start_step_without_existing_task_does_not_crash(tmp_path, env):
    """回归：任务不存在时 start_step 不应崩溃（MCP server 可能被单独拉起）"""
    from ecom_twin.app import build_app
    from ecom_twin.feishu import FakeFeishu
    from ecom_twin.store import Store
    from ecom_twin.config import Settings
    from pathlib import Path

    settings = Settings(db_path=Path(":memory:"))
    app = build_app(settings, Store(":memory:"), FakeFeishu(), env.miaoshou)

    step = app.tasks.start_step("T-DOES-NOT-EXIST", "取证")
    assert step.name == "取证"

    # heartbeat 同样不应崩溃
    app.tasks.heartbeat("T-DOES-NOT-EXIST")


def test_tool_exception_does_not_kill_mcp_server(tmp_path, env):
    """回归：工具内部抛异常应返回错误而不是让 MCP server 退出"""
    from ecom_twin.mcp_server import ToolServer
    from ecom_twin.memory import MemoryStore

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, "T-NOT-EXIST")

    # report_step 在任务不存在时仍应返回 ok，而不是抛异常
    result, is_error = server.call("report_step", {"name": "取证", "status": "成功"})
    assert not is_error
    assert "ok" in result
