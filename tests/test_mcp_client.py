"""
MCP client 测试: bot 连接 Food-Time MCP server, 拉取工具并调用
需要 Food-Time 的 MySQL 在运行 (3307)。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp_integration.client import create_food_time_client


async def test_connect_and_call():
    client = create_food_time_client()
    try:
        tool_defs = await client.connect()
        names = [d["function"]["name"] for d in tool_defs]
        print("拉到的工具:", names)
        assert "search_foods" in names, f"缺少 search_foods: {names}"
        assert "get_diet_records" in names
        assert len(tool_defs) >= 6

        # 调用不需要 user 的工具
        r = await client.call("search_foods", {"keyword": "番茄"})
        print("search_foods(番茄):", r[:150])
        assert "番茄" in r, r

        # 调用需要 user 的工具 (user_id=2 是 2259016269@qq.com 的账号)
        r2 = await client.call("get_diet_records", {"food_user_id": 2, "days": 7})
        print("get_diet_records(user=2):", r2[:150])
        assert isinstance(r2, str)
    finally:
        await client.close()
    print("Test 1 OK: Food-Time MCP client 连接+调用")


async def main():
    await test_connect_and_call()
    print("\nAll 1 MCP client test passed!")


if __name__ == "__main__":
    asyncio.run(main())
