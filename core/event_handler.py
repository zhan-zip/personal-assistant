"""
事件路由 / 接收层诊断 / 撤回检测

EventHandler 负责:
1. 接收 NapCat 推送的 event, 分发到 message / notice / meta_event
2. 接收层诊断 + 计数 (排查 AI 是否真的被推了两次)
3. 撤回检测 (单次 get_msg 反查)
4. 消息去重 (message_id + 内容哈希)
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Dict, Optional, Set

if TYPE_CHECKING:
    from bot import QQBot
    from protocols.base import Message

logger = logging.getLogger("event_handler")

# 去重参数
DEDUP_WINDOW_SECONDS = 3.0
DEDUP_HASH_MAX_SIZE = 200
PROCESSED_MSG_ID_MAX_SIZE = 500


class EventHandler:
    """事件路由 + 去重 + 撤回检测 + 接收层统计"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot
        # 严格按 message_id 去重
        self._processed_message_ids: Set[str] = set()
        # 内容+用户哈希 → 时间戳
        self._recent_msg_hashes: Dict[str, float] = {}

        # ── 接收层统计 (排查"用户消息被推两次") ──
        # message_id → 出现次数
        self._seen_message_ids: Dict[int, int] = {}
        # (user_id, content_hash) → 出现次数 (跨 message_id, 抓"同内容不同 message_id")
        self._seen_content: Dict[str, int] = {}
        # 累计接收 message 事件数
        self.total_received: int = 0
        # 累计过滤数 (按原因分类)
        self.filtered_by_dedup_id: int = 0
        self.filtered_by_dedup_content: int = 0
        self.filtered_by_other: int = 0

    # ── 消息去重 (双层: message_id + 内容哈希) ─────
    def is_duplicate(self, user_id, message: str,
                     message_id: Optional[str]) -> bool:
        """返回 True 表示消息是重复的, 应被丢弃

        第一层: message_id (同一 message_id 绝对只处理一次)
        第二层: (user_id, content) 在 DEDUP_WINDOW_SECONDS 内出现两次
        """
        # 第一层
        if message_id and message_id in self._processed_message_ids:
            self.filtered_by_dedup_id += 1
            logger.warning(
                f"[DUP] 重复消息已跳过 message_id={message_id} msg={message[:50]}"
            )
            return True
        if message_id:
            self._processed_message_ids.add(message_id)
            if len(self._processed_message_ids) > PROCESSED_MSG_ID_MAX_SIZE:
                keep = sorted(self._processed_message_ids)[
                    -PROCESSED_MSG_ID_MAX_SIZE // 2:
                ]
                self._processed_message_ids = set(keep)

        # 第二层
        if message:
            content_hash = hashlib.md5(
                f"{user_id}_{message}".encode()
            ).hexdigest()
            now_ts = time.time()
            last_ts = self._recent_msg_hashes.get(content_hash, 0)
            if now_ts - last_ts < DEDUP_WINDOW_SECONDS:
                self.filtered_by_dedup_content += 1
                logger.warning(
                    f"[DUP_CONTENT] 内容重复已跳过 user={user_id} "
                    f"hash={content_hash[:8]} msg={message[:50]}"
                )
                return True
            self._recent_msg_hashes[content_hash] = now_ts
            if len(self._recent_msg_hashes) > DEDUP_HASH_MAX_SIZE:
                cutoff = now_ts - DEDUP_WINDOW_SECONDS * 3
                self._recent_msg_hashes = {
                    k: v for k, v in self._recent_msg_hashes.items() if v > cutoff
                }
        return False

    # ── 接收层诊断日志 (用于排查"AI 收到两遍") ──────
    def _record_event_for_diag(self, event: Dict) -> None:
        """对每条 message event:
        1. 计入 _seen_message_ids / _seen_content (用于事后看是不是被推多次)
        2. 如果 message_id 出现 >= 2 次 → [DIAG_DOUBLE] 强提示
        """
        post_type = event.get("post_type")
        if post_type != "message":
            return
        self.total_received += 1

        mid = event.get("message_id")
        uid = event.get("user_id", "?")
        raw = event.get("raw_message", "")
        ch = hashlib.md5(f"{uid}_{raw}".encode()).hexdigest()[:8]
        logger.info(
            f"[RECV #{self.total_received}] post_type={post_type} "
            f"mid={mid} uid={uid} content_hash={ch} raw={raw[:30]!r}"
        )

        if mid is not None:
            self._seen_message_ids[mid] = self._seen_message_ids.get(mid, 0) + 1
            if self._seen_message_ids[mid] > 1:
                logger.error(
                    f"[DIAG_DOUBLE] message_id={mid} 已被 NapCat 推了 "
                    f"{self._seen_message_ids[mid]} 次! user={uid} raw={raw[:30]!r}"
                )
        self._seen_content[ch] = self._seen_content.get(ch, 0) + 1

    # ── 撤回检测 ──────────────────────────────────
    async def check_recall_via_get_msg(self, msg_id, user_id):
        """6秒后用 get_msg 反查消息是否被撤回, 是对 notice 路径的兜底

        关键: get_msg 拿不到 raw_message 可能是 "撤回" 也可能是 "NapCat 缓存过期/GC"
        所以必须**双重证据**才判定撤回:
          1) get_msg raw_message 为空
          2) message_id_buffer 里还有这条消息的原文 (说明之前收到过)
        """
        await asyncio.sleep(6)
        msg_key = str(msg_id)
        # 第一道证据: get_msg 拿到 raw_message 没?
        data = await self.bot._send_ws_request(
            "get_msg", {"message_id": int(msg_id)}, timeout=5
        )
        raw = ""
        if data and isinstance(data, dict):
            raw = data.get("raw_message") or data.get("message") or ""
            if isinstance(raw, list):
                # message 字段有时是 segment 数组, 拼成字符串
                raw = "".join(
                    str(s.get("data", {}).get("text", "")) if isinstance(s, dict) else str(s)
                    for s in raw
                )
        if raw:
            # 消息还在, 不算撤回
            if msg_key in self.bot.message_id_buffer:
                self.bot.message_id_buffer[msg_key] = raw
            return

        # 第二道证据: message_id_buffer 里得有原文, 否则可能是:
        #   (a) buffer 已被 LRU 淘汰 (超过 100 条后)
        #   (b) bot 启动后第一次收到消息前用户已撤回
        # 这两种情况都不响应, 但记日志方便排查
        original = self.bot.message_id_buffer.get(msg_key, "")
        if not original or original.startswith("("):
            logger.info(
                f"[get_msg反查] msg_id={msg_key} get_msg 无内容, "
                f"buffer 也无原文 (size={len(self.bot.message_id_buffer)}), 跳过"
            )
            return

        # 双重证据齐全 → 真正撤回
        logger.info(
            f"[get_msg反查] 消息 {msg_key} 已撤回, "
            f"buffer原内容: {original[:30]!r}"
        )
        # 用过即丢, 避免重复响应
        self.bot.message_id_buffer.pop(msg_key, None)
        await self.handle_recall(user_id, msg_key, original)

    async def handle_recall_notice(self, event: Dict):
        """处理 friend_recall / group_recall 通知事件

        这是撤回检测的主路径, 走 NapCat notice 事件.
        """
        notice_type = event.get("notice_type", "")
        user_id = event.get("user_id")
        msg_id = event.get("message_id")
        if not msg_id or not user_id:
            return

        # 群聊撤回时, user_id 是撤回者, 群成员里不一定有我们
        # 群聊不自动响应撤回 (群消息撤回太常见, 容易被刷)
        is_group = event.get("group_id") or event.get("sub_type") == "group"
        if is_group:
            logger.info(
                f"[NOTICE] 群聊撤回 (msg_id={msg_id}), 不主动响应"
            )
            return

        original = self.bot.message_id_buffer.pop(str(msg_id), "(撤回太快, 没记录到)")
        logger.info(
            f"[NOTICE] {notice_type} user={user_id} msg_id={msg_id} "
            f"原内容: {original[:30]!r}"
        )
        await self.handle_recall(user_id, str(msg_id), original)

    async def handle_recall(self, user_id: int, msg_id: int, content: str,
                            bypass_cooldown: bool = False):
        # 撤回是基础功能, 不依赖主动消息开关
        recall_cfg = self.bot.config.get("recall", {})
        require_enabled = recall_cfg.get("require_proactive_enabled", False)
        cooldown = recall_cfg.get("cooldown_seconds", 60)

        # 关键: 把这个 msg_id 标记为"已验证撤回", 防 LLM 幻觉后续说"你撤回了xxx"
        self.bot._verified_recall_ids.add(msg_id)

        proactive_data = self.bot.proactive_cache
        if require_enabled and not proactive_data.get(str(user_id), {}).get("enabled"):
            logger.info(f"撤回用户 {user_id} 未开启主动消息，不反应")
            return

        # cooldown: 只对 6 秒反查生效, 撤回扫描发现的(历史撤回)不设 cooldown
        # 因为扫描 loop 自己会检查 _verified_recall_ids 防重复
        if not bypass_cooldown:
            now = time.time()
            last = self.bot.recall_cooldown.get(str(user_id), 0)
            if now - last < cooldown:
                logger.info(f"撤回用户 {user_id} cooldown中，跳过")
                return
            self.bot.recall_cooldown[str(user_id)] = now

        # 优先用传入的 content (反查 / 通知路径已经取到)
        if content and not content.startswith("("):
            recalled_text = content
        else:
            recalled_text = self.bot.message_id_buffer.get(msg_id, "")
        if not recalled_text:
            recalled_text = "(已经看不到撤回的内容了)"

        # 调用 LLM 决策: 是否要主动发消息询问, 如果发, 写什么
        # 关键约束: 撤回原文未知时绝对不能瞎说, 必须让 LLM 输出 [SKIP_RECALL] 或
        # 仅表达"没注意到"等无具体内容反应
        unknown = (
            "已经看不到" in recalled_text
            or not recalled_text.strip()
            or recalled_text.startswith("(")
        )

        logger.info(
            f"[RECALL] user={user_id} msg_id={msg_id} "
            f"已知原文={not unknown}: {recalled_text[:40]!r}"
        )

        if unknown:
            # 原文未知: LLM 不能编造, 但仍可自由选择回应与否
            recall_info = (
                "用户刚撤回了消息, 但系统没记录到原文 (NapCat 缓存过期/消息GC)。\n"
                "你不知道用户撤回了什么具体内容。\n"
                "\n"
                "你可以:\n"
                "- 不回应 (输出 [SKIP_RECALL])\n"
                "- 简单回应, 比如 \"嗯?撤回了啥?\" \"诶?\" 等\n"
                "\n"
                "绝对不能编造/猜测撤回的具体内容。"
            )
        else:
            # 原文已知: LLM 根据人设 + 上下文 + 撤回内容, 自由决定如何回应
            recall_info = (
                f'用户刚才撤回了一条消息。\n'
                f'撤回的消息内容是: "{recalled_text[:200]}"\n'
                f'撤回时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n'
                f'\n'
                f'请结合你的人设、当前对话上下文、以及撤回的内容, 自由决定:\n'
                f'1. 是否主动发消息给用户\n'
                f'2. 如果要发, 说什么\n'
                f'\n'
                f'参考方向 (不限于此, 由你的人设主宰):\n'
                f'- 配合演戏: "嗯? 你刚才说啥了? 我没注意~"\n'
                f'- 戳穿调侃: "我看到了哦, 撤回也没用~"\n'
                f'- 好奇追问: "你撤回了什么? 快说快说"\n'
                f'- 温柔包容: "没关系的, 不想说就不说"\n'
                f'- 完全无视: 输出 [SKIP_RECALL] 假装什么都没发生\n'
                f'\n'
                f'决定: 如果回应, 直接输出回复文本 (不要带前缀标记)。\n'
                f'如果不想回应, 输出 [SKIP_RECALL]。\n'
                f'回应要自然, 符合人设, 不要说「作为AI」。'
            )

        prompt = recall_info

        reaction = None
        try:
            # 带对话上下文: 撤回回应要像正常聊天一样自然
            # 不能用 chat_with_persona (无历史), 需要用 chat 把历史拼进去
            persona = self.bot._get_persona()
            history = self.bot._get_history(user_id)
            from core.message_processor import MessageProcessor
            mp = MessageProcessor(self.bot)
            system_prompt = mp._build_system_prompt(persona, user_id)
            # 在 system prompt 末尾追加撤回事件
            system_prompt += (
                f"\n\n"
                f"─── 撤回事件通知 ───\n"
                f"{prompt}\n"
                f"以上是通知, 用户看不到这段。你需要决定是否主动发消息。"
            )
            messages = [{"role": "system", "content": system_prompt}]
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": "(撤回事件, 请决策)"})

            reaction = await self.bot.llm.chat(
                messages, temperature=0.7, max_tokens=200,
            )
        except Exception as e:
            logger.error(f"[RECALL] LLM 决策失败: {e}")
            reaction = None

        # 解析 LLM 决策
        if not reaction:
            logger.info(f"[RECALL] LLM 决策为空, 跳过")
            return
        reaction_stripped = reaction.strip()
        # LLM 决定不回应
        if reaction_stripped.upper().startswith("[SKIP_RECALL]") or "SKIP_RECALL" in reaction_stripped.upper():
            logger.info(f"[RECALL] LLM 决定跳过回应: {reaction_stripped[:40]!r}")
            # 仍写入历史: 撤回事件作为 "system" 消息, 让后续对话知道这件事
            self._write_recall_history(user_id, msg_id, recalled_text, unknown)
            return
        # LLM 输出有效回应
        logger.info(f"[RECALL] LLM 决定回应: {reaction_stripped[:40]!r}")

        try:
            logger.info(f"撤回反应 -> user={user_id}: {reaction[:50]}")
            # 先写撤回事件到历史, 再写 bot 回应 — 保持时间顺序
            self._write_recall_history(user_id, msg_id, recalled_text, unknown)
            await self.bot._send_private_msg(user_id, reaction)
            self.bot._add_message(user_id, "assistant", reaction)
        except Exception as e:
            logger.error(f"撤回反应生成失败: {e}")

    def _write_recall_history(self, user_id: int, msg_id: int,
                               recalled_text: str, unknown: bool):
        """把撤回事件写入对话历史 (system 角色), 后续聊天 LLM 能看到

        不修改原始用户消息 —— LLM 看到原文 + 撤回事件后,
        根据人设自行决定配合演戏还是戳穿.
        """
        ts = time.strftime("%H:%M:%S")
        if unknown:
            history_msg = (
                f"[撤回事件 {ts}] 用户撤回了一条消息。"
                f"系统未能记录到原文, 你不知道具体内容。"
            )
        else:
            history_msg = (
                f"[撤回事件 {ts}] 用户撤回了一条消息。"
                f"原消息内容: \"{recalled_text[:100]}\"。"
                f"用户可能想装作没说过 —— "
                f"你可以配合演戏, 也可以戳穿, 取决于你的人设。"
            )
        self.bot._add_message(user_id, "system", history_msg)
        logger.info(f"[RECALL] 撤回事件已写入历史: user={user_id}")

    # ── 接收层统计输出 ─────────────────────────────
    def get_recv_stats(self) -> Dict:
        """返回接收层统计, 可用于 #状态 / debug"""
        double_ids = {mid: cnt for mid, cnt in self._seen_message_ids.items() if cnt > 1}
        double_contents = {ch: cnt for ch, cnt in self._seen_content.items() if cnt > 1}
        return {
            "total_received": self.total_received,
            "filtered": {
                "by_dedup_id": self.filtered_by_dedup_id,
                "by_dedup_content": self.filtered_by_dedup_content,
                "by_other": self.filtered_by_other,
            },
            "double_pushed_message_ids": double_ids,
            "double_pushed_content_hashes": double_contents,
        }

    # ── 主入口: 处理聊天消息 (统一 Message) ───────
    async def handle_message(self, message: "Message"):
        """收到一条统一 Message（QQ/网页等任何协议）"""
        user_id = message.user_id
        group_id = message.group_id
        message_id = message.message_id
        text = message.text

        # 保底: 忽略自己发的
        if (self.bot.config["message"].get("ignore_self", True)
                and str(user_id) == str(self.bot.config.get("self_id"))):
            self.filtered_by_other += 1
            return

        # 白/黑名单
        if not self.bot._is_whitelisted(user_id, group_id):
            self.filtered_by_other += 1
            return
        if self.bot._is_blacklisted(user_id, group_id):
            self.filtered_by_other += 1
            return

        if not text and not message.images:
            self.filtered_by_other += 1
            return

        # 去重
        if self.is_duplicate(user_id, text or message.raw.get("raw_message", ""), message_id):
            return

        # 注册新好友 (仅 OneBot 私聊; 网页无"好友"概念)
        if message.channel == "onebot" and not group_id:
            try:
                self.bot._register_proactive_user(int(user_id), group_id)
            except (ValueError, TypeError):
                pass

        # 记录 self_id (OneBot)
        sid = message.raw.get("self_id")
        if sid and not self.bot.config.get("self_id"):
            self.bot.config["self_id"] = sid
            logger.info(f"已记录 self_id: {sid}")

        # 私聊撤回兜底检测 (仅 OneBot 私聊)
        if message.channel == "onebot" and message_id and not group_id:
            try:
                asyncio.create_task(
                    self.check_recall_via_get_msg(int(message_id), int(user_id))
                )
            except (ValueError, TypeError):
                pass

        # 投递到主流程
        asyncio.create_task(self.bot.message_processor.process(message))

    # ── 主入口: 处理非消息事件 (notice/meta) ───────
    async def handle_event(self, event: Dict):
        """处理非消息事件（撤回通知、心跳等）。消息事件已在 Adapter 层转成 Message。"""
        post_type = event.get("post_type")

        # 自己发的消息直接忽略 (保底)
        if post_type == "message_sent":
            return

        if post_type == "message":
            # 理论不会走到这里 (Adapter 已转 Message); 保底走统一处理
            return

        if post_type == "meta_event":
            meta_event_type = event.get("meta_event_type")
            if meta_event_type == "lifecycle":
                logger.info(f"生命周期事件: {event.get('sub_type')}")
        elif post_type == "notice":
            notice_type = event.get("notice_type", "")
            # 调试: 把所有 notice event 完整打印, 排查撤回事件的 notice_type
            logger.info(
                f"[NOTICE_RAW] notice_type={notice_type} sub_type={event.get('sub_type', '')} "
                f"group_id={event.get('group_id', '')} user_id={event.get('user_id', '')} "
                f"message_id={event.get('message_id', '')} "
                f"operator_id={event.get('operator_id', '')} "
                f"raw={json.dumps(event, ensure_ascii=False)[:300]}"
            )
            if notice_type in ("friend_recall", "group_recall"):
                logger.info(
                    f"[NOTICE] {notice_type}: "
                    f"{json.dumps(event, ensure_ascii=False)[:300]}"
                )
                # 立即响应撤回通知, 不等 6 秒反查
                asyncio.create_task(self.handle_recall_notice(event))
        else:
            logger.info(
                f"[UNKNOWN] post_type={post_type} 完整事件: "
                f"{json.dumps(event, ensure_ascii=False)[:300]}"
            )
