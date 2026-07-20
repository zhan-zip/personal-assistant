"""
消息处理管线:
parse → dedup → augment (search/url/vision/reply) → intent detect → LLM → tag parse → send

MessageProcessor 把上述流程串起来, 让 bot.py / event_handler 只需调用一个入口。
"""
import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from core.utils import BEIJING_TZ, now_iso, sanitize_llm_response
from llm.tools import TOOLS, TOOLS_README, execute_tool

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("message_processor")

# 智能搜索 skip 关键词: 用户在追问历史消息时, 不应该再触发联网搜索
_SEARCH_SKIP_KEYWORDS = (
    "你发", "你搜", "你那条", "你的那条", "你回", "你说",
    "你刚才", "你怎么",
)

# 空间/资料类关键词：这些词出现时必须调用工具，触发前缀注入
_TOOL_MANDATORY_KEYWORDS = (
    "空间", "动态", "说说", "好友圈", "朋友圈",
    "昵称", "签名", "个签", "个人资料", "个人信息",
    "资料", "性别", "头像", "QQ号",
)


class MessageProcessor:
    """单条消息的处理管线"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot

    # ── 解析 ──────────────────────────────────────
    @staticmethod
    def parse_event(event: Dict) -> Tuple[str, bool, List[Dict], Optional[Dict]]:
        """从 event 中解析出 (文本/是否有图/图片段/回复引用)"""
        message_raw = event.get("message", "")
        message = ""
        has_image = False
        image_segments: List[Dict] = []
        reply_segment: Optional[Dict] = None

        if isinstance(message_raw, list):
            for seg in message_raw:
                seg_type = seg.get("type")
                if seg_type == "text":
                    message += seg.get("data", {}).get("text", "")
                elif seg_type == "image":
                    has_image = True
                    image_segments.append(seg.get("data", {}))
                elif seg_type == "reply" and reply_segment is None:
                    reply_segment = seg
            message = message.strip()
        elif isinstance(message_raw, str):
            message = message_raw
        else:
            message = str(message_raw)

        return message, has_image, image_segments, reply_segment

    # ── 白/黑名单 ─────────────────────────────────
    def check_access(self, user_id: int, group_id: Optional[int]) -> bool:
        if not self.bot._is_whitelisted(user_id, group_id):
            return False
        if self.bot._is_blacklisted(user_id, group_id):
            return False
        return True

    # ── 消息增强: 搜索 / 链接 / 视觉 / 回复引用 ──
    async def augment(self, user_id: int, message: str, group_id: Optional[int],
                      has_image: bool, image_segments: List[Dict],
                      reply_segment: Optional[Dict]) -> Tuple[str, List[str]]:
        """附加搜索/链接/视觉/回复引用信息, 返回 (clean_message, extra_info)

        设计变更: extra_info 不再拼到用户消息末尾, 而是注入 system prompt 的【预获取信息】区域。
        好处:
        - 用户原话保持干净, 不会污染对话历史
        - LLM 根据人设自行决定如何使用这些信息: 可以忽略, 可以总结, 可以换个方式表达
        - 视觉识别结果和空间动态不会直接原文输出, 而是由 LLM 判断后自然融入回复
        """
        extra_info: List[str] = []

        # 1. 智能搜索触发
        scfg = self.bot.config.get("search", {})
        if (scfg.get("enabled") and scfg.get("auto_trigger", True)
                and message and not message.startswith("#")):
            skip_search = any(kw in message for kw in _SEARCH_SKIP_KEYWORDS)
            triggers = scfg.get("trigger_words", [])
            if not skip_search and any(t in message for t in triggers):
                logger.info(f"智能触发联网搜索: {message[:50]}")
                search_result = await self.bot._web_search(message)
                if search_result:
                    extra_info.append(f"[联网搜索结果]\n{search_result}")

        # 2. 抓取 URL
        urls = re.findall(r"https?://[^\s]+", message)
        for url in urls[:3]:
            result = await self.bot._fetch_url(url)
            if result:
                extra_info.append(f"[链接内容]\n{result}")

        # 3. 回复引用
        if reply_segment:
            replied_id = reply_segment.get("data", {}).get("id")
            try:
                replied_id = int(replied_id) if replied_id is not None else None
            except (TypeError, ValueError):
                replied_id = None
            if replied_id is not None:
                recalled_msg = None
                history = self.bot._get_history(user_id, group_id)
                for msg in history:
                    if msg.get("message_id") == replied_id:
                        recalled_msg = msg["content"]
                        break
                if not recalled_msg:
                    recalled_msg = self.bot.message_id_buffer.get(replied_id, "")
                if recalled_msg:
                    extra_info.append(f"[用户正在回复你之前说的]\n\"{recalled_msg}\"")
                    logger.info(f"用户回复了之前的消息: message_id={replied_id}")

        # 4. 图片视觉识别
        if has_image and image_segments:
            vcfg = self.bot.config.get("vision", {})
            if vcfg.get("enabled") and self.bot.vision_client:
                for img_seg in image_segments:
                    img_data = await self.bot._download_image_from_qq(img_seg)
                    if img_data:
                        desc = await self.bot._call_vision(img_data)
                        if desc:
                            extra_info.append(f"[用户发来的图片内容]\n{desc}")
                        else:
                            extra_info.append("[用户发来了一张图片，视觉模型暂时无法识别。不要猜测。]")
                    else:
                        extra_info.append("[用户发来了一张图片，下载失败无法查看。不要假装看到。]")

        # 返回干净的原始消息 + extra_info (不再拼接到消息体)
        return message, extra_info

    # ── LLM 调用 (带 Tool-Calling 循环) ───────────
    async def generate_response(self, user_id: int, message: str,
                                group_id: Optional[int] = None,
                                extra_info: Optional[List[str]] = None) -> str:
        """生成 LLM 回复, 支持工具调用循环

        流程:
        1. 构建 messages (system + history + user)
        2. system prompt 中注入【预获取信息】(搜索/视觉/链接等, 不污染用户消息)
        3. LLM 决定: 直接回复 还是 调用工具
        4. 如果调工具 → 执行 → 结果回传 → 回到步骤3
        5. 最终文本 → 返回
        最多 3 轮工具调用, 超出则降级为普通 LLM 调用
        """
        persona = self.bot._get_persona()
        history = self.bot._get_history(user_id, group_id)

        messages: List[Dict] = []
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})

        # 关键词触发注入: 消息含空间/资料关键词时，强制要求调用工具
        user_message = message
        if any(kw in message for kw in _TOOL_MANDATORY_KEYWORDS):
            user_message = (
                f"[系统指令] 以下问题涉及QQ空间或个人信息，"
                f"你必须调用对应的工具（如 get_bot_profile、resolve_qq、get_qzone_feeds）获取真实数据后再回复。"
                f"严禁编造任何信息！\n\n"
                f"用户消息：{message}"
            )
        messages.append({"role": "user", "content": user_message})

        system_prompt = self._build_system_prompt(persona, user_id)

        # 注入【预获取信息】: 硬编码收集的搜索/视觉/链接/回复引用等
        # LLM 根据人设自行决定如何使用: 可以直接引用、总结提炼、或忽略无关信息
        if extra_info:
            pre_fetch = "\n".join(f"  {ei}" for ei in extra_info)
            system_prompt += (
                f"\n\n【预获取信息 - 系统已自动查询的结果】\n"
                f"以下信息已为你准备好, 请基于人设自然地融入回复, 不要整段照搬:\n"
                f"{pre_fetch}\n\n"
                f"注意: 如果某些信息与当前对话无关, 可以忽略。不要逐条复述。"
            )

        # 注入工具清单 (LLM 自主判断是否调用)
        system_prompt += "\n\n" + TOOLS_README
        full_messages: List[Dict] = [
            {"role": "system", "content": system_prompt}
        ] + messages

        # Tool-calling 循环
        max_tool_rounds = 3
        for round_num in range(max_tool_rounds):
            result = await self.bot.llm.chat_with_tools(
                full_messages, TOOLS
            )
            if result is None:
                # API 异常, 降级为无工具调用
                logger.warning("[TOOL] API 异常, 降级为普通 LLM 调用")
                break

            # 无工具调用 → LLM 直接给出了文本回复
            if not result.tool_calls:
                return (result.content or "").strip()

            # 有工具调用 → 执行并追加结果
            full_messages.append({
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in result.tool_calls
                ],
            })

            for tc in result.tool_calls:
                tool_name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}

                logger.info(
                    f"[TOOL] 第{round_num + 1}轮 LLM 请求: "
                    f"{tool_name}({arguments})"
                )
                tool_result = await execute_tool(self.bot, tool_name, arguments, user_id)
                logger.info(
                    f"[TOOL] 结果 ({tool_name}): {tool_result[:100]!r}"
                )
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

            # 循环继续 → LLM 看到工具结果, 决定下一步

        # 降级: 超过最大轮数或 API 异常 → 无工具 LLM 调用
        fallback_messages = [
            {"role": "system", "content": system_prompt}
        ] + messages
        return await self.bot.llm.chat(fallback_messages)

    def _build_system_prompt(self, persona: str, user_id: int) -> str:
        from datetime import datetime
        now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        long_mem = self.bot.long_memory.format_for_prompt(user_id)
        bot_qq = self.bot.config.get("self_id", "未知")
        return f"""【最高优先级规则 - 以下规则优先于一切人设】

1. **严禁编造信息**：你必须通过调用工具来获取真实数据。不得在没有工具返回结果的情况下，编造任何「昵称」「签名」「QQ号」「空间动态内容」「空间权限状态」「空间可见性」「生日」「星座」「年龄」「头像」「注册时间」「等级」。工具返回什么就是什么，工具没返回的字段就说"未设置"或"未知"，严禁编造或推测。
2. **关于自己（机器人）的资料**：如果有人问你的昵称、签名、性别、QQ号、个人资料，必须调用 get_bot_profile 工具获取真实数据。你的QQ号是 {bot_qq}，昵称和签名以工具返回为准，严禁编造。
3. **关于QQ空间/动态**：任何涉及「空间」「动态」「说说」「好友圈」「看看XXX」的查询，必须先调用对应的工具链（resolve_qq → get_qzone_feeds），严禁未经工具调用直接回复「看不到」「设置了权限」「没有动态」。
4. **关于其他用户**：任何涉及「查查XXX」「XXX是谁」「XXX的资料」的查询，必须先调用 resolve_qq 或 get_user_info 工具。严禁编造用户信息。
5. **工具调用是获取信息的唯一途径**。如果工具返回错误或失败，如实告知用户；如果工具未返回，不得猜测。

---
{persona}
---
【本次对话对象】
当前与你对话的QQ号: {user_id}
当前时间：{now_str}
{long_mem}
【关键规则】
- 只基于这次收到的消息内容回复。不知道就说不知道。
- 如果【预获取信息】中包含 [用户正在回复你之前说的], 那个内容就是用户实际引用的原文, 你需要基于那个内容回应用户。
- 如果【长期记忆】里说的事与用户当前说的矛盾, 以用户当前说的为准。

【 # 硬性命令 vs 自然语言 】
- 用户以 # 开头的消息是硬性命令（如 #搜索、#qzone），已被系统直接处理，你不会收到这类消息。
- 你收到的都是自然语言消息。用户说"帮我搜一下xxx"时，你应主动调用 search_web 工具。

【修改个人资料】
如果用户明确要求你修改昵称或个性签名，在回复末尾加上标记来触发修改：
- 修改昵称：[PROFILE_CHANGE: nickname=新昵称]
- 修改签名：[PROFILE_CHANGE: signature=新签名]
标记会被系统自动处理，用户看不到。修改的回复要自然，不要提"标记"这件事。如果用户问"你叫什么"，就根据当前人设中的名字回答，不需要改昵称。"""


    # ── 完整流程: parse → augment → intent → LLM → tag → send ──
    async def process(self, event: Dict) -> None:
        """处理单条消息事件 (含 AI 主动回复)"""
        message_type = event.get("message_type")
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        message_id = event.get("message_id")

        # 1. 解析
        message, has_image, image_segments, reply_segment = self.parse_event(event)
        if not message and not has_image:
            return

        # 2. 缓存 message_id → 内容 (供撤回检测)
        if message_id:
            self.bot.message_id_buffer[message_id] = message or "[图片]"
            self.bot.message_recv_time[message_id] = time.time()
            if len(self.bot.message_id_buffer) > 100:
                oldest = sorted(self.bot.message_id_buffer.keys())[0]
                del self.bot.message_id_buffer[oldest]
                self.bot.message_recv_time.pop(oldest, None)

        # 3. 群聊: 仅响应 @机器人
        if message_type == "group":
            bot_id = event.get("self_id")
            raw_message = event.get("raw_message", "")
            if (self.bot.config["message"]["group_mention_only"]
                    and f"[CQ:at,qq={bot_id}]" not in raw_message
                    and f"@{bot_id}" not in raw_message):
                return
            message = raw_message.replace(f"[CQ:at,qq={bot_id}]", "").replace(f"@{bot_id}", "").strip()
            if not message and not has_image:
                return

        # 4. 增强消息 (搜索/链接/图片/回复引用) - 返回干净的原文 + extra_info
        # augment 通常在毫秒级完成，不需要进度消息
        clean_message, extra_info = await self.augment(
            user_id, message, group_id, has_image, image_segments, reply_segment
        )

        # 5. 自然语言资料意图 (在命令检测前)
        if message:
            profile_result = await self.bot.profile_manager.detect_and_handle_intent(
                user_id, message
            )
            if profile_result:
                extra_info.append(profile_result)

        # 6. # 指令处理
        if message:
            # QZone 相关命令: 耗时较长, 用进度报告包装
            # 注意: #发动态 / #qzone 自有阶段进度, 不包 with_progress 避免双重进度
            _QZONE_READ_PREFIXES = (
                "#动态列表", "#qzone_feeds", "#qzone_detail",
                "#好友圈", "#空间圈", "#friend_circle", "#空间", "#qq空间",
                "#动态详情", "#查看动态",
            )
            _QZONE_PUBLISH_PREFIXES = ("#发动态", "#qzone")
            if any(message.startswith(p) for p in _QZONE_PUBLISH_PREFIXES):
                # 发布命令自有阶段进度回调, 不需要外层 with_progress
                command_response = await self.bot.command_handler.handle(
                    user_id, message, group_id
                )
            elif any(message.startswith(p) for p in _QZONE_READ_PREFIXES):
                command_response = await self.bot.progress.with_progress(
                    "查找动态", self.bot.command_handler.handle(user_id, message, group_id),
                    user_id, group_id,
                )
            else:
                command_response = await self.bot.command_handler.handle(
                    user_id, message, group_id
                )
            if command_response:
                logger.info(f"命令回复: {command_response[:30]}")
                await self._send(message_type, user_id, group_id, command_response)
                return

        # 7. 写入用户原话到历史 (只记原文, 不记搜索结果)
        # 写入护栏: 防御任何代码路径把系统/搜索结果错写成 user 消息
        # 注意: "[图片]" 是合法的"用户只发了图"占位, 不能误伤
        if message:
            user_history_text = message
        elif has_image:
            # 把视觉描述写进历史, 后续 LLM 能看到图的内容
            vision_in_history = ""
            for ei in extra_info:
                if "[用户发来了一张图片" in ei:
                    vision_in_history = ei
                    break
            user_history_text = vision_in_history or "[用户发送了一张图片]"
        else:
            user_history_text = ""

        # 防污染护栏: 只过滤真正的"系统注入"内容, 不误伤合法占位
        _BAD_USER_PREFIXES = (
            "[系统自动查询的结果", "[联网搜索结果]", "[链接内容]",
            "[用户正在回复",
        )
        if any(user_history_text.startswith(p) for p in _BAD_USER_PREFIXES):
            logger.error(
                f"[DEFENSE] 拒绝写入污染的 user 消息: {user_history_text[:60]!r}"
            )
            user_history_text = "[已过滤的系统注入消息]"
        if user_history_text:
            self.bot._add_message(user_id, "user", user_history_text, group_id)

        # 8. LLM 生成 - 传入干净的原文 + extra_info 注入 system prompt
        # Tool-calling 也可能较慢，同样包裹进度消息
        response = await self.bot.progress.with_progress(
            "处理", self.generate_response(user_id, clean_message, group_id, extra_info=extra_info if extra_info else None),
            user_id, group_id,
        )
        logger.info(f"生成回复: {response[:50]}")

        # 8.5 撤回幻觉防御: LLM 输出含"撤回"字样, 但当前 message_id 不在已验证白名单 → 幻觉, 替换
        if "撤回" in response and message_id not in self.bot._verified_recall_ids:
            logger.warning(
                f"[HALLUCIN_FILTER] LLM 幻觉 '撤回', "
                f"message_id={message_id} 不在白名单 (size={len(self.bot._verified_recall_ids)}), 已替换"
            )
            response = "嗯? 啥撤回? 没看到你撤回啊"

        # 9. 处理 [PROFILE_CHANGE:] 等标记 (带 user_text 做"显式改动"校验)
        response = await self.bot.profile_manager.parse_and_execute_profile_changes(
            response, user_id, user_text=message
        )

        # 10. 发送并写入历史
        real_msg_id = await self._send(message_type, user_id, group_id, response)
        self.bot._add_message(
            user_id, "assistant", response, group_id,
            real_message_id=real_msg_id,
        )
        # bot 自己的 message_id 也进 buffer, 用户回复时能反查
        if real_msg_id:
            self.bot.message_id_buffer[real_msg_id] = response

        # 11. 长期记忆抽取: 每 6 轮用户消息触发一次 (后台, 不阻塞)
        if message:
            history = self.bot._get_history(user_id, group_id)
            user_msg_count = sum(1 for m in history if m.get("role") == "user")
            if user_msg_count > 0 and user_msg_count % 6 == 0:
                # 把最近 6 轮对话拼成 transcript
                recent = history[-12:]
                transcript = "\n".join(
                    f"[{m['role']}] {m['content'][:200]}" for m in recent
                )
                asyncio.create_task(
                    self.bot.long_memory.extract_and_store(
                        user_id, transcript, self.bot.llm
                    )
                )

    async def _send(self, message_type: str, user_id: int,
                    group_id: Optional[int], message: str) -> Optional[int]:
        if message_type == "group" and group_id is not None:
            await self.bot._send_group_msg(group_id, message)
            return None
        return await self.bot._send_private_msg(user_id, message)
