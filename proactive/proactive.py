"""
后台循环:
- 主动消息 (空闲检测 / 手动触发) - 走 tool-calling 流程
- 自主修改资料
- 自主发 QZone 动态

所有 LLM 调用统一走 bot.llm (LLMClient), JSON 解析统一走 llm.extract_json_decision
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from llm.llm import extract_json_decision
from llm.tools import TOOLS, TOOLS_README, execute_tool
from core.utils import BEIJING_TZ

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("proactive")


def _time_period(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "早上"
    if 12 <= h < 14:
        return "中午"
    if 14 <= h < 18:
        return "下午"
    if 18 <= h < 22:
        return "晚上"
    return "深夜"


class ProactiveManager:
    """三个后台循环合并管理"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot

    # ── 主动消息 ──────────────────────────────────
    async def trigger_manual(self, user_id: int):
        await self._do_proactive_message(user_id, reason="手动触发")

    async def _do_proactive_message(self, user_id: int, reason: str = "空闲检测"):
        """主动消息 - tool-calling 流程版

        直接走 LLM 决策 (相当于跳过消息处理管线, 直接到"第六步"):
        1. 构建 system prompt (人设 + 工具清单)
        2. LLM 决定: 调用工具获取信息? 还是直接生成消息? 还是跳过?
        3. 执行工具 → 结果回传 → LLM 最终决定内容
        4. 发送或跳过
        """
        bot = self.bot
        history = bot._get_history(user_id)
        user_str = str(user_id)
        note = bot.proactive_cache.get(user_str, {}).get("note", "")

        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        if bot.daily_proactive_date != today:
            bot.daily_proactive_count = {}
            bot.daily_proactive_date = today

        max_daily = bot.config.get("proactive", {}).get("max_daily_proactive_per_user", 2)
        if bot.daily_proactive_count.get(user_str, 0) >= max_daily:
            logger.info(f"[主动消息] {user_id} 今日已达上限({max_daily})")
            return

        persona = bot._get_persona()
        now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        time_period = _time_period(datetime.now(BEIJING_TZ))

        # 取最近几轮对话作为上下文
        context = ""
        if history:
            recent = history[-6:]  # 最近 6 条
            context_lines = []
            for msg in recent:
                role_label = "用户" if msg["role"] == "user" else "你"
                context_lines.append(f"[{role_label}] {msg['content'][:80]}")
            context = "\n".join(context_lines)

        # 构建 system prompt: 人设 + 场景 + 工具
        system_prompt = f"""{persona}

当前时间: {now_str} (星期{['一','二','三','四','五','六','日'][datetime.now(BEIJING_TZ).weekday()]}, {time_period})

你正在主动给用户 (QQ: {user_id}) 发消息。用户的备注: {note or '无'}
触发原因: {reason}

【最近对话上下文】
{context or '(这是第一次对话)'}

【主动消息规则】
- 你可以先调用工具获取信息 (时间、搜索、QZone动态等), 再决定说什么。
- 你也可以直接生成消息, 不调用任何工具。
- 如果你觉得现在不适合发消息, 输出 [SKIP] 跳过本次。
- 消息要自然、口语化, 1-3句话, 符合你当前人设。
- 不要提到"主动发消息"、"系统触发"等字眼。
- 可以做: 分享有趣的事、关心用户、聊天气/时间相关的话题、提到空间动态等等。
"""
        system_prompt += "\n\n" + TOOLS_README

        # Tool-calling 循环 (最多 3 轮)
        full_messages = [{"role": "system", "content": system_prompt}]
        full_messages.append({"role": "user", "content": reason})

        max_tool_rounds = 3
        for round_num in range(max_tool_rounds):
            result = await bot.llm.chat_with_tools(full_messages, TOOLS)
            if result is None:
                logger.warning("[主动-TOOL] API 异常")
                return

            # 无工具调用 → LLM 直接给出回复
            if not result.tool_calls:
                content = (result.content or "").strip()
                if not content or content.upper().startswith("[SKIP]"):
                    logger.info(f"[主动消息] LLM 决定跳过 ({reason})")
                    return
                # 发送
                sent = await bot._send_private_msg(user_id, content)
                if sent is None:
                    return
                bot._add_message(user_id, "assistant", content)
                bot.daily_proactive_count[user_str] = (
                    bot.daily_proactive_count.get(user_str, 0) + 1
                )
                logger.info(f"[主动-{reason}] -> {user_id}: {content[:50]}")
                return

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
                logger.info(f"[主动-TOOL] 第{round_num+1}轮: {tool_name}({arguments})")
                tool_result = await execute_tool(bot, tool_name, arguments, user_id)
                logger.info(f"[主动-TOOL] 结果: {tool_result[:100]!r}")
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

        logger.info(f"[主动消息] 超出工具调用轮数, 跳过发送")

    async def proactive_loop(self):
        """每隔 N 分钟检查好友空闲状态"""
        bot = self.bot
        while bot.running:
            await asyncio.sleep(
                60 * bot.config.get("proactive", {}).get("check_interval_minutes", 60)
            )
            if not bot.running:
                break
            try:
                # 用缓存, 不每次都读盘
                proactive_data = bot.proactive_cache
                idle_hours = bot.config.get("proactive", {}).get("idle_threshold_hours", 3)
                for uid_str, info in proactive_data.items():
                    if not info.get("enabled"):
                        continue
                    uid = int(uid_str)
                    history = bot._get_history(uid)
                    if not history:
                        continue
                    last_ts = history[-1].get("timestamp", "")
                    if not last_ts:
                        continue
                    last_dt = datetime.fromisoformat(last_ts)
                    delta = (datetime.now(BEIJING_TZ) - last_dt).total_seconds() / 3600
                    if delta >= idle_hours and history[-1]["role"] == "user":
                        logger.info(f"[主动-空闲] -> {uid}: 已{delta:.1f}小时未回复")
                        await self._do_proactive_message(uid, "空闲提醒")
            except Exception as e:
                logger.error(f"主动消息循环异常: {e}")

    # ── 自主修改资料 ────────────────────────────────
    async def auto_modify_profile_loop(self):
        bot = self.bot
        pcfg = bot.config.get("profile", {})

        while bot.running:
            interval = pcfg.get("auto_modify_interval_minutes", 60)
            await asyncio.sleep(60 * interval)
            if not bot.running:
                break
            if not pcfg.get("auto_modify_enabled", False):
                continue

            try:
                cooldown_hours = pcfg.get("auto_modify_cooldown_hours", 6)
                now = time.time()
                cooldown_seconds = cooldown_hours * 3600
                time_period = _time_period(datetime.now(BEIJING_TZ))

                persona = bot._get_persona()
                current_info = []
                if bot.current_nickname:
                    current_info.append(f"当前昵称: {bot.current_nickname}")
                if bot.current_signature:
                    current_info.append(f"当前签名: {bot.current_signature}")
                current_str = "\n".join(current_info) if current_info else "暂无记录"

                now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
                prompt = (
                    f"当前时间: {now_str} ({time_period})\n"
                    f"{current_str}\n\n你的性格人设:\n{persona}\n\n"
                    "请决定是否修改昵称或个性签名。只输出JSON。"
                )
                logger.info(f"[资料自动检查] 正在评估是否修改资料...")

                # system 走人设, user 走提示
                messages = [
                    {"role": "system", "content": pcfg.get("auto_modify_prompt", "")},
                    {"role": "user", "content": prompt},
                ]
                content = await bot.llm.chat(messages, temperature=0.8, max_tokens=100)
                logger.info(f"[资料自动检查] LLM返回: {content}")

                decision = extract_json_decision(content)
                if not decision:
                    continue

                action = decision.get("action", "none")
                value = decision.get("value", "")

                if action == "nickname":
                    if now - bot.last_nickname_change < cooldown_seconds:
                        logger.info(f"[资料自动检查] 昵称还在冷却中, 跳过")
                        continue
                    ok = await bot.profile_manager.set_profile(nickname=value)
                    if ok:
                        logger.info(f"[资料自动修改] 昵称 -> {value}")
                        await bot.profile_manager.notify_admin(
                            f"[资料自动变更] AI自主修改了昵称: \"{value}\""
                        )

                elif action == "signature":
                    if now - bot.last_signature_change < cooldown_seconds:
                        logger.info(f"[资料自动检查] 签名还在冷却中, 跳过")
                        continue
                    ok = await bot.profile_manager.set_profile(signature=value)
                    if ok:
                        logger.info(f"[资料自动修改] 签名 -> {value}")
                        await bot.profile_manager.notify_admin(
                            f"[资料自动变更] AI自主修改了签名: \"{value}\""
                        )

            except Exception as e:
                logger.error(f"自动修改资料异常: {e}")

    # ── 自主发 QZone 动态 ────────────────────────────
    async def auto_qzone_loop(self):
        bot = self.bot
        qcfg = bot.config.get("qzone", {})

        while bot.running:
            interval = qcfg.get("auto_publish_interval_minutes", 90)
            await asyncio.sleep(60 * interval)
            if not bot.running:
                break
            if not qcfg.get("auto_publish_enabled", False):
                continue

            try:
                cooldown_hours = qcfg.get("auto_publish_cooldown_hours", 3)
                now = time.time()
                cooldown_seconds = cooldown_hours * 3600

                if now - bot.last_qzone_publish < cooldown_seconds:
                    logger.info(f"[QZone自动] 还在冷却中, 跳过")
                    continue

                time_period = _time_period(datetime.now(BEIJING_TZ))
                persona = bot._get_persona()
                now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
                prompt = (
                    f"当前时间: {now_str} ({time_period})\n\n"
                    f"你的性格人设:\n{persona}\n\n"
                    "请决定是否发一条QQ空间动态。只输出JSON。"
                )
                logger.info(f"[QZone自动] 正在评估是否发动态...")

                messages = [
                    {"role": "system", "content": qcfg.get("auto_publish_prompt", "")},
                    {"role": "user", "content": prompt},
                ]
                content = await bot.llm.chat(messages, temperature=0.9, max_tokens=200)
                logger.info(f"[QZone自动] LLM返回: {content}")

                decision = extract_json_decision(content)
                if not decision:
                    continue

                action = decision.get("action", "none")
                if action == "publish" and decision.get("content"):
                    text = decision["content"].strip()
                    if len(text) > 200:
                        text = text[:200]
                    result = await bot._handle_qzone_publish(text)
                    logger.info(f"[QZone自动] 发布结果: {result}")
                    bot.last_qzone_publish = now
                else:
                    logger.info("[QZone自动] 决定不发布")

            except Exception as e:
                logger.error(f"自动发动态异常: {e}")
