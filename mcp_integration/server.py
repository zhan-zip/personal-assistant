"""
bot 作为 MCP Server: 暴露能力给其他 agent (opencode / 知识库等)

用途: 双向联动的"被其他项目调用"一侧。
外部 agent (如 opencode) 作为 MCP Client 连接本 Server, 可调用:
- notify_user: 给指定 QQ 用户发私聊消息
- get_status: bot 运行状态
- get_bot_profile: bot 资料
- read_todos: 待办 (占位, Phase 4)

运行方式:
- 独立运行: python -m mcp_integration.server   (创建 QQBot 完整实例 + 启动 MCP Server, stdio)
- 程序内: create_bot_mcp(bot) → server.run_stdio_async()

注意: 模块名用 mcp_integration 而非 mcp, 避免与官方 mcp SDK 包名冲突。
"""
import asyncio
import logging
from typing import Optional

from mcp.server import MCPServer

logger = logging.getLogger("mcp_integration")


def create_bot_mcp(bot=None) -> MCPServer:
    """创建绑定到 bot 实例的 MCP Server (只创建, 不启动)"""
    server = MCPServer(
        name="qq-agent",
        version="0.1",
        description="QQ AI 机器人能力接口: 发消息通知 / 查状态 / 查资料",
        instructions=(
            "这是一个 QQ 机器人的能力接口。"
            "notify_user 可以给指定 QQ 用户发私聊消息, 用于通知用户重要事件。"
        ),
    )

    @server.tool()
    async def notify_user(user_id: str, message: str) -> str:
        """给指定 QQ 用户发一条私聊消息。user_id 是 QQ 号(字符串), message 是要发送的内容。"""
        if bot is None:
            return "错误: bot 实例未就绪"
        try:
            mid = await bot._send_private_msg(int(user_id), message)
            return f"已发送 (message_id={mid})" if mid else "发送失败(可能被节流拦截)"
        except Exception as e:
            return f"发送失败: {e}"

    @server.tool()
    def get_status() -> str:
        """获取 bot 当前运行状态(是否运行 / 各协议在线情况)。"""
        if bot is None:
            return "bot 未就绪"
        adapters = {}
        for name, adp in bot.adapters.items():
            try:
                adapters[name] = bool(getattr(adp, "connected", False))
            except Exception:
                adapters[name] = False
        return f"running={bot.running} adapters={adapters}"

    @server.tool()
    def get_bot_profile() -> str:
        """获取 bot 自己的资料(昵称/签名)。"""
        if bot is None:
            return "bot 未就绪"
        return (f"昵称: {bot.current_nickname or '未知'} | "
                f"签名: {bot.current_signature or '无'}")

    @server.tool()
    async def read_todos(user_id: Optional[str] = None) -> str:
        """读取待办列表。可传 user_id 只看某人的; 不传则返回全部待办 (来自 SQLite)。"""
        if bot is None:
            return "bot 未就绪"
        try:
            if user_id:
                todos = await bot.memory_store.list_todos(str(user_id))
            else:
                todos = await bot.memory_store.list_all_todos()
        except Exception as e:
            return f"读取待办失败: {e}"
        if not todos:
            return "暂无待办"
        lines = ["待办列表:"]
        for t in todos:
            uid = t.get("user_id", "")
            mark = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}.get(t["status"], "[?]")
            lines.append(f"  {mark} #{t['id']} user={uid} {t['content']} ({t['status']})")
        return "\n".join(lines)

    return server


async def run_mcp_stdio(bot=None):
    """启动 MCP Server (stdio 传输), 常驻直到进程结束"""
    server = create_bot_mcp(bot)
    await server.run_stdio_async()


if __name__ == "__main__":
    # 独立运行: 创建完整 QQBot 实例 + MCP Server (stdio)
    async def _main():
        import bot as bot_module
        b = bot_module.QQBot()
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

    asyncio.run(_main())
