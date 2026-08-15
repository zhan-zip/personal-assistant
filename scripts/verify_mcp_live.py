"""实时验证: 客户端启动 start_mcp_server, 调用 notify_user 给用户发真实通知

模拟 opencode 作为 MCP Client 连接 bot 的 MCP Server 的完整链路。
运行前需先停掉手动运行的 bot.py (避免双连 NapCat)。
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER = 3496326306  # 管理员 QQ


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["start_mcp_server.py"], cwd=ROOT
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("MCP 工具:", names)

            st = await session.call_tool("get_status", {})
            txt = "".join(c.text for c in st.content if hasattr(c, "text"))
            print("get_status(初始):", txt)

            # 等待 bot 连上 NapCat (MCP server 进程内 bot 需要时间连接)
            connected = False
            for _ in range(30):
                st = await session.call_tool("get_status", {})
                txt = "".join(c.text for c in st.content if hasattr(c, "text"))
                if "'onebot': True" in txt or "onebot': True" in txt:
                    connected = True
                    print("bot 已连上 NapCat:", txt)
                    break
                await asyncio.sleep(1)
            if not connected:
                print("警告: 30 秒内 bot 未连上 NapCat, 仍尝试发通知")

            res = await session.call_tool(
                "notify_user",
                {"user_id": str(USER),
                 "message": "MCP 联动验证成功: opencode 已通过 MCP 调用 bot 能力, "
                            "你收到这条消息说明 bot 已被其他 AI 调用 ✓"},
            )
            txt2 = "".join(c.text for c in res.content if hasattr(c, "text"))
            print("notify_user:", txt2)

    print("验证结束, server 子进程已退出")


if __name__ == "__main__":
    asyncio.run(main())
