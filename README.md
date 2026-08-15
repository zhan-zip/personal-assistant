# qq-agent

基于 DeepSeek + NapCat 的 QQ 智能机器人，正从「聊天机器人」演进为「私人助理」：多协议接入、MCP 联动、SQLite 记忆、文件/待办工具。

## 功能

- **对话**：DeepSeek 主对话 + tool-calling（15 个工具），LLM 多模型路由与容灾（主失败自动切备用）
- **多协议**：QQ（NapCat/OneBot）+ 自制网页聊天（`http://127.0.0.1:8080`）；协议适配层（`protocols/`）可扩展微信等
- **记忆**：SQLite 存储（会话历史 / 长期事实 / 待办）+ FTS5 中文关键词检索；`#重置` 清短期+长期
- **工具**：联网搜索、图片识别（通义视觉）、QZone 动态、资料管理、沙箱文件读写、待办管理
- **MCP**：bot 作为 MCP Server 可被其他 AI agent 调用（`notify_user` 发消息 / `get_status` / `get_bot_profile` / `read_todos`），opencode 实测联动通过
- **其他**：撤回检测、主动消息、自动改资料/发动态、进度消息、WebSocket 自动重连、反幻觉机制

## 架构

```
protocols/        协议适配层（onebot=QQ / web=网页）→ 统一 Message
core/             核心管线（事件路由 / 消息处理 / 指令 / 记忆 / 进度）
llm/              多 provider LLM 路由 + 工具注册中心（15 个工具）
mcp_integration/  MCP Server（被其他 agent 调用）
core/memory_store SQLite 存储层（messages / facts / todos / events / FTS5）
```

## 快速开始

1. 启动 NapCat（`napcat_shell/launcher.bat`），QQ 小号扫码登录
2. 安装依赖：`pip install -r requirements.txt`
3. 配置密钥：复制 `.env.example` 为 `.env` 填入 API Key
4. 启动：
   - 普通：`python bot.py`（QQ + 网页 http://127.0.0.1:8080）
   - opencode 托管（含 MCP）：`python start_mcp_server.py`（见 `.opencode/opencode.json`）

## 测试

```
python tests/test_protocols_mock.py      # 协议层 8 项
python tests/test_llm_router.py          # LLM 路由 5 项
python tests/test_memory_store.py        # SQLite 记忆 4 项
python tests/test_mcp_server.py          # MCP Server 3 项
python tests/test_tools_phase4.py        # 文件/待办工具 2 项
python tests/test_core_regression.py     # 核心回归 3 项
python tests/test_nick_pattern.py        # 昵称防护 3 项
python tests/test_profile_guard.py       # 资料护栏 5 项
```

## 文档

- `docs/qq-ai-project-summary.md` — 项目交接摘要（各轮变更记录）
- `docs/TODO.md` — 路线图（Phase 1-6 + LLM）
- `docs/architecture-design.md` — 架构设计（私人助理演进方向）
- `docs/tutorial.md` — 学习教程（消息链路/机制详解）
