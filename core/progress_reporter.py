"""
进度消息系统：长耗时工具调用期间，每 5 秒发送 LLM 润色的进度提示。
- 启动时用 LLM 预生成 10 条符合人设的润色消息池
- 运行时从池中随机选取发送
- LLM 生成失败则使用内置基础消息
"""
import asyncio
import logging
import random
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("progress")

# 内置基础消息（LLM 生成失败时的兜底）
_BASE_MESSAGES = [
    "正在处理中，请稍候...",
    "还在查找，等一下哦~",
    "数据有点多，我还在翻...",
    "马上就好，耐心等待一下~",
    "正在努力工作中...",
    "还在处理，别急哦~",
    "正在搜索中，稍等一下...",
    "正在查找，很快就好了~",
]

_POOL_GENERATION_PROMPT = """你是一个有个性的AI助手。请生成10条短小的"等待中"提示语，用于在后台执行搜索/查询时告知用户"请稍等"。

要求：
- 每条10-20字，口语化、自然、符合你的性格
- 风格多样：有俏皮的、温柔的、酷酷的、可爱的
- 不要重复，每条语义不同
- 用JSON数组返回：["消息1", "消息2", ...]
- 只返回JSON，不要其他文字"""


class ProgressReporter:
    """进度消息管理器"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot
        self.messages: List[str] = list(_BASE_MESSAGES)
        self._pool_ready = False
        # 顺序轮播: 当前索引, 避免连续重复
        self._index: int = 0
        self._last_pick: str = ""

    async def init_pool(self):
        """启动时异步生成 LLM 润色版消息池（不阻塞 bot 启动）"""
        try:
            result = await self.bot.llm._chat_json(
                _POOL_GENERATION_PROMPT,
                temperature=0.9,
                max_tokens=300,
            )
            if result and isinstance(result, list) and len(result) >= 5:
                self.messages = [str(m) for m in result if str(m).strip()]
                logger.info(f"进度消息池已生成: {len(self.messages)} 条")
                self._pool_ready = True
                return
        except Exception as e:
            logger.warning(f"LLM 生成进度消息池失败: {e}, 使用内置消息")
        self._pool_ready = True  # 兜底消息也算就绪

    def pick(self) -> str:
        """顺序轮播取一条进度消息，确保不与上次重复"""
        if not self.messages:
            return "处理中..."
        if len(self.messages) == 1:
            return self.messages[0]
        # 跳过上次的消息
        msg = self.messages[self._index % len(self.messages)]
        if msg == self._last_pick:
            self._index += 1
            msg = self.messages[self._index % len(self.messages)]
        self._index += 1
        self._last_pick = msg
        return msg

    async def with_progress(self, action: str, coro,
                            user_id: int, group_id: Optional[int]):
        """包装长耗时协程，期间发送进度消息 (首条等5秒, 之后每10秒)。

        Args:
            action: 操作描述（如 "搜索" / "查找动态"）
            coro: 要执行的协程
            user_id: 发送进度消息的目标
            group_id: 群聊ID（私聊为None）

        Returns:
            coro 的返回值
        """
        stop_flag = {"stop": False}

        async def _progress_loop():
            # 先等 5 秒再发第一条（给快速操作留余地）
            await asyncio.sleep(5)
            while not stop_flag["stop"]:
                msg = self.pick()
                try:
                    if group_id:
                        await self.bot._send_group_msg(group_id, msg)
                    else:
                        await self.bot._send_private_msg(user_id, msg)
                except Exception:
                    pass
                # 之后每 10 秒发一次
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    break

        progress_task = asyncio.create_task(_progress_loop())

        try:
            result = await coro
            return result
        finally:
            stop_flag["stop"] = True
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
