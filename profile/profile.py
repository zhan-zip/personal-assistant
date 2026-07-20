"""
资料管理:
- 修改/查询 QQ 资料
- 修改头像/背景图
- 自然语言意图检测
- 解析 LLM 响应中的 PROFILE_CHANGE 等标记
"""
import aiohttp
import base64
import logging
import re
import time
from typing import TYPE_CHECKING, Optional, Dict

from core.utils import MIME_BY_EXT

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("profile")

_SEX_DISPLAY = {
    "male": "男", "female": "女", "unknown": "未设置",
    1: "男", 2: "女", 0: "未设置",
}


class ProfileManager:
    """集中所有资料相关 API + 意图检测"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot

    # ── WS 通知辅助 ────────────────────────────────
    async def notify_admin(self, message: str):
        admin_qq = self.bot.config.get("profile", {}).get("admin_notify", 0)
        if admin_qq:
            await self.bot._send_private_msg(admin_qq, message)

    # ── 资料修改 ──────────────────────────────────
    async def set_avatar(self, image_url: str = None, image_b64: str = None) -> bool:
        """更换QQ头像（通过 NapCat 扩展API）"""
        if not image_b64 and not image_url:
            return False

        if image_url and not image_b64:
            try:
                async with self.bot.get_http_session().get(
                    image_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    resp.raise_for_status()
                    img_bytes = await resp.read()
                    image_b64 = base64.b64encode(img_bytes).decode("utf-8")
            except Exception as e:
                logger.error(f"下载头像图片失败: {e}")
                return False

        logger.info(f"正在设置头像, base64长度: {len(image_b64) if image_b64 else 0}")
        for api_name in ("set_qq_avatar", "set_self_avatar", "set_avatar"):
            result = await self.bot._send_ws_request(api_name, {
                "file": f"base64://{image_b64}"
            }, timeout=20)
            if result is not None and not (isinstance(result, dict) and result.get("status") == "failed"):
                self.bot.current_avatar_b64 = image_b64
                self.bot.last_avatar_change = time.time()
                self.bot._save_profile_state()
                logger.info(f"头像设置成功 (API: {api_name})")
                return True
        logger.error("头像设置失败: 所有已知API均不响应")
        return False

    async def set_background(self, image_url: str = None, image_b64: str = None) -> bool:
        """更换QQ主页背景图"""
        if not image_b64 and not image_url:
            return False
        if image_url and not image_b64:
            try:
                async with self.bot.get_http_session().get(
                    image_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    resp.raise_for_status()
                    img_bytes = await resp.read()
                    image_b64 = base64.b64encode(img_bytes).decode("utf-8")
            except Exception as e:
                logger.error(f"下载背景图片失败: {e}")
                return False

        logger.info(f"正在设置背景, base64长度: {len(image_b64) if image_b64 else 0}")
        for api_name in ("set_background", "set_self_background", "set_profile_background"):
            result = await self.bot._send_ws_request(api_name, {
                "file": f"base64://{image_b64}"
            }, timeout=20)
            if result is not None and not (isinstance(result, dict) and result.get("status") == "failed"):
                self.bot.current_background_b64 = image_b64
                self.bot.last_background_change = time.time()
                self.bot._save_profile_state()
                logger.info(f"背景设置成功 (API: {api_name})")
                return True
        logger.error("背景设置失败: 所有已知API均不响应")
        return False

    async def set_profile(self, nickname: str = None, signature: str = None) -> bool:
        """修改QQ个人资料（昵称 / 个性签名），NapCat 要求 nickname 必传"""
        bot = self.bot

        if bot.current_nickname is None:
            info = await bot._send_ws_request("get_login_info", timeout=10)
            self_id = None
            if info and isinstance(info, dict):
                self_id = info.get("user_id")
                bot.current_nickname = info.get("nickname", "")
            if self_id and not bot.current_nickname:
                stranger = await bot._send_ws_request("get_stranger_info", {
                    "user_id": self_id, "no_cache": True
                }, timeout=10)
                if stranger and isinstance(stranger, dict):
                    bot.current_nickname = stranger.get("nickname", "")
            if bot.current_nickname is None:
                bot.current_nickname = ""
            bot._save_profile_state()

        target_nick = nickname if nickname is not None else bot.current_nickname
        target_sig = signature if signature is not None else bot.current_signature

        params = {"nickname": target_nick or ""}
        if target_sig is not None:
            params["personal_note"] = target_sig

        logger.info(f"修改资料: {params}")
        result = await bot._send_ws_request("set_qq_profile", params, timeout=15)
        if result is None:
            return False
        if isinstance(result, dict) and result.get("status") == "failed":
            logger.error(f"修改资料失败: {result}")
            return False

        if nickname is not None:
            bot.current_nickname = nickname
            bot.last_nickname_change = time.time()
        if signature is not None:
            bot.current_signature = signature
            bot.last_signature_change = time.time()
        bot._save_profile_state()
        logger.info(f"资料修改成功: nick={nickname}, sig={signature}")
        return True

    async def get_profile(self) -> Dict:
        """获取机器人自己的个人资料，优先用 get_stranger_info 实时拉取QQ数据"""
        bot = self.bot
        self_id = bot.config.get("self_id", "")
        result = {"user_id": self_id or "未知"}

        if self_id:
            stranger = await bot._send_ws_request("get_stranger_info", {
                "user_id": self_id, "no_cache": True
            }, timeout=10)
            if stranger and isinstance(stranger, dict):
                # 调试: 输出实际 raw data 便于排查 sex 字段
                import json as _json
                logger.info(
                    f"[get_profile] self raw data: "
                    f"{_json.dumps(stranger, ensure_ascii=False)[:400]}"
                )
                result["nickname"] = stranger.get("nickname") or bot.current_nickname or "未设置"
                result["signature"] = (stranger.get("long_nick")
                                       or stranger.get("longNick")
                                       or stranger.get("signature")
                                       or bot.current_signature or "未设置")
                # 性别: 兼容多种字段名 (sex / gender) + 多种值类型 (1/2/0, "male"/"female")
                sex_val = (stranger.get("sex")
                           if "sex" in stranger
                           else stranger.get("gender"))
                if sex_val is None or sex_val == "":
                    sex_display = "未设置"
                else:
                    sex_display = _SEX_DISPLAY.get(
                        sex_val if isinstance(sex_val, int) else str(sex_val).strip().lower(),
                        f"未设置 (raw={sex_val})"
                    )
                result["sex"] = sex_display

                by = stranger.get("birthday_year")
                bm = stranger.get("birthday_month")
                bd = stranger.get("birthday_day")
                if by and bm and bd:
                    try:
                        result["birthday"] = f"{by}-{int(bm):02d}-{int(bd):02d}"
                    except (ValueError, TypeError):
                        result["birthday"] = "未设置"
                else:
                    bday = stranger.get("birthday", "")
                    result["birthday"] = str(bday) if bday else "未设置"
                return result

        # 降级
        result["nickname"] = bot.current_nickname or "未设置"
        result["signature"] = bot.current_signature or "未设置"
        result["sex"] = "未知"
        result["birthday"] = "未知"
        return result

    # ── # 指令处理 ──────────────────────────────────
    async def handle_command(self, user_id: int, args_raw: str) -> str:
        """处理 #修改昵称 / #修改签名 / #查看资料 指令"""
        message = args_raw.strip()
        admin_qq = self.bot.config.get("profile", {}).get("admin_notify", 0)
        user_str = str(user_id)

        if message.startswith("查看资料") or message in ("资料", "查看"):
            profile = await self.get_profile()
            return (
                f"当前资料:\n"
                f"QQ号: {profile['user_id']}\n"
                f"昵称: {profile['nickname']}\n"
                f"个性签名: {profile['signature']}\n"
                f"性别: {profile['sex']}\n"
                f"生日: {profile['birthday']}"
            )

        if message.startswith("修改昵称"):
            nickname = message[len("修改昵称"):].strip()
            if not nickname:
                return "用法: #修改昵称 <新昵称>"
            if len(nickname) > 20:
                return "昵称过长，请控制在20个字符以内。"
            ok = await self.set_profile(nickname=nickname)
            if ok:
                await self.notify_admin(
                    f"[资料变更] 用户 {user_str} 通过指令修改了昵称: \"{nickname}\""
                )
                return f"昵称已修改为: {nickname}"
            return "昵称修改失败，请检查NapCat是否正常运行。"

        if message.startswith("修改签名"):
            signature = message[len("修改签名"):].strip()
            if not signature:
                return "用法: #修改签名 <新签名>"
            if len(signature) > 50:
                return "签名过长，请控制在50个字符以内。"
            ok = await self.set_profile(signature=signature)
            if ok:
                await self.notify_admin(
                    f"[资料变更] 用户 {user_str} 通过指令修改了签名: \"{signature}\""
                )
                return f"个性签名已修改为: {signature}"
            return "签名修改失败。"

        if message.startswith("修改头像"):
            url = message[len("修改头像"):].strip()
            if not url.startswith("http"):
                return "用法: #修改头像 <图片URL>，例如 #修改头像 https://example.com/avatar.jpg"
            ok = await self.set_avatar(image_url=url)
            if ok:
                await self.notify_admin(f"[资料变更] 用户 {user_str} 通过指令修改了头像")
                return "头像已修改。"
            return "头像修改失败，请检查URL是否可访问。"

        if message.startswith("修改背景"):
            url = message[len("修改背景"):].strip()
            if not url.startswith("http"):
                return "用法: #修改背景 <图片URL>，例如 #修改背景 https://example.com/bg.jpg"
            ok = await self.set_background(image_url=url)
            if ok:
                await self.notify_admin(f"[资料变更] 用户 {user_str} 通过指令修改了背景")
                return "背景已修改。"
            return "背景修改失败，请检查URL是否可访问。"

        return """资料修改指令:
#查看资料 / #资料 - 查看当前个人资料
#修改昵称 <新昵称> - 修改QQ昵称
#修改签名 <新签名> - 修改个性签名
#修改头像 <图片URL> - 修改QQ头像（传入图片链接）
#修改背景 <图片URL> - 修改QQ主页背景（传入图片链接）"""

    # ── 自然语言意图检测 ────────────────────────────
    async def detect_and_handle_intent(self, user_id: int, message: str) -> Optional[str]:
        """检测自然语言中的资料修改/查看意图，直接执行API。
        返回: 执行摘要字符串；未匹配但有疑似意图→幻觉防护；完全无关→None"""
        message_stripped = message.strip()

        # --- 查看资料 ---
        if re.search(r'(?:查看|看看|告诉我|显示|我的)(?:你的?)?(?:个人)?(?:资料|信息|昵称|签名|性别|年龄|生日|叫什么|名字)',
                     message_stripped) or message_stripped in ("资料", "个人信息"):
            profile = await self.get_profile()
            return (
                f"[系统: 已查询当前资料]\n"
                f"QQ号: {profile['user_id']}\n"
                f"昵称: {profile['nickname']}\n"
                f"个性签名: {profile['signature']}\n"
                f"性别: {profile['sex']}\n"
                f"生日: {profile['birthday']}"
            )

        # --- 修改昵称 ---
        # 关键: "叫" 单独出现 ≠ 改名, 例如"你叫什么"是疑问, 不是让AI改名
        # 必须同时含"改/换/设置/把/将" + 名字类名词 (昵称/名字/名称/称呼)
        nick_patterns = [
            r'(?:修改|改|换|设置)(?:一下|一个)?(?:你的?)?(?:昵称|名字|称呼|名称)(?:为|成|是|：|:)?\s*(.+)',
            r'(?:把|将)(?:你的?)?(?:昵称|名字|称呼|名称)(?:修改|改|换|设为|设置为|设为|改成|换成)(?:为|成|是|：|:)?\s*(.+)',
            r'改名[为成]?\s*(.+)',
            r'换名字[为成]?\s*(.+)',
        ]
        for pat in nick_patterns:
            m = re.search(pat, message_stripped)
            if m:
                value = m.group(1).strip().rstrip("。！!，, ")
                # 二次护栏: 如果 value 是疑问句/闲聊, 拒绝改名
                if value and len(value) <= 20 and not self._looks_like_question(value):
                    ok = await self.set_profile(nickname=value)
                    if ok:
                        await self.notify_admin(
                            f"[资料自然变更] 用户 {user_id} 让AI修改了昵称: \"{value}\""
                        )
                        return f"[系统: 昵称已修改为 \"{value}\"。请在回复中自然地告知用户。]"
                    return "[系统: 昵称修改失败。请在回复中告知用户稍后再试。]"
                # 解析出的是疑问/反问 → 不改名, 让 LLM 自然回答
                logger.info(
                    f"[昵称意图拦截] 解析值 {value!r} 看起来是疑问, 跳过改名"
                )
                break

        # --- 修改签名 ---
        sig_patterns = [
            r'(?:修改|改|换)(?:一下|一个)?(?:你的?)?(?:个性)?(?:签名|个签|资料)(?:为|成|是|：|:)?\s*(.+)',
            r'(?:把|将)(?:你的?)?(?:个性)?(?:签名|个签|资料)(?:修改|改|换)(?:为|成|是)?\s*(.+)',
            r'(?:签(?:名|字)改[为成]?|签名换成?)\s*(.+)',
        ]
        for pat in sig_patterns:
            m = re.search(pat, message_stripped)
            if m:
                value = m.group(1).strip().rstrip("。！!，, ")
                if value and len(value) <= 50:
                    ok = await self.set_profile(signature=value)
                    if ok:
                        await self.notify_admin(
                            f"[资料自然变更] 用户 {user_id} 让AI修改了签名: \"{value}\""
                        )
                        return f"[系统: 个性签名已修改为 \"{value}\"。请在回复中自然地告知用户。]"
                    return "[系统: 签名修改失败。请在回复中告知用户稍后再试。]"
                break

        # --- 修改性别 ---
        if re.search(r'(?:修改|改|换|设置|把|将)(?:一下|一个)?(?:你的?)?(?:性别|身份)(?:为|成|是|：|:)?',
                     message_stripped):
            return "[系统: 性别无法通过API修改，请告诉用户去QQ客户端自行设置。]"

        # --- 修改年龄 / 生日 ---
        if re.search(r'(?:修改|改|换|设置|把|将)(?:一下|一个)?(?:你的?)?(?:年龄|岁数|生日|出生日期)',
                     message_stripped):
            return "[系统: 生日/年龄无法通过API修改，请告诉用户去QQ客户端自行设置。]"

        # --- 修改头像 ---
        if re.search(r'(?:修改|改|换)(?:一下|一个)?(?:你的?)?(?:头像|QQ头像)(?:为|成|：|:)?',
                     message_stripped):
            return ("[系统: 收到头像修改请求。如果用户同时发了图片链接，请输出 "
                    "[AVATAR_CHANGE: url=链接]。否则告诉用户发送图片链接。]")

        # --- 修改背景 ---
        if re.search(r'(?:修改|改|换)(?:一下|一个)?(?:你的?)?(?:背景|主页背景|QQ背景)(?:为|成|：|:)?',
                     message_stripped):
            return ("[系统: 收到背景修改请求。如果用户同时发了图片链接，请输出 "
                    "[BACKGROUND_CHANGE: url=链接]。否则告诉用户发送图片链接。]")

        # --- 幻觉防护 ---
        if re.search(r'(?:修改|改一?下|换一?个|设置)(?:你的?)?(?:昵称|名字|签名|个签|资料|信息|性别|年龄|生日|头像|个人)',
                     message_stripped):
            return "[系统: 检测到资料修改意图但无法解析具体参数。请勿声称已经修改。直接告诉用户需要明确说明改什么、改成什么值。]"

        return None

    # ── 用户原文的"显式改动"判定 ────────────────────
    # 改昵称/签名/头像/背景: 用户必须**显式**说了"改/换/设置/把..."等动词
    # 单纯提问/闲聊/陈述**不算**改
    _PROFILE_TRIGGER_VERBS = (
        "改名", "改昵称", "换名字", "换个名字", "换个昵称", "设置昵称", "设置名字",
        "把昵称", "把名字", "改一下", "换一下", "把签名", "改签名", "换签名",
        "设置签名", "改个性签名", "换个性签名", "改一下签名", "换个签名",
        "把头像", "换个头像", "换头像", "设置头像", "改头像",
        "把背景", "换个背景", "换背景", "设置背景", "改背景",
    )

    # 判定某个解析值"看起来像问题/闲聊, 而非新名字"
    _QUESTION_HINTS = (
        "什么", "谁", "怎么", "为啥", "为什么", "哪", "吗", "呢",
        "?", "？", "如何", "几岁", "多大", "谁啊", "哪位",
    )
    _NAME_KEYWORDS = (
        "昵称", "名字", "名称", "称呼", "叫",
    )

    def _looks_like_question(self, text: str) -> bool:
        """检测 value 是否是疑问/闲聊, 而不是新昵称值"""
        if not text:
            return True
        # 显疑问词开头
        if any(text.startswith(q) for q in ("什么", "谁", "哪", "怎么", "为", "为什", "多", "几", "如", "?", "？")):
            return True
        # 包含疑问词
        for q in self._QUESTION_HINTS:
            if q in text:
                return True
        # 包含"你/我"等主语 → 99% 是对话而非新名字
        if any(w in text for w in ("你", "我", "他", "她", "它", "谁", "自己")):
            return True
        # "叫"是疑问/称呼, 不是新名字
        if "叫" in text and not any(n in text for n in self._NAME_KEYWORDS):
            # "改名叫小闻" → "小闻"才是名字, 但 "叫什么" 是疑问
            # 这里 "叫" 和名字类同现, 视为新名字; 单独 "叫" 视为问题
            if not any(n in text for n in ("昵称", "名字", "名称", "称呼")):
                return True
        return False

    def _is_explicit_profile_request(self, user_text: str, change_kind: str) -> bool:
        """检查用户原文里是否真的说了"改X" (change_kind: nickname/signature/avatar/background)"""
        if not user_text:
            return False
        # 通用: 必须有"改/换/设置/把..."这类动词
        verb_ok = any(kw in user_text for kw in self._PROFILE_TRIGGER_VERBS)
        if not verb_ok:
            return False
        # 类型必须对应: 改昵称 ≠ 改头像
        if change_kind == "nickname":
            return any(kw in user_text for kw in (
                "昵称", "名字", "名称", "呢称"
            ))
        if change_kind == "signature":
            return "签名" in user_text or "个签" in user_text
        if change_kind == "avatar":
            return "头像" in user_text
        if change_kind == "background":
            return "背景" in user_text
        return False

    # ── 解析 LLM 响应中的标记 ─────────────────────────
    async def parse_and_execute_profile_changes(self, response: str, user_id: int, user_text: str = "") -> str:
        """解析 LLM 响应中的 [PROFILE_CHANGE: ...] / [AVATAR_CHANGE: ...] / [BACKGROUND_CHANGE: ...] 标记

        关键: 用户原文必须含显式改动动词 + 字段名, 否则视为 LLM 误判, 剥掉 marker 不执行
        """
        # PROFILE_CHANGE
        for match in re.findall(r'\[PROFILE_CHANGE:\s*([^\]]+?)\]', response):
            match = match.strip()
            logger.info(f"[资料自然语言触发] 解析到: {match}")
            if match.startswith("nickname="):
                if not self._is_explicit_profile_request(user_text, "nickname"):
                    logger.warning(
                        f"[PROFILE_GUARD] 拒绝改昵称, 用户原文: {user_text[:50]!r}, "
                        f"标记: {match[:50]!r}"
                    )
                    continue
                value = match[len("nickname="):].strip()
                if value:
                    await self.set_profile(nickname=value)
                    logger.info(f"PROFILE_CHANGE: 昵称 -> {value}")
            elif match.startswith("signature="):
                if not self._is_explicit_profile_request(user_text, "signature"):
                    logger.warning(
                        f"[PROFILE_GUARD] 拒绝改签名, 用户原文: {user_text[:50]!r}, "
                        f"标记: {match[:50]!r}"
                    )
                    continue
                value = match[len("signature="):].strip()
                if value:
                    await self.set_profile(signature=value)
                    logger.info(f"PROFILE_CHANGE: 签名 -> {value}")

        # AVATAR_CHANGE
        for match in re.findall(r'\[AVATAR_CHANGE:\s*url=([^\]]+?)\]', response):
            if not self._is_explicit_profile_request(user_text, "avatar"):
                logger.warning(
                    f"[PROFILE_GUARD] 拒绝改头像, 用户原文: {user_text[:50]!r}"
                )
                continue
            url = match.strip()
            logger.info(f"[头像自然语言触发] URL: {url}")
            ok = await self.set_avatar(image_url=url)
            if ok:
                await self.notify_admin(f"[头像自然变更] 用户 {user_id} 让AI修改了头像")

        # BACKGROUND_CHANGE
        for match in re.findall(r'\[BACKGROUND_CHANGE:\s*url=([^\]]+?)\]', response):
            if not self._is_explicit_profile_request(user_text, "background"):
                logger.warning(
                    f"[PROFILE_GUARD] 拒绝改背景, 用户原文: {user_text[:50]!r}"
                )
                continue
            url = match.strip()
            logger.info(f"[背景自然语言触发] URL: {url}")
            ok = await self.set_background(image_url=url)
            if ok:
                await self.notify_admin(f"[背景自然变更] 用户 {user_id} 让AI修改了背景")

        # QZONE_PUBLISH
        for match in re.findall(r'\[QZONE_PUBLISH:\s*content=(.+?)\]', response):
            content = match.strip()
            logger.info(f"[QZone自然语言触发] 内容: {content[:50]}")
            try:
                result = await self.bot._handle_qzone_publish(content, user_id)
                logger.info(f"[QZone自然语言触发] 结果: {result}")
            except Exception as e:
                logger.error(f"[QZone自然语言触发] 失败: {e}")

        cleaned = re.sub(r'\s*\[PROFILE_CHANGE:\s*[^\]]*\]', '', response)
        cleaned = re.sub(r'\s*\[AVATAR_CHANGE:\s*[^\]]*\]', '', cleaned)
        cleaned = re.sub(r'\s*\[BACKGROUND_CHANGE:\s*[^\]]*\]', '', cleaned)
        cleaned = re.sub(r'\s*\[QZONE_PUBLISH:\s*[^\]]*\]', '', cleaned)
        return cleaned.strip()
