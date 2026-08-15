"""
OneBot v11 协议适配器（NapCat 桥接）

把原来散落在 bot.py 里的 WebSocket 连接 / 请求-响应 / 收发 / 节流逻辑收口到这里。
核心（bot / event_handler / message_processor）不再直接接触 websockets。
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

import websockets

from protocols.base import BaseAdapter, Message

logger = logging.getLogger("protocols.onebot")

# 发送节流：同一用户连续发相同内容至少间隔 N 秒
SEND_THROTTLE_SECONDS = 1.5


class OneBotAdapter(BaseAdapter):
    """NapCat（OneBot v11）协议适配器"""

    channel = "onebot"

    def __init__(self, bot: Any, ws_url: str,
                 access_token: str = "", timeout: float = 30):
        super().__init__(bot)
        self.ws_url = ws_url
        self.access_token = access_token
        self.timeout = timeout

        self.websocket = None
        # 请求-响应映射: echo 编号 → Future
        self.pending_futures: Dict[int, asyncio.Future] = {}
        self.message_id_counter: int = 0
        # 发送节流: (user_id, content_hash) → 最后发送时间
        self._last_send_time: Dict[tuple, float] = {}

    # ── 连接状态 ──
    @property
    def connected(self) -> bool:
        return self.websocket is not None

    def get_self_id(self) -> Optional[str]:
        return self.bot.config.get("self_id")

    def reset_state(self):
        """重连前清空依赖旧连接的临时状态"""
        self.pending_futures.clear()
        self._last_send_time.clear()

    # ── 生命周期 ──
    async def close(self) -> None:
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self.websocket = None

    async def connect(self, on_event=None, on_message=None, on_ready=None) -> bool:
        self._event_callback = on_event
        self._message_callback = on_message
        async with websockets.connect(
            self.ws_url,
            extra_headers={"Authorization": f"Bearer {self.access_token}"} if self.access_token else None,
        ) as ws:
            self.websocket = ws
            logger.info("NapCat 连接成功")
            if on_ready:
                await on_ready()
            await self._listen(ws)
        self.websocket = None
        return True

    async def _listen(self, ws):
        """监听 WebSocket 消息循环"""
        async for message in ws:
            try:
                event = json.loads(message)
                echo = event.get("echo")
                if echo and echo in self.pending_futures:
                    future = self.pending_futures[echo]
                    if not future.done():
                        status = event.get("status")
                        if status == "ok":
                            future.set_result(event.get("data"))
                        else:
                            future.set_result(event)
                else:
                    pt = event.get("post_type", "?")
                    mid = event.get("message_id", "?")
                    logger.info(f"[WS] post_type={pt} message_id={mid} echo={echo}")
                    # 聊天消息 → 转统一 Message → on_message；其他 → on_event
                    msg = self.parse_event(event)
                    if msg is not None:
                        if self._message_callback:
                            await self._message_callback(msg)
                    else:
                        if self._event_callback:
                            await self._event_callback(event)
            except json.JSONDecodeError:
                logger.warning(f"无法解析的消息: {message[:200]}")
            except Exception as e:
                logger.error(f"事件处理异常: {e}")

    # ── OneBot 事件 → 统一 Message ──
    def parse_event(self, event: Dict) -> Optional[Message]:
        """把 OneBot 的 message 事件转成统一 Message；非消息 / 应过滤的返回 None。

        - 自己发的消息 → None
        - 群聊且没 @ 机器人（group_mention_only 开启）→ None
        - 无文本也无图片 → None
        """
        if event.get("post_type") != "message":
            return None
        # 忽略自己发的
        _sender_uid = event.get("sender", {}).get("user_id") if isinstance(event.get("sender"), dict) else None
        if (_sender_uid is not None
                and self.bot.config.get("message", {}).get("ignore_self", True)
                and _sender_uid == self.bot.config.get("self_id")):
            return None

        user_id = event.get("user_id")
        group_id = event.get("group_id")
        if user_id is None:
            return None

        # 群聊: 仅响应 @机器人
        raw = event.get("raw_message", "")
        if event.get("message_type") == "group":
            bot_id = event.get("self_id") or self.bot.config.get("self_id")
            if (self.bot.config.get("message", {}).get("group_mention_only", True)
                    and bot_id
                    and f"[CQ:at,qq={bot_id}]" not in raw
                    and f"@{bot_id}" not in raw):
                return None
            raw = raw.replace(f"[CQ:at,qq={bot_id}]", "").replace(f"@{bot_id}", "").strip()

        # 解析文本 / 图片 / 回复引用
        text = raw
        images = []
        reply_to = None
        message_raw = event.get("message")
        if isinstance(message_raw, list):
            parts = []
            for seg in message_raw:
                seg_type = seg.get("type")
                if seg_type == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
                elif seg_type == "image":
                    images.append(seg.get("data", {}))
                elif seg_type == "reply" and reply_to is None:
                    reply_to = seg.get("data", {}).get("id")
            text = "".join(parts).strip()
        if not text and not images:
            return None

        mid = event.get("message_id")
        return Message(
            channel="onebot",
            session_id=Message.make_session_id("onebot", str(user_id), group_id),
            user_id=str(user_id),
            group_id=group_id,
            text=text,
            images=images,
            reply_to=str(reply_to) if reply_to is not None else None,
            message_id=str(mid) if mid is not None else None,
            raw=event,
        )

    # ── 请求-响应 ──
    def _next_message_id(self) -> int:
        self.message_id_counter += 1
        return self.message_id_counter

    async def api_call(self, action: str, params: Optional[Dict] = None,
                       timeout: float = 30) -> Any:
        if not self.websocket:
            logger.error("WebSocket未连接")
            return None
        msg_id = self._next_message_id()
        request = {"action": action, "params": params or {}, "echo": msg_id}
        future = asyncio.get_event_loop().create_future()
        self.pending_futures[msg_id] = future
        try:
            await self.websocket.send(json.dumps(request, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"WebSocket请求超时 ({action})")
            return None
        except Exception as e:
            logger.error(f"WebSocket请求失败: {e}")
            return None
        finally:
            self.pending_futures.pop(msg_id, None)

    # ── 发送 ──
    async def send_private(self, user_id: int, message: str) -> Optional[int]:
        user_id = int(user_id)
        now = time.time()
        content_hash = hashlib.md5(message.encode()).hexdigest()[:8]
        key = (user_id, content_hash)
        last = self._last_send_time.get(key, 0)
        if now - last < SEND_THROTTLE_SECONDS:
            logger.warning(
                f"[SEND_BLOCK] 阻止重复发送 user={user_id} "
                f"hash={content_hash} gap={now - last:.2f}s msg={message[:40]}"
            )
            return None
        self._last_send_time[key] = now
        logger.info(f"[SEND] user={user_id} hash={content_hash} msg={message[:40]}")

        data = await self.api_call(
            "send_private_msg", {"user_id": user_id, "message": message}
        )
        if data and isinstance(data, dict):
            return data.get("message_id")
        return None

    async def send_group(self, group_id: int, message: str) -> Optional[int]:
        group_id = int(group_id)
        data = await self.api_call(
            "send_group_msg", {"group_id": group_id, "message": message}
        )
        if data and isinstance(data, dict):
            return data.get("message_id")
        return None
