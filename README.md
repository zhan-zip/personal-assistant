# Personal Assistant

一个基于 LLM 的**个人 AI 助理** agent 项目，本地部署、全异步、模块化设计。

> 核心思路：**协议层与 AI 能力层分离**。底层通过协议适配层对接多种聊天渠道（目前：QQ、网页），上层是统一的"私人助理"能力（对话、工具、记忆、主动行动），并通过 MCP 与其他 AI agent 互通。

## 功能特性

- **私人助理能力**（协议无关）：
  - 自然语言对话 + tool-calling 多轮循环，LLM 自主决定是否调用工具
  - 多模型路由与容灾（DeepSeek 主 / 通义千问备用，失败自动切换）
  - 分层记忆：短期对话（SQLite + FTS5）/ 长期事实（per-user facts）/ 向量语义（ChromaDB RAG）
  - 主动行动：主动消息、自动改资料、自动发动态（LLM 判断，可跳过）
  - 反幻觉机制：撤回检测双重路径、输出过滤
- **多协议接入**（协议适配层）：
  - QQ（NapCat / OneBot v11）
  - 自制网页聊天（`http://127.0.0.1:8080`）
  - 架构可扩展其他渠道（如微信）
- **工具调用（15 个）**：联网搜索、图片识别（通义视觉）、资料管理、沙箱文件读写、待办管理、QQ 空间自动化
- **MCP 双向联动**：
  - 作为 **MCP Server** 被其他 AI agent 调用（`notify_user` / `get_status` / `get_bot_profile` / `read_todos`）
  - 作为 **MCP Client** 连接其他本地 agent 的 MCP server，动态并入其工具
- **稳定性**：WebSocket 自动重连、进度消息

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.13+ |
| 架构 | 协议适配层（adapter 模式）+ AI 核心层分离 |
| 协议 | OneBot v11（NapCat）、MCP、WebSocket |
| LLM | DeepSeek / 通义千问（OpenAI 兼容 API） |
| 存储 | SQLite（+ FTS5 中文检索）、ChromaDB（向量） |
| 自动化 | Playwright（QQ 空间） |
| 网络 | aiohttp（全异步） |

## 快速开始

### 前置要求
- Python 3.13+
- （使用 QQ 渠道时）NapCat（QQ 协议桥接）

### 1. 启动 NapCat（仅 QQ 渠道需要）
进入 `napcat_shell/`，双击 `launcher.bat`，用 QQ 小号扫码登录。**登录后不要关闭窗口。**

### 2. 安装依赖
```bash
pip install -r requirements.txt
```

### 3. 配置密钥
复制 `.env.example` 为 `.env`，填入 API Key（详见文件内注释）。

### 4. 启动
```bash
python bot.py
```

## 项目结构

```
protocols/         协议适配层（onebot=QQ / web=网页）→ 统一 Message
core/              核心管线（事件路由 / 消息处理 / 指令 / 记忆 / 进度 / 反幻觉）
llm/               多 provider LLM 路由 + 容灾 + 工具注册中心
mcp_integration/   MCP Server（被外部 agent 调）+ MCP Client（接其他 agent 工具）
core/memory_store  SQLite 存储层（messages / facts / todos / events / FTS5）
core/vector_store  ChromaDB 向量语义记忆（RAG）
qzone/             QQ 空间自动化（Playwright 浏览器）
profile/           资料管理（昵称/签名/头像/背景）
services/          外部服务（视觉模型 / 联网搜索 / 媒体下载）
proactive/         后台主动任务（主动消息 / 自动发动态 / 自动改资料）
```

## 测试

```bash
python tests/test_protocols_mock.py      # 协议层
python tests/test_llm_router.py          # LLM 路由与容灾
python tests/test_memory_store.py        # SQLite 记忆
python tests/test_vector_store.py        # 向量语义记忆（RAG）
python tests/test_mcp_client.py          # MCP Client 接入
python tests/test_mcp_server.py          # MCP Server
python tests/test_tools_phase4.py        # 文件/待办工具
python tests/test_core_regression.py     # 核心回归
python tests/test_nick_pattern.py        # 昵称防护
python tests/test_profile_guard.py       # 资料护栏
```

## 许可
[MIT](LICENSE)
