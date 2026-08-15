"""bot 作为 MCP client: 连接 Food-Time 的 MCP server, 把饮食工具并入 tool-calling

- connect(): 用 stdio 启动 Food-Time MCP server 子进程, 建立会话
- 拉取它的工具列表, 转成 OpenAI function-calling schema (供 TOOLS 合并)
- call(): 执行 Food-Time 的工具, 返回文本
"""
import asyncio
import logging
import sys
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters, stdio_client

logger = logging.getLogger("mcp.client")

# Food-Time MCP server 位置 (在 server/ 目录启动, 以便读到它的 .env 连 MySQL)
FOOD_TIME_SERVER_DIR = r"D:\Desktop\test\TreaWork\Food-Time\server"


class FoodTimeClient:
    """连接 Food-Time MCP server 的客户端"""

    def __init__(self):
        self.session: Optional[ClientSession] = None
        self._ctx = None
        self.tool_names: set = set()
        self.tool_defs: List[Dict[str, Any]] = []   # OpenAI schema 格式

    async def connect(self) -> List[Dict[str, Any]]:
        """启动 Food-Time MCP server 并拉取工具定义"""
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.mcp_integration.server"],
            cwd=FOOD_TIME_SERVER_DIR,
        )
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()

        tools = await self.session.list_tools()
        for t in tools.tools:
            self.tool_names.add(t.name)
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", {})
            self.tool_defs.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            })
        logger.info(f"[MCP-client] Food-Time 已连接, 工具: {sorted(self.tool_names)}")
        return self.tool_defs

    async def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """调用 Food-Time 的一个工具, 返回文本结果"""
        result = await self.session.call_tool(name, arguments or {})
        parts = [c.text for c in result.content if hasattr(c, "text")]
        return "\n".join(parts) if parts else str(result)

    async def close(self):
        try:
            if self.session:
                await self.session.__aexit__(None, None, None)
            if self._ctx:
                await self._ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"[MCP-client] 关闭失败: {e}")
        self.session = None
        self._ctx = None


def create_food_time_client() -> FoodTimeClient:
    """工厂: 创建 Food-Time MCP client"""
    return FoodTimeClient()
