"""通用 MCP Client 管理器: 配置驱动的多 server 连接

- 扫描 mcp_integration/configs/*.yaml, 每个文件描述一个 MCP server
- 工具带前缀命名空间并入 tool-calling (food.search_foods / mso.choose_model),
  避免不同 server 工具重名冲突
- 单个 server 连接失败只记 warning, 不影响其他 server 与 bot 启动
- 添加新 MCP server 零代码: 建一个 yaml -> 重启 bot

配置格式 (configs/food_time.yaml):
    name: food_time              # 唯一标识 (也是日志前缀)
    display_name: "饮食记录"      # 可选, 人类可读
    enabled: true
    connection:
      type: stdio
      command: python            # "python" = qq-agent 当前解释器; 也可写绝对路径
      args: ["-m", "src.mcp_integration.server"]
      cwd: "${FOOD_TIME_SERVER_DIR}"   # 支持 ${VAR} 环境变量展开
      env: {}                    # 可选, 注入子进程的环境变量
    tool_prefix: "food"          # 工具前缀命名空间
    auto_connect: true           # 启动时自动连接
"""
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from mcp import ClientSession, StdioServerParameters, stdio_client

from mcp_integration.client import DEFAULT_FOOD_TIME_SERVER_DIR

logger = logging.getLogger("mcp.client_manager")

DEFAULT_CONFIG_DIR = Path(__file__).parent / "configs"

# 旧版兼容: cwd 环境变量未设置时的回退默认路径 (键 = server name)
LEGACY_DEFAULTS = {
    "food_time": DEFAULT_FOOD_TIME_SERVER_DIR,
}

_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def _expand(value: str) -> str:
    """展开字符串中的 ${VAR} 环境变量引用; 变量未设置时替换为空串"""
    if not isinstance(value, str):
        return value
    return _VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


class StdioMCPClient:
    """按配置连接一个 stdio MCP server 的通用客户端"""

    def __init__(self, name: str, connection: Dict[str, Any],
                 tool_prefix: str, display_name: str = ""):
        self.name = name
        self.display_name = display_name or name
        self.connection = connection
        self.tool_prefix = tool_prefix
        self.session: Optional[ClientSession] = None
        self._ctx = None
        self.tool_names: set = set()                 # 该 server 的原始工具名
        self.prefixed_names: set = set()             # 带前缀的工具名 (路由用)
        self.tool_defs: List[Dict[str, Any]] = []    # OpenAI schema (带前缀)

    async def connect(self) -> List[Dict[str, Any]]:
        """启动 MCP server 子进程并拉取工具定义 (工具名加前缀)"""
        conn = self.connection
        command = _expand(conn.get("command", "python"))
        if command == "python":
            command = sys.executable
        args = [_expand(a) for a in conn.get("args", [])]
        cwd = _expand(conn.get("cwd", "")) or LEGACY_DEFAULTS.get(self.name, "")
        env = conn.get("env") or None

        params = StdioServerParameters(
            command=command, args=args, cwd=cwd or None, env=env
        )
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()

        tools = await self.session.list_tools()
        for t in tools.tools:
            self.tool_names.add(t.name)
            prefixed = f"{self.tool_prefix}.{t.name}"
            self.prefixed_names.add(prefixed)
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", {})
            self.tool_defs.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": getattr(t, "description", "") or "",
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            })
        logger.info(
            f"[MCP-client] {self.display_name}({self.name}) 已连接, "
            f"工具: {sorted(self.prefixed_names)}"
        )
        return self.tool_defs

    async def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """调用该 server 的一个工具 (原始名), 返回文本结果"""
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
            logger.warning(f"[MCP-client] {self.name} 关闭失败: {e}")
        self.session = None
        self._ctx = None


class MCPClientManager:
    """管理多个 MCP server client: 加载配置 / 连接 / 工具路由 / 关闭"""

    def __init__(self, config_dir: Optional[Path | str] = None):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.clients: Dict[str, StdioMCPClient] = {}      # name -> client
        self.tool_index: set = set()                      # 全部带前缀工具名
        self._tool_owner: Dict[str, str] = {}             # prefixed name -> client name

    def _load_yaml_configs(self) -> List[Dict[str, Any]]:
        """读取配置目录下全部 yaml (含 disabled 的, 便于日志提示)"""
        configs: List[Dict[str, Any]] = []
        if not self.config_dir.exists():
            return configs
        for path in sorted(self.config_dir.glob("*.yaml")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[MCP-client] 配置读取失败 {path.name}: {e}")
                continue
            if not cfg.get("name"):
                logger.warning(f"[MCP-client] 配置缺 name 字段, 跳过: {path.name}")
                continue
            cfg["_file"] = path.name
            configs.append(cfg)
        return configs

    async def load_all(self) -> List[Dict[str, Any]]:
        """扫描配置目录, 连接全部启用的 server, 返回合并后的工具定义"""
        all_defs: List[Dict[str, Any]] = []
        for cfg in self._load_yaml_configs():
            name = cfg["name"]
            if name in self.clients:
                logger.warning(f"[MCP-client] server 重名, 跳过后加载的: {name}")
                continue
            if not cfg.get("enabled", True):
                logger.info(f"[MCP-client] 已禁用, 跳过: {name} ({cfg['_file']})")
                continue
            if not cfg.get("auto_connect", True):
                logger.info(f"[MCP-client] auto_connect=false, 跳过: {name}")
                continue
            conn = cfg.get("connection") or {}
            if not conn.get("args"):
                logger.warning(f"[MCP-client] 配置缺 connection.args, 跳过: {name}")
                continue
            client = StdioMCPClient(
                name=name,
                connection=conn,
                tool_prefix=cfg.get("tool_prefix") or name,
                display_name=cfg.get("display_name", ""),
            )
            try:
                tool_defs = await client.connect()
            except Exception as e:
                logger.warning(f"[MCP-client] 连接 {name} 失败 (不影响其他 server): {e}")
                try:
                    await client.close()
                except Exception:
                    pass
                continue
            self.clients[name] = client
            for d in tool_defs:
                prefixed = d["function"]["name"]
                self.tool_index.add(prefixed)
                self._tool_owner[prefixed] = name
            all_defs.extend(tool_defs)
        return all_defs

    async def call_tool(self, prefixed_name: str, arguments: Dict[str, Any]) -> str:
        """按前缀路由执行外部工具 (food.search_foods -> food_time client)"""
        owner_name = self._tool_owner.get(prefixed_name)
        if owner_name is None:
            return f"[错误] 未知 MCP 工具: {prefixed_name}"
        client = self.clients.get(owner_name)
        if client is None:
            return f"[错误] MCP server 不在线: {owner_name}"
        real_name = prefixed_name.split(".", 1)[1] if "." in prefixed_name else prefixed_name
        return await client.call(real_name, arguments)

    async def close_all(self):
        for name in list(self.clients):
            try:
                await self.clients[name].close()
            except Exception:
                pass
        self.clients.clear()
        self.tool_index.clear()
        self._tool_owner.clear()
