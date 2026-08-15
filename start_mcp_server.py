"""
MCP Server 启动脚本 (供 opencode 等 MCP 客户端作为子进程调用)

用法: python start_mcp_server.py
- 自动切换到项目根目录, 确保 import 正常
- 创建完整 QQBot 实例 (连 NapCat + 网页) + 启动 MCP Server (stdio)
- MCP 协议走 stdout, bot 日志走 stderr, 互不冲突

注意: 该进程会占用 NapCat 连接。若已手动运行 bot.py, 请先停止, 避免双连接。
"""
import asyncio
import os
import sys


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    import bot as bot_module
    from mcp_integration.server import run_mcp_stdio

    b = bot_module.QQBot()

    async def _run():
        bot_task = asyncio.create_task(b.run())
        try:
            await run_mcp_stdio(b)
        finally:
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass
            await b._shutdown()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
