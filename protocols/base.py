"""
协议适配层：统一消息模型 + 适配器接口

架构目标：
- 核心（event_handler / message_processor / bot）只依赖 Message / Session / BaseAdapter，
  不关心底层协议（QQ / 微信 / 网页）。
- 每个协议实现一个 Adapter，负责：
  1. 建立连接、收发消息
  2. 把协议特有的事件格式翻译成统一 Message
  3. 把统一 Session 翻译回协议侧的发送目标
- bot.py 持有 Adapter，网络细节不再泄漏到核心。

阶段说明：当前先落地"网络层分离"（连接 / 收发 / API 调用收口进 Adapter），
核心暂仍接收原始事件；随后再把核心切换为纯 Message 驱动。
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("protocols")

# 事件回调：Adapter 收到一条"原始事件"时调用（暂用于向核心转发，逐步改为 Message）
EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class Message:
    """统一消息模型：各协议翻译成这个交给核心处理"""

    channel: str                            # 来源协议："onebot" / "wechat" / "web"
    session_id: str                         # 跨协议唯一会话标识
    user_id: str                            # 协议内用户标识
    group_id: Optional[str] = None          # 群聊 ID（私聊为 None）
    text: str = ""                          # 文本内容
    images: List[Dict] = field(default_factory=list)   # 图片段列表
    reply_to: Optional[str] = None          # 被回复消息的引用
    raw: Dict[str, Any] = field(default_factory=dict)  # 原始事件（备用）

    @staticmethod
    def make_session_id(channel: str, user_id: str,
                        group_id: Optional[str] = None) -> str:
        return f"{channel}:{group_id or 'private'}:{user_id}"


@dataclass
class Session:
    """会话标识：用于路由与记忆隔离"""

    channel: str
    user_id: str
    group_id: Optional[str] = None


class BaseAdapter(ABC):
    """协议适配器抽象基类"""

    channel: str = "base"

    def __init__(self, bot: Any):
        self.bot = bot
        self._event_callback: Optional[EventCallback] = None

    # ── 生命周期 ──
    @abstractmethod
    async def connect(self, on_event: Optional[EventCallback] = None,
                      on_ready: Optional[Callable[[], Awaitable[None]]] = None) -> bool:
        """建立连接并开始监听。

        on_event: 收到原始事件时回调（当前传事件 dict）。
        on_ready: 连接建立后、开始监听前调用（用于核心初始化）。
        阻塞直到断线。返回是否正常退出。
        """

    @abstractmethod
    async def close(self) -> None: ...

    # ── 能力调用（协议 API） ──
    @abstractmethod
    async def api_call(self, action: str, params: Optional[Dict] = None,
                       timeout: float = 30) -> Any:
        """调用协议能力（OneBot 中如 get_stranger_info / get_msg 等）。"""

    # ── 发送 ──
    @abstractmethod
    async def send_private(self, user_id: int, message: str) -> Optional[int]:
        """发私聊文本，返回协议侧 message_id（无则 None）。"""

    @abstractmethod
    async def send_group(self, group_id: int, message: str) -> Optional[int]:
        """发群聊文本，返回协议侧 message_id（无则 None）。"""

    async def send_image(self, session: Session, data_url: str) -> Optional[int]:
        """默认不支持发图，子类按需覆盖。"""
        return None

    # ── 连接状态 ──
    @property
    def connected(self) -> bool:
        return False

    def get_self_id(self) -> Optional[str]:
        """返回机器人自己的账号标识（协议内）。"""
        return None
