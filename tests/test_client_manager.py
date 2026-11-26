"""
MCP client_manager 测试: 配置驱动的多 server 连接 + 前缀路由
需要 Food-Time 的 MySQL 在运行 (3307)。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp_integration.client_manager import MCPClientManager, _expand


async def test_env_expand():
    os.environ["MSO_TEST_VAR"] = "hello"
    assert _expand("${MSO_TEST_VAR}/sub") == "hello/sub"
    assert _expand("${NOT_SET_VAR}/x") == "/x"
    assert _expand("plain") == "plain"
    print("Test 0 OK: ${VAR} 展开")


async def test_manager_load_and_route():
    """真实连接 Food-Time (food_time.yaml), 验证前缀并入与路由调用"""
    manager = MCPClientManager()
    try:
        tool_defs = await manager.load_all()
        names = [d["function"]["name"] for d in tool_defs]
        print("并入的工具:", names)
        assert "food.search_foods" in names, f"缺少前缀工具: {names}"
        assert "food.get_diet_records" in names
        assert len(tool_defs) >= 6
        assert "food.search_foods" in manager.tool_index

        # 前缀路由: manager.call_tool -> food_time client
        r = await manager.call_tool("food.search_foods", {"keyword": "番茄"})
        print("food.search_foods(番茄):", r[:150])
        assert "番茄" in r, r

        r2 = await manager.call_tool("food.get_diet_records", {"food_user_id": 2, "days": 7})
        print("food.get_diet_records(user=2):", r2[:150])
        assert isinstance(r2, str)

        # 未知前缀 -> 错误文本 (不抛异常)
        r3 = await manager.call_tool("nope.unknown", {})
        assert r3.startswith("[错误]"), r3
        print("未知前缀返回错误文本:", r3)
    finally:
        await manager.close_all()
    print("Test 1 OK: client_manager 配置加载+前缀路由")


async def main():
    await test_env_expand()
    await test_manager_load_and_route()
    print("\nAll 2 MCP client_manager tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
