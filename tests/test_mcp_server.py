"""
MCP Server 测试
1. 工具注册齐全 (server 实例)
2. 各工具调用返回正确 (mock bot)
3. 端到端 stdio: 客户端 ClientSession 连内联 server, 调用工具
"""
import asyncio
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_integration.server import create_bot_mcp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MockBot:
    running = True
    current_nickname = "WEN"
    current_signature = "测试签名"
    adapters = {"onebot": type("A", (), {"connected": True})()}
    sent = []

    async def _send_private_msg(self, uid, msg):
        self.sent.append((uid, msg))
        return 999


def _text(result):
    """从 CallToolResult 提取文本"""
    try:
        return result.structured_content.get("result", "")
    except Exception:
        for c in result.content:
            t = getattr(c, "text", None)
            if t:
                return t
        return str(result)


async def test_tools_registered():
    s = create_bot_mcp(MockBot())
    tools = await s.list_tools()
    names = [t.name for t in tools]
    assert names == ["notify_user", "get_status", "get_bot_profile", "read_todos"], names
    print("Test 1 OK: 工具注册齐全")


async def test_call_tools():
    bot = MockBot()
    s = create_bot_mcp(bot)
    r1 = await s.call_tool("get_status", {})
    assert "running=True" in _text(r1), _text(r1)
    r2 = await s.call_tool("get_bot_profile", {})
    assert "WEN" in _text(r2), _text(r2)
    r3 = await s.call_tool("read_todos", {})
    assert "Phase 4" in _text(r3), _text(r3)
    r4 = await s.call_tool("notify_user", {"user_id": "3496326306", "message": "hello"})
    assert "已发送" in _text(r4), _text(r4)
    assert bot.sent == [(3496326306, "hello")]
    print("Test 2 OK: 各工具调用")


async def test_stdlib_e2e():
    """端到端 stdio: 客户端连接内联 server 并调用工具"""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    code = textwrap.dedent(f"""
        import asyncio, sys
        sys.path.insert(0, {ROOT!r})
        from mcp_integration.server import create_bot_mcp
        class MockBot:
            running = True
            current_nickname = 'WEN'
            current_signature = 'sig'
            adapters = {{'onebot': type('A', (), {{'connected': True}})()}}
            async def _send_private_msg(self, uid, msg): return 999
        async def main():
            s = create_bot_mcp(MockBot())
            await s.run_stdio_async()
        asyncio.run(main())
    """)
    params = StdioServerParameters(command=sys.executable, args=["-c", code], cwd=ROOT)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert "notify_user" in names and "get_status" in names, names
            res = await session.call_tool("get_status", {})
            txt = "".join(c.text for c in res.content if hasattr(c, "text"))
            assert "running=True" in txt, txt
            res2 = await session.call_tool("notify_user", {"user_id": "1", "message": "hi"})
            txt2 = "".join(c.text for c in res2.content if hasattr(c, "text"))
            assert "已发送" in txt2, txt2
    print("Test 3 OK: stdio 端到端")


async def main():
    await test_tools_registered()
    await test_call_tools()
    await test_stdlib_e2e()
    print("\nAll 3 MCP server tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
