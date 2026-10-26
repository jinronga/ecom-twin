"""测试功能4：店铺背景查询"""

from ecom_twin.config import Settings, Limits, Approver
from ecom_twin.mcp_server import ToolServer
from ecom_twin.memory import MemoryStore


def test_get_shop_context_in_whitelist(tmp_path, env):
    """测试查询在白名单内的店铺"""
    env.app.settings.limits.allowed_shops = ["tiktok:10001"]
    env.app.settings.approvers["tiktok:10001"] = Approver(
        primary="ou_xxx_primary",
        delegate="ou_xxx_delegate"
    )

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, "T-0001")

    result = server._get_shop_context({"shop_key": "tiktok:10001"})

    assert result["shop_key"] == "tiktok:10001"
    assert result["in_whitelist"] is True
    assert result["approver"]["primary"] == "ou_xxx_primary"
    assert result["approver"]["delegate"] == "ou_xxx_delegate"
    assert result["limits"]["max_daily_writes"] == 50
    assert result["limits"]["min_margin_rate"] == 0.15
    assert "warning" not in result


def test_get_shop_context_not_in_whitelist(tmp_path, env):
    """测试查询不在白名单内的店铺"""
    env.app.settings.limits.allowed_shops = ["tiktok:10001"]

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, "T-0001")

    result = server._get_shop_context({"shop_key": "tiktok:99999"})

    assert result["in_whitelist"] is False
    assert "warning" in result
    assert "不在白名单内" in result["warning"]


def test_get_shop_context_no_approver(tmp_path, env):
    """测试查询未配置审批人的店铺"""
    env.app.settings.limits.allowed_shops = ["tiktok:10001"]
    # 移除审批人配置
    if "tiktok:10001" in env.app.settings.approvers:
        del env.app.settings.approvers["tiktok:10001"]

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, "T-0001")

    result = server._get_shop_context({"shop_key": "tiktok:10001"})

    assert result["approver"]["primary"] is None
    assert result["approver"]["delegate"] is None
    assert "warning" in result
    assert "未配置审批人" in result["warning"]


def test_get_shop_context_with_erp_info(tmp_path, env):
    """测试从 ERP 查询店铺信息"""
    env.app.settings.limits.allowed_shops = ["tiktok:10001"]

    # 设置 FakeMiaoshou 返回店铺信息
    env.miaoshou.shops = [
        {"shop_id": 10001, "shop_name": "测试店铺", "site": "US", "status": "active"}
    ]

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, "T-0001")

    result = server._get_shop_context({"shop_key": "tiktok:10001"})

    assert result["erp"] is not None
    assert result["erp"]["shop_name"] == "测试店铺"
    assert result["erp"]["site"] == "US"
    assert result["erp"]["status"] == "active"


def test_get_shop_context_erp_unavailable(tmp_path, env):
    """测试 ERP 不可用时的降级处理"""
    env.app.settings.limits.allowed_shops = ["tiktok:10001"]

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, None, "T-0001")  # miaoshou = None

    result = server._get_shop_context({"shop_key": "tiktok:10001"})

    # 即使 ERP 不可用，其他信息仍应返回
    assert result["in_whitelist"] is True
    assert result["erp"] is None


def test_shop_context_tool_integration(tmp_path, env):
    """测试 get_shop_context 工具集成"""
    env.app.settings.limits.allowed_shops = ["tiktok:10001"]
    env.app.settings.approvers["tiktok:10001"] = Approver(primary="ou_xxx")

    memory = MemoryStore(tmp_path / "memory")
    server = ToolServer(env.app, memory, env.miaoshou, "T-0001")

    # 验证工具在工具列表中
    tools = server.list_tools()
    tool_names = [t["name"] for t in tools]
    assert "get_shop_context" in tool_names

    # 验证可以通过 call 调用
    result, is_error = server.call("get_shop_context", {"shop_key": "tiktok:10001"})
    assert not is_error
    assert "tiktok:10001" in result
