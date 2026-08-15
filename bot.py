"""
QQ AI 机器人主类
- WebSocket 接入 NapCat
- 编排各模块 (LLM / Vision / Profile / Commands / Proactive / QZone / Event)
"""
import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Optional, Any

import aiohttp
import yaml
from dotenv import load_dotenv
from openai import OpenAI

from protocols.onebot import OneBotAdapter
from protocols.web import WebAdapter
from qzone.qzone_browser import QZoneBrowser, _extract_f_info_text
from core.commands import CommandHandler
from proactive.proactive import ProactiveManager
from profile.profile import ProfileManager
from services.services import VisionClient, SearchClient, MediaService, set_bot_instance
from core.progress_reporter import ProgressReporter
from llm.llm import LLMClient
from core.message_processor import MessageProcessor
from core.event_handler import EventHandler
from core.utils import now_iso, format_time
from core.memory_store import get_store

logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)

# 关键: 给 root logger 也配置 handler, 否则子 logger (qzone_browser / event_handler /
# profile / long_memory / commands) 的日志会因为没 propagate handler 而被吞掉,
# 表现就是: 代码里有 logger.info 但 bot.log 看不到, 排查 bug 时极度困难
_root = logging.getLogger()
if not _root.handlers:
    _root.addHandler(handler)
    _root.setLevel(logging.INFO)

CHAT_MEMORY_FILE = "chat_memory.json"
PROACTIVE_FILE = "proactive_friends.json"
PROFILE_STATE_FILE = "profile_state.json"

class QQBot:
    """机器人主类, 仅做状态管理 + WS 编排"""

    def __init__(self, config_path: str = "config.yaml"):
        # 加载 .env
        load_dotenv()

        # 配置 / 客户端
        self.config = self._load_config(config_path)
        self._apply_env_to_config(self.config)

        # LLM providers (多模型路由 + 容灾): name → OpenAI client
        self._llm_clients: Dict[str, OpenAI] = {}
        for name, prov in self.config["llm"].get("providers", {}).items():
            if prov.get("api_key"):
                self._llm_clients[name] = OpenAI(
                    api_key=prov["api_key"],
                    base_url=prov["base_url"],
                )

        vcfg = self.config.get("vision", {})
        self.vision_client = None
        if vcfg.get("enabled") and vcfg.get("api_key"):
            self.vision_client = OpenAI(
                api_key=vcfg["api_key"],
                base_url=vcfg.get(
                    "base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
            )

        # 运行状态
        self.running = False
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._bg_tasks: List[asyncio.Task] = []  # 后台任务引用, 重连时取消
        self.chat_memory: Dict = {}   # 内存缓存, 启动时从 SQLite 载入
        self.proactive_cache: Dict = self._load_proactive()
        self.memory_store = get_store()   # SQLite 记忆存储层

        # 人设缓存
        self.persona_cache: Optional[str] = None
        self.persona_mtime: float = 0.0

        # 协议适配器列表 (支持多协议并发) —— 协议细节收口在各 adapter
        self.adapters: Dict[str, Any] = {}
        ncfg = self.config.get("napcat", {})
        self.adapters["onebot"] = OneBotAdapter(
            self,
            ws_url=ncfg.get("ws_url", "ws://localhost:3001"),
            access_token=ncfg.get("access_token", ""),
            timeout=ncfg.get("timeout", 30),
        )
        wcfg = self.config.get("web", {})
        if wcfg.get("enabled", True):
            self.adapters["web"] = WebAdapter(
                self,
                host=wcfg.get("host", "127.0.0.1"),
                port=wcfg.get("port", 8080),
            )
        # 兼容引用: 默认 adapter 指向 onebot
        self.adapter = self.adapters["onebot"]
        self._retry_count = 0
        self._bg_started = False

        # 外部 MCP 客户端 (如 Food-Time 饮食工具), 启动时连接并入 tool-calling
        self.mcp_clients: Dict[str, Any] = {}

        # 向量记忆 (RAG 语义检索, ChromaDB), 启动时构建, 失败降级为纯关键词检索
        self.vector_store: Optional[Any] = None

        # 撤回相关
        self.message_id_buffer: Dict[str, Any] = {}
        # message_id → 接收时间 (用于周期性撤回扫描: 只检查最近 N 分钟内收到的)
        self.message_recv_time: Dict[str, float] = {}
        # 本轮已确认触发的撤回 message_id 集合, 用于防 LLM 幻觉:
        # handle_recall 把 message_id 加进来, message_processor 检查 LLM 输出
        # 是否含"你撤回了"但当前 message_id 不在集合里 → 视为幻觉, 过滤
        self._verified_recall_ids: set = set()
        self.recall_cooldown: Dict[str, float] = {}

        # 长期记忆 (per-user facts)
        from core.long_memory import LongMemory
        self.long_memory = LongMemory()

        # 主动消息
        self.daily_proactive_count: Dict[str, int] = {}
        self.daily_proactive_date: str = ""

        # 资料缓存
        self.current_nickname: Optional[str] = None
        self.current_signature: Optional[str] = None
        self.current_avatar_b64: Optional[str] = None
        self.current_background_b64: Optional[str] = None
        self.last_nickname_change: float = 0.0
        self.last_signature_change: float = 0.0
        self.last_avatar_change: float = 0.0
        self.last_background_change: float = 0.0
        self.last_qzone_publish: float = 0.0
        self._load_profile_state()

        # QZone
        self.qzone_browser: Optional[QZoneBrowser] = None
        self._qzone_initializing = False

        # 子系统
        self.llm = LLMClient(
            providers=self._llm_clients,
            config=self.config["llm"],
            temperature=self.config["llm"]["temperature"],
            max_tokens=self.config["llm"]["max_tokens"],
        )
        self.vision = VisionClient(
            vision_client=self.vision_client,
            model=self.config.get("vision", {}).get("model", "qwen-vl-max"),
        )
        scfg = self.config.get("search", {})
        self.search = SearchClient(
            api_key=scfg.get("api_key", ""),
            base_url=scfg.get("base_url", "https://api.bochaai.com/v1/web-search"),
            freshness=scfg.get("freshness", "noLimit"),
        )
        self.media = MediaService(self)
        self.profile_manager = ProfileManager(self)
        self.command_handler = CommandHandler(self)
        self.proactive_manager = ProactiveManager(self)
        self.message_processor = MessageProcessor(self)
        self.event_handler = EventHandler(self)
        self.progress = ProgressReporter(self)

        set_bot_instance(self)

    # ==================== HTTP 会话 ====================

    def get_http_session(self) -> aiohttp.ClientSession:
        """懒加载共享 aiohttp session，避免每次请求创建新连接"""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    # ==================== 加载/保存 ====================

    def _load_config(self, path: str) -> Dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    @staticmethod
    def _apply_env_to_config(config: Dict):
        """将 .env 中的环境变量注入到 config 中，覆盖空值占位符"""
        # LLM providers (多模型路由): deepseek 主对话, qwen 容灾/视觉
        lcfg = config.setdefault("llm", {})
        providers = lcfg.setdefault("providers", {})
        for name, prov in providers.items():
            if name == "deepseek":
                if not prov.get("api_key"):
                    prov["api_key"] = os.getenv("DEEPSEEK_API_KEY", "")
                if not prov.get("base_url"):
                    prov["base_url"] = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
                models = prov.setdefault("models", {})
                if not models.get("chat"):
                    models["chat"] = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
            elif name == "qwen":
                # qwen 容灾 provider 复用视觉专属空间的 key / base_url
                if not prov.get("api_key"):
                    prov["api_key"] = os.getenv("VISION_API_KEY", "")
                if not prov.get("base_url"):
                    prov["base_url"] = os.getenv("VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
                models = prov.setdefault("models", {})
                if not models.get("chat"):
                    models["chat"] = os.getenv("QWEN_MODEL", "qwen-plus")
                if not models.get("vision"):
                    models["vision"] = os.getenv("VISION_MODEL", "qwen-vl-max")

        # Vision
        vcfg = config.get("vision", {})
        if not vcfg.get("api_key"):
            vcfg["api_key"] = os.getenv("VISION_API_KEY", "")
        if not vcfg.get("base_url"):
            vcfg["base_url"] = os.getenv("VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        if not vcfg.get("model"):
            vcfg["model"] = os.getenv("VISION_MODEL", "qwen-vl-max")

        # Search
        scfg = config.get("search", {})
        if not scfg.get("api_key"):
            scfg["api_key"] = os.getenv("SEARCH_API_KEY", "")

        # Admin QQ
        pcfg = config.get("profile", {})
        admin_qq = os.getenv("ADMIN_QQ", "")
        if admin_qq and (not pcfg.get("admin_notify")):
            pcfg["admin_notify"] = int(admin_qq)

    def _load_chat_memory(self) -> Dict:
        if os.path.exists(CHAT_MEMORY_FILE):
            try:
                with open(CHAT_MEMORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_chat_memory(self):
        with open(CHAT_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(self.chat_memory, f, ensure_ascii=False, indent=2)

    def _load_proactive(self) -> Dict:
        if os.path.exists(PROACTIVE_FILE):
            try:
                with open(PROACTIVE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_proactive(self, data: Dict):
        with open(PROACTIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_profile_state(self):
        if os.path.exists(PROFILE_STATE_FILE):
            try:
                with open(PROFILE_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.current_nickname = data.get("nickname")
                self.current_signature = data.get("signature")
                self.current_avatar_b64 = data.get("avatar_b64")
                self.current_background_b64 = data.get("background_b64")
                self.last_nickname_change = data.get("last_nickname_change", 0.0)
                self.last_signature_change = data.get("last_signature_change", 0.0)
                self.last_avatar_change = data.get("last_avatar_change", 0.0)
                self.last_background_change = data.get("last_background_change", 0.0)
                logger.info(
                    f"已加载资料缓存: nickname={self.current_nickname}, "
                    f"signature={self.current_signature}"
                )
            except Exception as e:
                logger.warning(f"加载资料缓存失败: {e}")

    def _save_profile_state(self):
        data = {
            "nickname": self.current_nickname,
            "signature": self.current_signature,
            "avatar_b64": self.current_avatar_b64,
            "background_b64": self.current_background_b64,
            "last_nickname_change": self.last_nickname_change,
            "last_signature_change": self.last_signature_change,
            "last_avatar_change": self.last_avatar_change,
            "last_background_change": self.last_background_change,
        }
        with open(PROFILE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ==================== 人设 / 历史 ====================

    def _get_persona(self) -> str:
        file_path = self.config["persona"]["file_path"]
        if os.path.exists(file_path):
            mtime = os.path.getmtime(file_path)
            if mtime > self.persona_mtime:
                with open(file_path, "r", encoding="utf-8") as f:
                    self.persona_cache = f.read().strip()
                self.persona_mtime = mtime
                logger.info("人设文件已更新")
            return self.persona_cache or "你是一个智能助手。"
        return "你是一个智能助手。"

    @staticmethod
    def _get_cache_key(user_id: int, group_id: Optional[int] = None) -> str:
        if group_id:
            return f"group_{group_id}_{user_id}"
        return f"private_{user_id}"

    def _get_history(self, user_id: int, group_id: Optional[int] = None) -> List[Dict]:
        return self.chat_memory.get(self._get_cache_key(user_id, group_id), [])

    def _add_message(self, user_id: int, role: str, content: str,
                     group_id: Optional[int] = None,
                     real_message_id: Optional[int] = None):
        key = self._get_cache_key(user_id, group_id)
        if key not in self.chat_memory:
            self.chat_memory[key] = []
        msg = {
            "role": role,
            "content": content,
            "timestamp": now_iso(),
        }
        if real_message_id:
            msg["message_id"] = real_message_id
        self.chat_memory[key].append(msg)
        max_history = self.config["llm"]["max_history"]
        if len(self.chat_memory[key]) > max_history:
            self.chat_memory[key] = self.chat_memory[key][-max_history:]
        # 异步落盘 SQLite (不阻塞事件循环)
        try:
            asyncio.create_task(self.memory_store.add_message(
                key, msg["role"], msg["content"], msg["timestamp"],
                msg.get("message_id"),
            ))
        except Exception as e:
            logger.warning(f"[MEM] 落盘失败: {e}")
        # 异步同步到向量库 (语义记忆, 失败仅警告不阻断)
        if getattr(self, "vector_store", None) is not None:
            try:
                asyncio.create_task(self.vector_store.add_message(
                    key, msg["role"], msg["content"], msg["timestamp"],
                    self.llm.embed_texts,
                ))
            except Exception as e:
                logger.warning(f"[VEC] 向量同步失败: {e}")

    def _clear_history(self, user_id: int, group_id: Optional[int] = None) -> bool:
        key = self._get_cache_key(user_id, group_id)
        if key in self.chat_memory:
            self.chat_memory[key] = []
            try:
                asyncio.create_task(self.memory_store.clear_history(key))
            except Exception as e:
                logger.warning(f"[MEM] 清空落盘失败: {e}")
            return True
        return False

    async def _init_memory(self):
        """启动时从 SQLite 载入记忆到内存缓存; SQLite 为空时从旧 JSON 迁移"""
        store = self.memory_store
        await store.init()
        if await store.is_empty():
            legacy_mem = {}
            if os.path.exists(CHAT_MEMORY_FILE):
                try:
                    with open(CHAT_MEMORY_FILE, "r", encoding="utf-8") as f:
                        legacy_mem = json.load(f)
                except Exception:
                    legacy_mem = {}
            legacy_facts = {}
            facts_file = "user_facts.json"
            if os.path.exists(facts_file):
                try:
                    with open(facts_file, "r", encoding="utf-8") as f:
                        legacy_facts = json.load(f)
                except Exception:
                    legacy_facts = {}
            if legacy_mem or legacy_facts:
                await store.migrate_from_json(legacy_mem, legacy_facts)
        self.chat_memory = await store.load_all_messages()
        # 长期记忆注入 long_memory 内存缓存
        try:
            self.long_memory.set_store(store)
            self.long_memory._cache = await store.load_all_facts()
        except Exception as e:
            logger.warning(f"[MEM] 长期记忆加载失败: {e}")
        logger.info(f"[MEM] 记忆已加载: {len(self.chat_memory)} 个会话")

    async def _init_vector_store(self):
        """启动时构建向量记忆 (ChromaDB, 幂等)。失败降级为纯关键词检索, 不影响 bot 启动"""
        try:
            from core.vector_store import VectorStore
            vcfg = self.config.get("memory", {}).get("vector", {}) or {}
            if not vcfg.get("enabled", True):
                logger.info("[VEC] 向量记忆已在配置中关闭")
                return
            vs = VectorStore(persist_dir=vcfg.get("persist_dir", "vector_db"))
            await vs.rebuild(self.chat_memory, self.llm.embed_texts)
            self.vector_store = vs
            logger.info(f"[VEC] 语义记忆就绪 (共 {vs.count()} 条)")
        except Exception as e:
            logger.warning(f"[VEC] 向量库初始化失败, 降级为关键词检索: {e}")
            self.vector_store = None

    # ==================== 资料初始化 (启动时调用一次) ====================

    async def _init_profile_cache(self):
        """启动后从QQ API获取实际资料, 仅在缓存为空时执行"""
        if self.current_nickname is not None and self.current_signature is not None:
            return

        info = await self._send_ws_request("get_login_info", timeout=10)
        self_id = None
        if info and isinstance(info, dict):
            self_id = info.get("user_id")
            if self.current_nickname is None:
                self.current_nickname = info.get("nickname", "")

        if self_id:
            stranger = await self._send_ws_request(
                "get_stranger_info", {"user_id": self_id, "no_cache": True}, timeout=10
            )
            if stranger and isinstance(stranger, dict):
                if "nickname" in stranger:
                    self.current_nickname = stranger["nickname"]
                if self.current_signature is None:
                    self.current_signature = stranger.get("sign", "")
                logger.info(
                    f"从QQ获取实际资料: nickname={self.current_nickname}, "
                    f"signature={self.current_signature}"
                )
        self._save_profile_state()

    # ==================== 协议能力代理 ====================
    # 所有协议相关操作统一转发给 adapter，核心模块无需感知底层协议

    async def _send_ws_request(self, action: str, params: Optional[Dict] = None,
                               timeout: float = 30) -> Any:
        return await self.adapter.api_call(action, params, timeout)

    async def _send_private_msg(self, user_id: int, message: str) -> Optional[int]:
        return await self.adapter.send_private(user_id, message)

    async def _send_group_msg(self, group_id: int, message: str):
        return await self.adapter.send_group(group_id, message)

    async def send_text(self, channel: str, user_id, group_id,
                        message: str) -> Optional[int]:
        """按 channel 路由发送 (核心跨协议发送入口)"""
        adapter = self.adapters.get(channel)
        if adapter is None:
            logger.warning(f"[SEND] 未知 channel: {channel}, 消息丢弃: {message[:30]}")
            return None
        if group_id:
            return await adapter.send_group(group_id, message)
        return await adapter.send_private(user_id, message)

    # ==================== LLM / 服务封装 ====================

    async def _call_vision(self, image_url: str) -> Optional[str]:
        return await self.vision.describe(image_url)

    async def _web_search(self, query: str, count: int = 5) -> Optional[str]:
        return await self.search.search(query, count)

    async def _fetch_url(self, url: str) -> Optional[str]:
        return await self.media.fetch_url(url)

    async def _download_image_from_qq(self, image_info: Dict) -> Optional[str]:
        return await self.media.download_qq_image(image_info)

    # ==================== 白/黑名单 ====================

    def _is_whitelisted(self, user_id: int, group_id: Optional[int]) -> bool:
        cfg = self.config["whitelist"]
        if not cfg.get("enabled"):
            return True
        if group_id and group_id in cfg.get("groups", []):
            return True
        return user_id in cfg.get("users", [])

    def _is_blacklisted(self, user_id: int, group_id: Optional[int]) -> bool:
        cfg = self.config["blacklist"]
        if not cfg.get("enabled"):
            return False
        if group_id and group_id in cfg.get("groups", []):
            return True
        return user_id in cfg.get("users", [])

    # ==================== 主动消息注册 (有缓存避免反复读盘) ====================

    def _register_proactive_user(self, user_id: int, group_id: Optional[int]):
        """把新私聊用户加入 proactive_cache, 只在第一次写入磁盘"""
        user_str = str(user_id)
        if user_str in self.proactive_cache:
            return
        self.proactive_cache[user_str] = {"enabled": False}
        self._save_proactive(self.proactive_cache)
        if not group_id:
            logger.info(f"新好友已注册: {user_id}")

    # ==================== 主动消息指令 ====================

    def _handle_proactive_command(self, user_id: int, args_raw: str) -> str:
        args = args_raw.strip().split()

        if not args:
            return self._proactive_help()

        sub = args[0]
        if sub in ("列表", "list"):
            if not self.proactive_cache:
                return "暂无好友记录。发一条私聊消息即可自动添加。"
            lines = ["好友主动消息状态:"]
            for uid, info in sorted(
                self.proactive_cache.items(), key=lambda x: str(x[0])
            ):
                status = "开启" if info.get("enabled") else "关闭"
                note = f" - {info.get('note', '')}" if info.get("note") else ""
                lines.append(f"  {uid}: {status}{note}")
            return "\n".join(lines)

        if sub in ("开启", "open", "关闭", "close") and len(args) >= 2:
            target = args[1]
            enabled = sub in ("开启", "open")
            if target in self.proactive_cache:
                self.proactive_cache[target]["enabled"] = enabled
                self._save_proactive(self.proactive_cache)
                return f"{target} 的主动消息{'已开启' if enabled else '已关闭'}。"
            return f"{target} 不在列表中，请先私聊一次。"

        if sub in ("全部开启", "all_on"):
            for uid in self.proactive_cache:
                self.proactive_cache[uid]["enabled"] = True
            self._save_proactive(self.proactive_cache)
            return "已开启所有好友的主动消息。"

        if sub in ("全部关闭", "all_off"):
            for uid in self.proactive_cache:
                self.proactive_cache[uid]["enabled"] = False
            self._save_proactive(self.proactive_cache)
            return "已关闭所有好友的主动消息。"

        if sub in ("触发", "trigger") and len(args) >= 2:
            target = args[1]
            if target not in self.proactive_cache:
                return f"{target} 不在列表中。"
            asyncio.create_task(self.proactive_manager.trigger_manual(int(target)))
            return f"正在给 {target} 发送主动消息..."

        if sub in ("备注", "note") and len(args) >= 3:
            target = args[1]
            note = " ".join(args[2:])
            if target not in self.proactive_cache:
                self.proactive_cache[target] = {"enabled": False, "note": ""}
            self.proactive_cache[target]["note"] = note
            self._save_proactive(self.proactive_cache)
            return f"{target} 的备注已更新: {note}"

        return self._proactive_help()

    @staticmethod
    def _proactive_help() -> str:
        return """#主动 指令:
#主动 列表 - 查看所有好友状态
#主动 开启 <QQ> - 开启主动消息
#主动 关闭 <QQ> - 关闭主动消息
#主动 全部开启 / 全部关闭
#主动 触发 <QQ> - 立刻测试发送
#主动 备注 <QQ> <内容> - 添加备注"""

    # ==================== QZone ====================

    async def _ensure_qzone_browser(self) -> Optional[QZoneBrowser]:
        if self.qzone_browser and self.qzone_browser._initialized:
            return self.qzone_browser
        if self._qzone_initializing:
            logger.info("[QZone] 浏览器正在初始化中，跳过")
            return None
        self._qzone_initializing = True
        try:
            # 尝试多个 domain 拿 cookies, NapCat 不同版本可能域名不同
            cookies_text = None
            for domain in (".qzone.qq.com", "qzone.qq.com", "user.qzone.qq.com", "taotao.qzone.qq.com"):
                raw = await self._send_ws_request(
                    "get_cookies", {"domain": domain}, timeout=8
                )
                if raw and isinstance(raw, dict) and raw.get("cookies"):
                    cookies_text = raw["cookies"]
                    logger.info(f"[QZone] 从 {domain} 拿到 {len(cookies_text)} chars cookies")
                    break
            if not cookies_text:
                logger.error("[QZone] 所有 domain 都拿不到 cookies")
                return None

            uin = str(self.config.get("self_id", ""))
            if not uin:
                info = await self._send_ws_request("get_login_info", timeout=10)
                if info and isinstance(info, dict):
                    uin = str(info.get("user_id", ""))
                    if uin:
                        self.config["self_id"] = uin
            if not uin:
                logger.error("[QZone] 未知 self_id，请先收到一条消息后再试")
                return None

            logger.info(f"[QZone] 正在启动浏览器... uin={uin}")
            browser = QZoneBrowser()
            if await browser.init(cookies_text, uin):
                self.qzone_browser = browser
                return browser
            return None
        finally:
            self._qzone_initializing = False

    async def _handle_qzone_publish(self, content: str, user_id: int = 0) -> str:
        browser = await self._ensure_qzone_browser()
        if not browser:
            return "QZone: 浏览器初始化失败，请稍后重试"

        async def _on_publish_progress(msg: str):
            """发布阶段进度回调：直接发送进度消息"""
            try:
                await self._send_private_msg(user_id, msg)
            except Exception:
                pass

        feed_id = await browser.publish_text(content, on_progress=_on_publish_progress)
        if feed_id:
            # 通知管理员
            user_str = f"用户 {user_id}" if user_id else "AI"
            await self.profile_manager.notify_admin(
                f"[QZone发布] {user_str} 发布了一条动态: \"{content[:50]}\" → id={feed_id}"
            )
            return f"动态已发布 (id={feed_id})"
        return "发动态失败，请检查QQ空间状态"

    async def _handle_qzone_feeds(self, target_uin: Optional[str] = None,
                                   mode: str = "self",
                                   user_id: int = 0,
                                   group_id: Optional[int] = None) -> str:
        """#动态列表 / #空间 <QQ号> / #好友圈

        mode:
            - "self"          #空间 / #动态列表 - 只看自己空间里自己发的
            - "target"        #空间 <QQ号> - 只看某QQ空间里他/她发的
            - "friend_circle" #好友圈 - 公开好友圈所有好友的动态

        user_id/group_id: 用于增量进度消息的发送目标
        """
        browser = await self._ensure_qzone_browser()
        if not browser:
            return "QZone: 浏览器初始化失败，请稍后重试"

        # 自识别: 如果 target_uin 是机器人自己, 自动切换为 self 模式
        if mode == "target" and target_uin:
            bot_self_id = str(self.config.get("self_id", "") or "")
            if bot_self_id and target_uin == bot_self_id:
                logger.info(
                    f"[QZone] 目标QQ {target_uin} 是机器人自己，切换为 self 模式"
                )
                mode = "self"
                target_uin = None

        # 构建增量进度回调: 每翻一页新数据, 发送 LLM 润色的进度消息
        async def _on_feeds_progress(entries: list):
            if not entries:
                return
            # 取最新 3 条
            latest = entries[-3:]
            summaries = []
            for e in latest:
                nickname = (e.get("nickname") or "?").strip()[:10]
                if not nickname or nickname == "?":
                    nickname = f"QQ{e.get('uin', '?')}"
                summary = (e.get("_display_text") or e.get("summary") or "").strip()[:30]
                if summary:
                    summaries.append(f"[{nickname}]{summary}")
            if not summaries:
                return

            # 尝试调用 LLM 生成自然评价
            try:
                prompt = (
                    f"你正在帮用户翻QQ空间动态。目前翻到{len(entries)}条，"
                    f"最新的是：{' | '.join(summaries[:3])}。"
                    f"请用一句话（15-25字）自然地告诉用户进展，"
                    f"可以评价内容特点或表达继续翻的决心。"
                    f"直接输出一句话，不要加引号。"
                )
                llm_msg = await self.llm.chat([
                    {"role": "user", "content": prompt}
                ])
                if llm_msg and llm_msg.strip():
                    msg = llm_msg.strip()
                else:
                    raise Exception("empty LLM response")
            except Exception:
                # 降级为模板拼接
                msg = f"正在翻空间动态... 目前看到 {len(entries)} 条了:\n" + "\n".join(
                    f"  {s}" for s in summaries
                )

            try:
                if group_id:
                    await self._send_group_msg(group_id, msg)
                elif user_id:
                    await self._send_private_msg(user_id, msg)
            except Exception:
                pass

        try:
            entries = await browser.get_feeds(
                20, target_uin=target_uin, mode=mode, max_scroll=20,
                on_progress=_on_feeds_progress if (user_id or group_id) else None
            )
            if not entries:
                if mode == "friend_circle":
                    return "QZone: 好友圈暂无可见动态 (可尝试查看具体好友的个人空间)"
                who = f"QQ {target_uin} " if target_uin else ""
                return f"QZone: {who}暂无可见动态 (用户可能未发布公开说说, 或隐私设置限制了访问)"

            lines = []
            if mode == "friend_circle":
                lines.append("=== QQ空间 好友圈 最近动态 ===")
            elif target_uin:
                lines.append(f"=== QQ {target_uin} 空间 最近动态 ===")
            else:
                lines.append("=== QQ空间 最近动态 ===")
            count = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # 内容: 优先用预存的 _display_text, 否则重新解析
                content = entry.get("_display_text", "").strip()
                if not content:
                    content = entry.get("summary", "").strip()
                if not content:
                    content = _extract_f_info_text(entry.get("html", ""))
                if not content:
                    content = "(空)"

                nickname = entry.get("nickname", "匿名") or "匿名"
                time_str = format_time(
                    entry.get("abstime", ""), entry.get("feedstime", "")
                )

                flags = []
                if entry.get("pic") or entry.get("image") or entry.get("hasPic"):
                    flags.append("📷")
                if entry.get("rt_tid") or entry.get("rt_"):
                    flags.append("🔁")
                flag_str = f" {'/'.join(flags)}" if flags else ""

                count += 1
                content_short = content[:80] + ("…" if len(content) > 80 else "")
                if time_str:
                    lines.append(
                        f"  {count}. [{nickname}]{flag_str} {content_short}  ({time_str})"
                    )
                else:
                    lines.append(f"  {count}. [{nickname}]{flag_str} {content_short}")

            if count == 0:
                lines.append("  (过滤后未找到真实动态)")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[QZone列表] 异常: {e}")
            return f"QZone: 查询失败: {e}"

    # ==================== 主循环 ====================

    def _reset_connection_state(self):
        """每次新连接前重置依赖旧连接的核心临时状态"""
        # 取消所有旧的后台任务
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        self._bg_started = False

        self.message_id_buffer.clear()
        self.message_recv_time.clear()
        self._verified_recall_ids.clear()

    async def run(self):
        """主循环: 并发连接所有 adapter, 各自断线自动重连"""
        await self._init_memory()   # 从 SQLite 载入记忆 / 迁移旧 JSON
        await self._init_vector_store()   # 语义记忆向量库 (失败自动降级关键词检索)
        await self._init_mcp_clients()   # 连接外部 MCP server (如 Food-Time)
        tasks = [
            asyncio.create_task(self._adapter_loop(name, adp))
            for name, adp in self.adapters.items()
        ]
        await asyncio.gather(*tasks)

    async def _init_mcp_clients(self):
        """连接外部 MCP server (Food-Time), 把它的饮食工具并入 tool-calling"""
        try:
            from mcp_integration.client import create_food_time_client
            from llm.tools import TOOLS
            client = create_food_time_client()
            tool_defs = await client.connect()
            if tool_defs:
                self.mcp_clients["food_time"] = client
                TOOLS.extend(tool_defs)
                logger.info(f"[MCP-client] Food-Time {len(tool_defs)} 个工具已并入 tool-calling")
            else:
                await client.close()
        except Exception as e:
            logger.warning(f"[MCP-client] 连接 Food-Time 失败: {e}")

    async def _adapter_loop(self, name: str, adp):
        """单个 adapter 的连接 + 自动重连循环"""
        max_backoff = 60  # 最大重试间隔（秒）
        while True:
            try:
                self._reset_connection_state()
                if hasattr(adp, "reset_state"):
                    adp.reset_state()
                label = (f"重连 {name} (第{self._retry_count}次)"
                         if self._retry_count else f"连接 {name}")
                logger.info(label)
                await adp.connect(
                    on_event=self._on_adapter_event,
                    on_message=self._on_message,
                    on_ready=self._on_adapter_ready,
                )
            except Exception as e:
                logger.error(f"[{name}] 连接断开: {e}")
            finally:
                self.running = False

            self._retry_count += 1
            delay = min(max_backoff, 2 ** self._retry_count)
            logger.info(f"[{name}] {delay} 秒后自动重连...")
            await asyncio.sleep(delay)

    async def _on_adapter_ready(self):
        """连接建立后、开始监听前，初始化核心状态（只做一次）"""
        self.running = True
        self._retry_count = 0
        if self._bg_started:
            return
        self._bg_started = True
        logger.info("连接成功, 初始化核心...")
        await self._init_profile_cache()
        # 后台生成进度消息池 (不阻塞主流程)
        asyncio.create_task(self.progress.init_pool())
        await self._start_background_tasks()

    async def _on_message(self, message):
        """Adapter 收到统一 Message → 交给事件处理器"""
        asyncio.create_task(self.event_handler.handle_message(message))

    async def _on_adapter_event(self, event: Dict):
        """Adapter 收到非消息事件（撤回通知/心跳等）→ 交给事件处理器"""
        asyncio.create_task(self.event_handler.handle_event(event))

    async def _shutdown(self):
        """最终清理: 关闭所有适配器 / 浏览器 / HTTP 会话"""
        self.running = False
        for adp in self.adapters.values():
            try:
                await adp.close()
            except Exception:
                pass
        if self.qzone_browser:
            try:
                await self.qzone_browser.close()
            except Exception:
                pass
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        try:
            await self.memory_store.close()
        except Exception:
            pass
        for _name, _client in self.mcp_clients.items():
            try:
                await _client.close()
            except Exception:
                pass

    async def _start_background_tasks(self):
        if self.config.get("proactive", {}).get("enabled"):
            logger.info(
                f"主动消息已启用, 检查间隔: "
                f"{self.config['proactive'].get('check_interval_minutes', 10)}分钟"
            )
            self._bg_tasks.append(asyncio.create_task(self.proactive_manager.proactive_loop()))

        if self.config.get("profile", {}).get("auto_modify_enabled"):
            logger.info(
                f"自主修改资料已启用, 检查间隔: "
                f"{self.config['profile'].get('auto_modify_interval_minutes', 60)}分钟"
            )
            self._bg_tasks.append(asyncio.create_task(self.proactive_manager.auto_modify_profile_loop()))

        if self.config.get("qzone", {}).get("auto_publish_enabled"):
            logger.info(
                f"QZone自主发动态已启用, 检查间隔: "
                f"{self.config['qzone'].get('auto_publish_interval_minutes', 90)}分钟"
            )
            self._bg_tasks.append(asyncio.create_task(self.proactive_manager.auto_qzone_loop()))

        # 撤回扫描
        logger.info("撤回扫描已启动 (每 30 秒扫一次, 检查最近 5 分钟内的消息)")
        self._bg_tasks.append(asyncio.create_task(self._recall_scan_loop()))

    async def _recall_scan_loop(self):
        """周期性扫描历史消息, 检查是否有撤回

        关键设计:
        - 扫描间隔 30 秒 (避免频繁 get_msg)
        - 只检查 5 分钟内的消息 (避免消息已被 GC 导致的 false positive)
        - 用双证据: buffer 里有原文 + get_msg 拿不到 raw_message
        """
        while True:
            try:
                await asyncio.sleep(30)
            except Exception:
                break
            try:
                now = time.time()
                # 复制 keys, 避免遍历时修改
                for mid, recv_time in list(self.message_recv_time.items()):
                    # 5 分钟前的消息不检查 (NapCat 缓存可能过期, 误报率高)
                    if now - recv_time > 300:
                        continue
                    # 已确认撤回过的跳过
                    if mid in self._verified_recall_ids:
                        continue
                    # 调用 get_msg 检查
                    original = self.message_id_buffer.get(mid)
                    if not original:
                        continue
                    try:
                        result = await self._send_ws_request(
                            "get_msg", {"message_id": int(mid)}, timeout=5
                        )
                    except Exception as e:
                        logger.debug(f"[撤回扫描] get_msg {mid} 异常: {e}")
                        continue
                    # 双证据: 拿不到 raw_message + buffer 有原文 → 真撤回
                    if not result or not isinstance(result, dict):
                        continue
                    raw = result.get("raw_message", "") or result.get("message", "")
                    if not raw:
                        # 找 user_id 用于 handle_recall
                        uid = (
                            result.get("sender", {}).get("user_id")
                            if isinstance(result.get("sender"), dict)
                            else None
                        )
                        if uid:
                            logger.info(
                                f"[撤回扫描] 消息 {mid} 已撤回 "
                                f"(recv_time {now-recv_time:.0f}s 前), 原文: "
                                f"{original[:40]!r}"
                            )
                            # 模拟 friend_recall notice 的处理
                            # scan 发现的撤回不设 cooldown (历史撤回),
                            # 因为 scan loop 自身用 _verified_recall_ids 防重复
                            await self.event_handler.handle_recall(
                                user_id=uid,
                                msg_id=mid,
                                content=original,
                                bypass_cooldown=True,
                            )
                            # 标记为已确认, 避免重复触发
                            self._verified_recall_ids.add(mid)
            except Exception as e:
                logger.error(f"[撤回扫描] loop 异常: {e}")


if __name__ == "__main__":
    bot = QQBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("机器人已停止")
