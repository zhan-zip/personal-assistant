"""
工具注册中心: 所有 LLM 可调用的工具定义 + 执行分发

架构:
- TOOLS: OpenAI function calling 格式的工具清单, 注入 system prompt
- execute_tool(bot, tool_name, arguments) -> str: 统一执行入口

添加新工具只需:
1. 在 TOOLS 列表追加定义
2. 在 execute_tool 的 dispatch 里加一个分支
"""

import json
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.utils import BEIJING_TZ

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("tools")

# ── 工具定义 (OpenAI function calling schema) ──────────────

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "获取当前的日期和时间 (北京时间)。"
                "当用户问「几点了」「今天几号」「现在什么时候」或需要确认当前时间时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "联网搜索实时信息。"
                "当用户询问最新新闻、实时数据、需要查证的具体事实时调用。"
                "注意: 如果对话上下文中已有搜索结果, 不需要重复搜索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或问题",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": (
                "查询指定 QQ 用户的基本资料 (昵称、性别、年龄、等级、头像描述等)。"
                "当用户问「xxx是谁」「查一下xxx的资料」或需要了解对话对象的身份信息时调用。"
                "注意: 工具返回的字段是真实数据，未返回的字段（如生日=未设置）不要编造。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "integer",
                        "description": "要查询的 QQ 号",
                    },
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_qq",
            "description": (
                "解析一个 QQ 号的真实身份。按优先级查找：\n"
                "① 判断是否机器人自己 → ② 判断是否当前对话用户 → ③ 查好友列表 → ④ 查陌生人资料\n"
                "返回：真实昵称、来源（self/current_user/friend/stranger/not_found）\n"
                "重要：对于任何涉及「查看某个QQ号空间」「看看XXX的空间」的请求，"
                "你必须先调用 resolve_qq 获取该QQ号的真实身份和昵称，"
                "严禁在未调用工具时编造昵称或推断空间状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_uin": {
                        "type": "integer",
                        "description": "要解析的 QQ 号",
                    },
                },
                "required": ["target_uin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bot_profile",
            "description": (
                "获取你自己 (机器人) 的当前资料: 昵称、个性签名、头像描述等。"
                "当用户问「你叫什么」「你的签名是什么」「你的头像」时调用。"
                "注意: 如果对话消息中已经附带了你的资料信息, 直接基于已有信息回复。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_qzone_feeds",
            "description": (
                "获取 QQ 空间动态。根据用户意图选择对应的 mode:\n"
                "- mode='self': 用户要看你(机器人)自己的空间动态。用户说\"你的空间\"时使用\n"
                "- mode='target': 用户要看某个指定QQ的空间动态, 需填写 target_uin\n"
                "  · 用户说\"我的空间\"→ target_uin=<当前对话QQ号>\n"
                "  · 用户说\"看看123456的空间\"→ target_uin=123456\n"
                "  · **强制要求**: 调用前必须先调用 resolve_qq(target_uin) 验证QQ号身份!\n"
                "    - 如果 resolve_qq 返回 source='self', 改用 mode='self' 不传 target_uin\n"
                "    - 如果 resolve_qq 返回 source='not_found', 告知用户该QQ不存在, 不调用此工具\n"
                "    - 其他情况正常传入 target_uin\n"
                "- mode='friend_circle': 用户要看\"好友圈\"\"好友动态\"时使用\n\n"
                "重要: 如果用户只说\"看看空间\"而没明确要看哪一种, 不要猜测, "
                "直接回复询问\"你想看我的空间动态，还是你自己的动态，或是某个好友的空间(告诉QQ号)，"
                "还是好友圈呢？\" 等待用户确认后再调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "模式: 'self'=自己的动态, 'friend_circle'=好友圈动态, 'target'=指定QQ号的动态",
                        "enum": ["self", "friend_circle", "target"],
                    },
                    "target_uin": {
                        "type": "string",
                        "description": "目标QQ号 (仅 mode='target' 时需要)",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_qzone_feed",
            "description": (
                "发一条 QQ 空间动态。"
                "当用户说「帮我发一条空间动态」「发个说说」等内容时调用。"
                "注意: 内容不要太长, 控制在200字以内。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要发布的动态内容",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recognize_image",
            "description": (
                "识别一张图片的内容 (通过视觉模型)。"
                "当用户问「这张图是什么」「帮我看看这个图片」以及收到图片需要描述时调用。"
                "参数 image_url 可以是公开可访问的图片URL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_url": {
                        "type": "string",
                        "description": "图片的 URL 地址 (必须是 http/https 开头)",
                    },
                },
                "required": ["image_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取沙箱工作目录里的一个文件内容。"
                "当用户让你读取某个文件/笔记/文档内容时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名 (相对沙箱工作目录, 如 notes/idea.txt)",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "在沙箱工作目录里写入一个文件 (覆盖已有内容)。"
                "当用户让你记录/保存内容到文件时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名 (相对沙箱工作目录)",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整内容",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "在沙箱工作目录里追加内容到文件末尾 (不覆盖已有内容)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名 (相对沙箱工作目录)",
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加的内容",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "列出沙箱工作目录里的文件。"
                "当用户问「你有哪些文件/笔记」「看看记录」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "子目录路径 (默认空=根目录)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_todo",
            "description": (
                "新增一条待办事项。当用户说「记住/帮我记个待办/我要做X」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "待办内容",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": (
                "列出当前用户的待办事项。当用户问「我有什么待办/要做的事」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_todo",
            "description": (
                "更新一条待办的状态 (pending/done/cancelled)。"
                "当用户说「这件事做完了/取消了/标记完成」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todo_id": {
                        "type": "integer",
                        "description": "待办 id (来自 list_todos)",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "done", "cancelled"],
                        "description": "新状态",
                    },
                },
                "required": ["todo_id", "status"],
            },
        },
    },
]

# ── 工具可读名称 (用于 system prompt) ──────────────────────

TOOLS_README = """
【可用工具清单 - 以下是获取真实信息的唯一途径，严禁绕过工具编造】

1. get_current_time — 获取当前北京时间
2. search_web(query) — 联网搜索
3. resolve_qq(target_uin) — **解析QQ号身份（调用前必须先调此工具）**
   - 按优先级: 自己→对话用户→好友列表→陌生人资料
   - 返回真实昵称+来源
4. get_user_info(user_id) — 查询QQ用户详细资料
5. get_bot_profile() — 获取你自己的真实资料（昵称、签名）
   - 用户问"你叫什么""你的签名""你的昵称""你的个签""你的性别"必须调用
6. get_qzone_feeds(mode) — 获取QQ空间动态
   - mode='self': 机器人自己的动态
   - mode='friend_circle': 好友圈动态
   - mode='target'(+target_uin): 指定QQ号的动态（必须先用 resolve_qq）
7. publish_qzone_feed(content) — 发QQ空间动态
8. recognize_image(image_url) — 视觉识别图片内容
9. read_file(filename) / write_file(filename, content) / append_file(filename, content) / list_files(path) — 读写沙箱工作目录里的文件(笔记/记录)
10. create_todo(content) / list_todos() / update_todo(id, status) — 待办事项管理

【调用规则】
- 涉及「空间/动态/说说/好友圈/看看XXX」→ 先 resolve_qq 再 get_qzone_feeds
- 涉及「昵称/签名/个签/性别/QQ号/个人资料」→ 调用 get_bot_profile
- 涉及「查查XXX/XXX是谁」→ 调用 resolve_qq 或 get_user_info
- 涉及「读文件/看笔记/记录/保存」→ 调用 read_file/write_file/append_file/list_files
- 涉及「待办/要做的事/记一下/做完/取消」→ 调用 create_todo/list_todos/update_todo
- 搜索结果返回多个疑似用户时，列出所有结果让用户选择

【歧义处理】
- "看看空间"未明确哪种 → 询问"我的、你的、某个好友的还是好友圈？"
- "看看我的空间" → 先 resolve_qq(<对话QQ>), 再 mode='target'
- "看看你的空间" → mode='self'
- "看看123456的空间" → 先 resolve_qq(123456)
- "看看好友圈" → mode='friend_circle'
- 工具返回的原始数据请用自然语言总结后输出
"""

# ── 工具执行分发 ──────────────────────────────────────────


def _safe_workspace_path(bot, filename: str) -> Optional[str]:
    """解析沙箱内文件绝对路径; 越出沙箱返回 None"""
    ws = bot.config.get("workspace", {}).get("dir", "workspace")
    base = os.path.realpath(ws)
    if not os.path.exists(base):
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            return None
    target = os.path.realpath(os.path.join(base, filename or ""))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


async def _exec_file_tool(bot, tool_name: str, arguments: Dict) -> str:
    """沙箱文件工具: read_file / write_file / append_file / list_files"""
    if tool_name == "list_files":
        sub = str(arguments.get("path", "") or "")
        base = os.path.realpath(bot.config.get("workspace", {}).get("dir", "workspace"))
        os.makedirs(base, exist_ok=True)
        target = os.path.realpath(os.path.join(base, sub)) if sub else base
        if target != base and not target.startswith(base + os.sep):
            return "[错误] 路径越出沙箱目录"
        if not os.path.isdir(target):
            return "[错误] 目录不存在"
        items = sorted(os.listdir(target))
        if not items:
            return "目录为空"
        lines = []
        for it in items:
            full = os.path.join(target, it)
            kind = "目录" if os.path.isdir(full) else "文件"
            lines.append(f"- {it} ({kind})")
        return "\n".join(lines)

    filename = arguments.get("filename", "")
    path = _safe_workspace_path(bot, filename)
    if not path:
        return "[错误] 文件路径无效或越出沙箱目录"

    if tool_name == "read_file":
        if not os.path.isfile(path):
            return f"[错误] 文件不存在: {filename}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return f"[错误] 读取失败: {e}"
        return content[:2000] + ("\n...(内容过长已截断)" if len(content) > 2000 else "")

    content = str(arguments.get("content", ""))
    if tool_name == "write_file":
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"已写入 {filename}"
        except Exception as e:
            return f"[错误] 写入失败: {e}"
    if tool_name == "append_file":
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + content if os.path.exists(path) and os.path.getsize(path) else content)
            return f"已追加到 {filename}"
        except Exception as e:
            return f"[错误] 追加失败: {e}"
    return "[错误] 未知文件操作"


async def _exec_todo_tool(bot, tool_name: str, arguments: Dict, user_id: int) -> str:
    """待办工具: create_todo / list_todos / update_todo (走 SQLite todos 表)"""
    store = bot.memory_store
    if tool_name == "create_todo":
        content = str(arguments.get("content", "")).strip()
        if not content:
            return "[错误] 待办内容不能为空"
        from core.utils import now_iso
        todo_id = await store.add_todo(str(user_id), content[:200], now_iso())
        return f"已添加待办 #{todo_id}: {content[:80]}"

    if tool_name == "list_todos":
        todos = await store.list_todos(str(user_id))
        if not todos:
            return "暂无待办事项"
        lines = ["当前待办:"]
        for t in todos:
            mark = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}.get(t["status"], "[?]")
            lines.append(f"  {mark} #{t['id']} {t['content']} ({t['status']})")
        return "\n".join(lines)

    if tool_name == "update_todo":
        try:
            todo_id = int(arguments.get("todo_id"))
        except (TypeError, ValueError):
            return "[错误] todo_id 必须是数字"
        status = str(arguments.get("status", "")).strip()
        if status not in ("pending", "done", "cancelled"):
            return "[错误] status 只能是 pending/done/cancelled"
        ok = await store.update_todo(todo_id, status)
        return f"已更新待办 #{todo_id} → {status}" if ok else f"[错误] 待办 #{todo_id} 不存在"
    return "[错误] 未知待办操作"


async def execute_tool(bot: "QQBot", tool_name: str,
                       arguments: Dict[str, Any],
                       user_id: int = 0) -> str:
    """执行单个工具, 返回结果文本 (给 LLM)

    Args:
        bot: QQBot 实例
        tool_name: 工具名
        arguments: LLM 传入的参数
        user_id: 触发该工具调用的用户 QQ 号 (用于管理员通知等)
    """
    try:
        if tool_name == "get_current_time":
            now = datetime.now(BEIJING_TZ)
            return (
                f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(星期{['一','二','三','四','五','六','日'][now.weekday()]})"
            )

        elif tool_name == "search_web":
            query = arguments.get("query", "")
            if not query:
                return "[错误] 搜索关键词不能为空"
            result = await bot._web_search(query)
            return result or "[搜索未返回结果]"

        elif tool_name == "get_user_info":
            user_id = arguments.get("user_id")
            if not user_id:
                return "[错误] 未提供 user_id"
            data = await bot._send_ws_request(
                "get_stranger_info",
                {"user_id": int(user_id), "no_cache": False},
                timeout=8,
            )
            if not data or not isinstance(data, dict):
                return f"未查询到用户 {user_id} 的资料"

            logger.info(
                f"[get_user_info] raw data for {user_id}: "
                f"keys={list(data.keys())} "
                f"user_id_in_data={data.get('user_id')!r}"
            )

            # 交叉验证: 如果 data 中有 user_id 字段, 但与请求的不一致 → 模糊匹配
            returned_uid = data.get("user_id")
            if returned_uid is not None:
                try:
                    if int(returned_uid) != int(user_id):
                        return (
                            f"[QQ号不匹配] 查询 QQ {user_id} 时返回了 QQ {returned_uid} "
                            f"的资料，说明该 QQ 号不存在或已注销。"
                            f"请用自然语言告知用户：这个 QQ 号查不到，确认一下是否输错了。"
                        )
                except (ValueError, TypeError):
                    pass

            parts = []
            if data.get("nickname"):
                parts.append(f"昵称: {data['nickname']}")

            # 性别: 兼容字符串和数字格式
            sex_val = data.get("sex")
            if sex_val is not None:
                sex_map_str = {"male": "男", "female": "女", "unknown": "未知"}
                sex_map_int = {0: "男", 1: "女", 2: "未知", 255: "未知"}
                if isinstance(sex_val, str):
                    parts.append(f"性别: {sex_map_str.get(sex_val, sex_val)}")
                elif isinstance(sex_val, int):
                    parts.append(f"性别: {sex_map_int.get(sex_val, str(sex_val))}")
                else:
                    parts.append(f"性别: {sex_val}")

            # 签名: NapCat 返回 longNick 长昵称作为签名
            signature = data.get("longNick") or data.get("long_nick") or data.get("sign")
            if signature:
                parts.append(f"个性签名: {signature}")

            if data.get("age"):
                parts.append(f"年龄: {data['age']}")

            # 生日: 始终展示（未填则标注"未设置"），方便后续生日祝福等功能
            by = data.get("birthday_year")
            bm = data.get("birthday_month")
            bd = data.get("birthday_day")
            logger.info(
                f"[get_user_info] birthday raw: "
                f"by={by!r}({type(by).__name__}) "
                f"bm={bm!r}({type(bm).__name__}) "
                f"bd={bd!r}({type(bd).__name__})"
            )
            has_valid_birthday = False
            if by is not None and bm is not None and bd is not None:
                try:
                    by_int = int(by)
                except (ValueError, TypeError):
                    by_int = 0
                try:
                    bm_int = int(bm)
                except (ValueError, TypeError):
                    bm_int = 0
                try:
                    bd_int = int(bd)
                except (ValueError, TypeError):
                    bd_int = 0
                # 过滤: 全0 或 年份<1900 (明显的无效值)
                # 注意: 2008-01-06 可能是真实生日，不再过滤
                is_default = (
                    (by_int == 0 and bm_int == 0 and bd_int == 0)
                    or by_int < 1900
                )
                logger.info(
                    f"[get_user_info] birthday parsed: "
                    f"by_int={by_int} bm_int={bm_int} bd_int={bd_int} "
                    f"is_default={is_default}"
                )
                if not is_default:
                    parts.append(f"生日: {by}年{bm}月{bd}日")
                    has_valid_birthday = True
                else:
                    logger.info(
                        f"[get_user_info] 生日为默认值，标记未设置: "
                        f"{by_int}-{bm_int:02d}-{bd_int:02d}"
                    )
            if not has_valid_birthday:
                parts.append("生日: 未设置")

            # 星座: 0、12、"0"、"12"、"unknown" 等是QQ默认值
            constellation = data.get("constellation")
            if constellation is not None:
                # 过滤默认星座 (QQ 默认把 2008-01-06 映射为星座12=摩羯座, 或0=未设置)
                cons_str = str(constellation).strip()
                if cons_str not in ("0", "12", "unknown", ""):
                    parts.append(f"星座: {constellation}")

            if data.get("level"):
                parts.append(f"等级: {data['level']}")

            # VIP 信息
            if data.get("is_vip") or data.get("is_years_vip"):
                vip_level = data.get("vip_level", "")
                vip_info = "VIP"
                if data.get("is_years_vip"):
                    vip_info = "年费VIP"
                if vip_level:
                    vip_info += f" Lv{vip_level}"
                parts.append(f"会员: {vip_info}")

            # 头像 OCR 识别: 构造头像 URL 并调用视觉模型
            try:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                desc = await bot._call_vision(avatar_url)
                if desc and "失败" not in desc and "无法" not in desc:
                    parts.append(f"\n头像描述: {desc}")
            except Exception as e:
                logger.warning(f"[get_user_info] 头像OCR失败 (user={user_id}): {e}")

            # 如果没有 user_id 字段且无有用信息, 可能是模糊匹配或无此用户
            if not parts:
                return f"[QQ号不存在] QQ {user_id} 不存在或无法访问。请用自然语言告知用户。"
            return f"用户 {user_id} 的资料:\n" + "\n".join(parts)

        elif tool_name == "resolve_qq":
            target_uin = arguments.get("target_uin")
            if not target_uin:
                return "[错误] 未提供 target_uin"
            target_uin_int = int(target_uin)

            # ① 判断是否机器人自己
            bot_self_id = str(bot.config.get("self_id", "") or "")
            if bot_self_id and int(bot_self_id) == target_uin_int:
                return json.dumps({
                    "nickname": bot.current_nickname or "机器人",
                    "source": "self",
                    "qq": target_uin_int,
                }, ensure_ascii=False)

            # ② 判断是否当前对话用户
            if user_id and int(user_id) == target_uin_int:
                user_info = await bot._send_ws_request(
                    "get_stranger_info",
                    {"user_id": target_uin_int, "no_cache": False},
                    timeout=8,
                )
                nickname = "未知"
                if user_info and isinstance(user_info, dict):
                    nickname = user_info.get("nickname", "未知")
                return json.dumps({
                    "nickname": nickname,
                    "source": "current_user",
                    "qq": target_uin_int,
                }, ensure_ascii=False)

            # ③ 查好友列表
            try:
                friend_list = await bot._send_ws_request(
                    "get_friend_list", timeout=10
                )
                if friend_list and isinstance(friend_list, list):
                    for friend in friend_list:
                        if not isinstance(friend, dict):
                            continue
                        fid = friend.get("user_id")
                        if fid is not None and int(fid) == target_uin_int:
                            return json.dumps({
                                "nickname": friend.get("nickname", "未知"),
                                "remark": friend.get("remark", ""),
                                "source": "friend",
                                "qq": target_uin_int,
                            }, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"resolve_qq 好友列表查询失败: {e}")

            # ④ 查陌生人资料（带交叉验证）
            try:
                stranger_info = await bot._send_ws_request(
                    "get_stranger_info",
                    {"user_id": target_uin_int, "no_cache": False},
                    timeout=8,
                )
                if stranger_info and isinstance(stranger_info, dict):
                    returned_uid = stranger_info.get("user_id")
                    if returned_uid is not None and int(returned_uid) == target_uin_int:
                        return json.dumps({
                            "nickname": stranger_info.get("nickname", "未知"),
                            "source": "stranger",
                            "qq": target_uin_int,
                            "sex": stranger_info.get("sex", "unknown"),
                        }, ensure_ascii=False)
                    elif returned_uid is not None:
                        return json.dumps({
                            "source": "not_found",
                            "qq": target_uin_int,
                            "reason": (
                                f"查询QQ {target_uin_int} 时返回了QQ {returned_uid} "
                                f"的资料，说明该QQ号不存在或已注销"
                            ),
                        }, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"resolve_qq 陌生人查询失败: {e}")

            return json.dumps({
                "source": "not_found",
                "qq": target_uin_int,
                "reason": f"未找到QQ {target_uin_int} 的任何信息",
            }, ensure_ascii=False)

        elif tool_name == "get_bot_profile":
            # 从 ProfileManager 获取 bot 当前资料
            nickname = bot.current_nickname or "未设置"
            signature = bot.current_signature or "未设置"
            # 尝试获取最新资料 (可能有变化)
            info = await bot._send_ws_request("get_login_info", timeout=5)
            bot_uin = ""
            if info and isinstance(info, dict):
                nickname = info.get("nickname", nickname)
                bot_uin = str(info.get("user_id", ""))
            stranger = await bot._send_ws_request(
                "get_stranger_info",
                {"user_id": info.get("user_id") if info else None, "no_cache": False},
                timeout=5,
            )
            if stranger and isinstance(stranger, dict):
                if stranger.get("nickname"):
                    nickname = stranger["nickname"]
                if stranger.get("longNick") or stranger.get("long_nick"):
                    signature = stranger.get("longNick") or stranger.get("long_nick") or signature

            parts = [
                f"你的当前资料:\n昵称: {nickname}\nQQ号: {bot_uin or '未知'}\n个性签名: {signature}"
            ]

            # 尝试获取头像并做 OCR
            if bot_uin:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={bot_uin}&s=640"
                try:
                    desc = await bot._call_vision(avatar_url)
                    if desc and "失败" not in desc:
                        parts.append(f"\n头像描述: {desc}")
                except Exception as e:
                    logger.warning(f"get_bot_profile 头像OCR失败: {e}")

            return "\n".join(parts)

        elif tool_name == "get_qzone_feeds":
            mode = arguments.get("mode", "self")
            target_uin = arguments.get("target_uin") if mode == "target" else None
            # 防御性验证: LLM 应该已通过 resolve_qq 验证, 这里做兜底
            if mode == "target" and target_uin:
                # 自识别: 目标是自己, 无需额外验证
                bot_self_id = str(bot.config.get("self_id", "") or "")
                if bot_self_id and str(target_uin) == bot_self_id:
                    logger.info(f"[get_qzone_feeds] 目标 {target_uin} 是机器人自己")
                else:
                    try:
                        user_info = await bot._send_ws_request(
                            "get_stranger_info",
                            {"user_id": int(target_uin), "no_cache": False},
                            timeout=8,
                        )
                    except Exception:
                        user_info = None
                    if not user_info or not isinstance(user_info, dict):
                        return (
                            f"[QQ号验证失败] 未查询到 QQ {target_uin} 的用户信息，"
                            f"该QQ号可能不存在。请用自然语言告知用户。"
                        )
                    # 交叉验证: 返回的 user_id 必须匹配
                    returned_uid = user_info.get("user_id")
                    if returned_uid is not None:
                        try:
                            if int(returned_uid) != int(target_uin):
                                return (
                                    f"[QQ号不匹配] 查询 QQ {target_uin} 时返回了 QQ {returned_uid} "
                                    f"的资料，说明该 QQ 号不存在。请用自然语言告知用户。"
                                )
                        except (ValueError, TypeError):
                            pass
            result = await bot._handle_qzone_feeds(
                target_uin=target_uin, mode=mode,
                user_id=user_id,
            )
            return result or "[QZone 未返回动态]"

        elif tool_name == "publish_qzone_feed":
            content = arguments.get("content", "")
            if not content:
                return "[错误] 动态内容不能为空"
            if len(content) > 200:
                content = content[:200]
            result = await bot._handle_qzone_publish(content, user_id)
            return result or "[发布失败]"

        elif tool_name == "recognize_image":
            image_url = arguments.get("image_url", "")
            if not image_url or not image_url.startswith("http"):
                return "[错误] 请提供有效的图片 URL (http/https开头)"
            desc = await bot._call_vision(image_url)
            if desc:
                return f"[图片识别结果]:\n{desc}"
            return "[图片识别失败, 可能URL不可访问或不是有效的图片]"

        elif tool_name in ("read_file", "write_file", "append_file", "list_files"):
            return await _exec_file_tool(bot, tool_name, arguments)

        elif tool_name in ("create_todo", "list_todos", "update_todo"):
            return await _exec_todo_tool(bot, tool_name, arguments, user_id)

        # MCP 外部工具 (如 Food-Time 饮食工具), 由 bot 启动时动态并入
        mcp_clients = getattr(bot, "mcp_clients", None)
        if mcp_clients:
            for _name, _client in mcp_clients.items():
                if tool_name in _client.tool_names:
                    return await _client.call(tool_name, arguments)

        return f"[错误] 未知工具: {tool_name}"

    except Exception as e:
        logger.error(f"工具执行失败 [{tool_name}]: {e}")
        return f"[工具执行失败] {tool_name}: {e}"
