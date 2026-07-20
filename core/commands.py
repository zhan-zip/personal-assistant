"""
所有 # 开头的指令处理集合。
- 原 bot.py 内 _handle_command / _handle_proactive_command / _handle_profile_command 整合到此处
- 仍然依赖 bot 实例 (self.bot) 来调用 LLM / 视觉 / 资料修改 / QZone 等
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("commands")

BEIJING_TZ = timezone(timedelta(hours=8))


class CommandHandler:
    """集中管理所有 # 指令"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot

    # ── 入口分发 ──────────────────────────────────────
    async def handle(self, user_id: int, raw_message: str, group_id: Optional[int] = None) -> Optional[str]:
        message = raw_message
        # 修正全角井号 / 前导空格
        message = message.replace("＃", "#")
        if message.startswith("# ") and len(message) > 2:
            message = "#" + message[2:].lstrip()

        # ── 基础指令 ──
        if message.startswith("#重置") or message.startswith("#clear") or message.startswith("#cls"):
            self.bot._clear_history(user_id, group_id)
            return "记忆已清除。"

        if message.startswith("#人设") or message.startswith("#persona"):
            persona = self.bot._get_persona()
            if len(persona) > 200:
                return f"当前人设:\n{persona[:200]}..."
            return f"当前人设:\n{persona}"

        if message.startswith("#帮助") or message.startswith("#help"):
            return self._help_text()

        if message.startswith("#历史") or message.startswith("#history"):
            return self._history_text(user_id, group_id)

        if message.startswith("#白名单") or message.startswith("#whitelist"):
            return self._whitelist_text()

        if message.startswith("#状态") or message.startswith("#status") or message.startswith("#stats"):
            return self._stats_text()

        if message.startswith("#主动") or message.startswith("#proactive"):
            return self._proactive(user_id, message)

        if (message.startswith("#修改昵称") or message.startswith("#修改签名")
                or message.startswith("#修改头像") or message.startswith("#修改背景")
                or message == "#查看资料" or message == "#资料"):
            return await self.bot.profile_manager.handle_command(user_id, message[1:])

        if message.startswith("#查 ") or message.startswith("#查\uff20") or message.startswith("#查用户"):
            return await self._query_user(message)

        if message.startswith("#动态详情") or message.startswith("#查看动态") or message.startswith("#qzone_detail"):
            return await self._qzone_detail(message)

        # ── 时间/搜索/视觉/抓取/QZone ──
        if message.startswith("#时间") or message.startswith("#time"):
            return self._time_text()

        if message.startswith("#搜索") or message.startswith("#search"):
            return await self._search(message)

        if message.startswith("#测试识图") or message.startswith("#vision"):
            return await self._vision(message)

        if message.startswith("#访问") or message.startswith("#fetch"):
            return await self._fetch(message)

        if message.startswith("#发动态") or message.startswith("#qzone") and not message.startswith("#qzone_feeds") and not message.startswith("#qzone_detail"):
            return await self._qzone_publish(message, user_id)

        if message.startswith("#删动态") or message.startswith("#delete_feed"):
            return "删动态需手动操作：请登录QQ空间自行删除"

        if message.startswith("#动态列表") or message.startswith("#qzone_feeds"):
            return await self.bot._handle_qzone_feeds(mode="self", user_id=user_id, group_id=group_id)

        # #空间 <QQ号> - 查看某个QQ号空间里他/她发的动态
        if message.startswith("#空间") or message.startswith("#qq空间") or message.startswith("#QQ空间"):
            # 提取参数: 去掉前缀
            arg = ""
            for prefix in ("#qq空间", "#QQ空间", "#空间"):
                if message.startswith(prefix):
                    arg = message[len(prefix):].strip()
                    break
            if not arg:
                # 不带QQ号 → 等价 #动态列表, 看自己
                return await self.bot._handle_qzone_feeds(mode="self", user_id=user_id, group_id=group_id)
            if not arg.isdigit():
                return "用法: #空间 <QQ号>  (查看某QQ空间他/她发的, 不带QQ号 = #动态列表)"
            return await self.bot._handle_qzone_feeds(target_uin=arg, mode="target", user_id=user_id, group_id=group_id)

        # #好友圈 / #空间圈 - 公开好友圈里所有人的动态
        if message.startswith("#好友圈") or message.startswith("#空间圈") or message.startswith("#friend_circle"):
            return await self.bot._handle_qzone_feeds(mode="friend_circle", user_id=user_id, group_id=group_id)

        return None

    # ── 基础指令实现 ────────────────────────────────
    @staticmethod
    def _help_text() -> str:
        return """【 # 硬性命令 —— 以下命令直接触发功能，不经过AI判断 】
#帮助 / #help — 显示此帮助
#重置 / #clear / #cls — 清除对话记忆
#人设 / #persona — 查看当前人设
#时间 / #time — 当前北京时间
#历史 / #history — 最近对话记录
#状态 / #status / #stats — 接收层统计
#白名单 / #whitelist — 白名单状态
#查 <QQ号> — 查询用户资料

【 资料管理 】
#查看资料 / #资料 — 查看机器人资料
#修改昵称 <新昵称>
#修改签名 <新签名>
#修改头像 <图片URL>
#修改背景 <图片URL>

【 主动消息 】
#主动 / #proactive 列表|开启|关闭|全部开启|全部关闭|触发|备注

【 QZone 动态 】
#发动态 / #qzone <内容> — 发说说
#动态列表 / #qzone_feeds — 看自己动态
#好友圈 / #空间圈 / #friend_circle — 好友圈
#空间 / #qq空间 <QQ号> — 看某人空间
#动态详情 / #查看动态 / #qzone_detail <id> — 动态详情
#删动态 / #delete_feed — (已禁用)

【 搜索 & 识图 】
#搜索 / #search <关键词> — 联网搜索
#测试识图 / #vision <URL> — 识别图片
#访问 / #fetch <URL> — 访问链接

【 自然语言 —— 不用 # 也能用 】
直接对机器人说话，AI 自动判断是否需要搜索/看空间/发动态/识图等。
例如: "帮我搜一下xxx"、"看看空间动态"、"发一条说说xxx"、"现在几点了" """

    def _history_text(self, user_id: int, group_id: Optional[int]) -> str:
        history = self.bot._get_history(user_id, group_id)
        if not history:
            return "暂无对话记录。"
        lines = []
        for i, msg in enumerate(reversed(history[-10:])):
            role = "我" if msg["role"] == "user" else "小闻"
            lines.append(f"{len(history) - i}. [{role}] {msg['content'][:50]}")
        return "\n".join(lines)

    def _whitelist_text(self) -> str:
        cfg = self.bot.config["whitelist"]
        status = "开启" if cfg["enabled"] else "关闭"
        users = ", ".join(str(u) for u in cfg["users"]) if cfg["users"] else "无"
        groups = ", ".join(str(g) for g in cfg["groups"]) if cfg["groups"] else "无"
        return f"白名单状态: {status}\n用户白名单: {users}\n群聊白名单: {groups}"

    def _stats_text(self) -> str:
        """返回接收层诊断统计 (用于排查'消息被推两遍'等问题)"""
        stats = self.bot.event_handler.get_recv_stats()
        double_ids = stats["double_pushed_message_ids"]
        double_hashes = stats["double_pushed_content_hashes"]
        f = stats["filtered"]
        lines = [
            "=== 接收层诊断 ===",
            f"累计接收 message 事件: {stats['total_received']}",
            f"过滤数: 去重ID {f['by_dedup_id']}, 去重内容 {f['by_dedup_content']}, 其他 {f['by_other']}",
            f"被推多次的 message_id: {len(double_ids)} 个",
        ]
        if double_ids:
            for mid, cnt in list(double_ids.items())[:5]:
                lines.append(f"  - mid={mid} 出现 {cnt} 次")
        lines.append(f"内容相同 (跨 message_id) 的次数异常: {len(double_hashes)} 个")
        if double_hashes:
            for ch, cnt in list(double_hashes.items())[:5]:
                lines.append(f"  - hash={ch} 出现 {cnt} 次")
        return "\n".join(lines)

    @staticmethod
    def _time_text() -> str:
        now = datetime.now(BEIJING_TZ)
        weekday = "一二三四五六日"[now.weekday()]
        return f"{now.strftime('%Y-%m-%d %H:%M:%S')}（星期{weekday} UTC+8）"

    async def _search(self, message: str) -> str:
        query = message.split(" ", 1)[-1].strip() if " " in message else ""
        if not query:
            return "用法: #搜索 <关键词>"
        result = await self.bot._web_search(query)
        if result:
            return f"搜索结果:\n{result}"
        return "搜索失败，请检查API配置。"

    async def _vision(self, message: str) -> str:
        url = message.split(" ", 1)[-1].strip() if " " in message else ""
        if not url or not url.startswith("http"):
            return "用法: #测试识图 <图片URL>"
        desc = await self.bot._call_vision(url)
        return f"图片识别结果:\n{desc}" if desc else "图片识别失败。"

    async def _fetch(self, message: str) -> str:
        url = message.split(" ", 1)[-1].strip() if " " in message else ""
        if not url or not url.startswith("http"):
            return "用法: #访问 <URL>"
        result = await self.bot._fetch_url(url)
        return result or "无法访问该链接。"

    async def _qzone_publish(self, message: str, user_id: int = 0) -> str:
        # 兼容多种前缀: #发动态 / #qzone
        for prefix in ("#发动态", "#qzone"):
            if message.startswith(prefix):
                content = message[len(prefix):].strip()
                break
        else:
            content = ""
        if content:
            return await self.bot._handle_qzone_publish(content, user_id)
        return "用法: #发动态 / #qzone <内容>"

    async def _qzone_detail(self, message: str) -> str:
        """#动态详情 / #查看动态 / #qzone_detail <id> - 查看某条QQ空间动态的完整内容"""
        for prefix in ("#动态详情", "#查看动态", "#qzone_detail"):
            if message.startswith(prefix):
                fid = message[len(prefix):].strip()
                break
        else:
            fid = ""
        if not fid:
            return "用法: #动态详情 <动态id>\n(先 #动态列表 获取 id)"
        # 未启动 → 后台自动启动, 告诉用户稍等
        if not self.bot.qzone_browser or not self.bot.qzone_browser._initialized:
            asyncio.create_task(self.bot._ensure_qzone_browser())
            return "QQ空间浏览器未启动, 我正在后台启动, 请稍后再发一次 #动态详情。"
        detail = await self.bot.qzone_browser.get_feed_detail(fid)
        if not detail:
            return f"找不到动态 {fid}, 或抓取失败。"
        return (
            f"=== 动态 {fid} ===\n"
            f"时间: {detail.get('time', '?')}\n"
            f"内容: {detail.get('content', '')}\n"
            f"图片: {'有' if detail.get('images') else '无'}"
        )

    async def _query_user(self, message: str) -> str:
        """#查 <qq> - 查询某个QQ号的资料 (NapCat get_stranger_info + 视觉头像)"""
        if message.startswith("#查用户"):
            qq = message[3:].strip()
        else:
            qq = message[2:].strip()
        if not qq or not qq.isdigit():
            return "用法: #查 <QQ号>"
        qq_int = int(qq)
        data = await self.bot._send_ws_request(
            "get_stranger_info", {"user_id": qq_int}
        )
        if not data:
            return f"获取 {qq} 资料失败 (NapCat 未响应)。"
        logger.info(f"[#查] QQ {qq} raw data keys: {list(data.keys())}")
        logger.info(f"[#查] QQ {qq} raw data: {json.dumps(data, ensure_ascii=False)[:500]}")

        # 提取基础字段
        nickname = data.get("nickname", "(无)")
        # 性别: NapCat 实际可能返回 数字 1/2/0 或 字符串 "male"/"female"/"unknown"
        sex_code = data.get("sex", 0)
        sex_map_num = {1: "男", 2: "女", 0: "未设置"}
        sex_map_str = {"male": "男", "female": "女", "unknown": "未设置", "": "未设置"}
        if isinstance(sex_code, str):
            sex = sex_map_str.get(sex_code.strip().lower(), sex_code if sex_code else "未设置")
        else:
            sex = sex_map_num.get(sex_code, "未设置")

        # 生日: NapCat 的字段可能是 birthday 字符串 OR birthday_year/month/day 拆分
        # 也可能是 birth_year/month/day (旧版), 兼容 4 套
        birthday = str(data.get("birthday", "") or "").strip()
        y, m, d = 0, 0, 0
        if not birthday:
            y = data.get("birthday_year", 0) or data.get("birth_year", 0)
            m = data.get("birthday_month", 0) or data.get("birth_month", 0)
            d = data.get("birthday_day", 0) or data.get("birth_day", 0)
            if y and m and d:
                try:
                    birthday = f"{int(y)}-{int(m):02d}-{int(d):02d}"
                except (ValueError, TypeError):
                    birthday = ""
        # 生日可能只是 "MM-DD" 或 "M-D" 没年份
        if not birthday and m and d:
            try:
                birthday = f"??-{int(m):02d}-{int(d):02d} (仅月日)"
            except (ValueError, TypeError):
                birthday = ""

        if not birthday:
            birthday = "(未设置)"

        # 年龄: 优先用 API 返回的 age, 没有再算
        age = data.get("age", 0) or 0
        if not age and y:
            try:
                from datetime import datetime
                age = datetime.now().year - int(y)
            except (ValueError, TypeError):
                age = 0
        age_str = f"{age}岁" if age else "(未设置)"

        signature = data.get("signature", "").strip()
        long_nick = data.get("long_nick", "").strip()
        # 头像 URL: avatarUrl 或 q.qlogo.cn
        avatar_url = data.get("avatarUrl") or f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

        out = (
            f"=== QQ {qq} 资料 ===\n"
            f"昵称: {nickname}\n"
            f"性别: {sex}\n"
            f"生日: {birthday} (年龄: {age_str})\n"
            f"签名: {signature or '(空)'}"
        )
        if long_nick:
            out += f"\n简介: {long_nick[:120]}"

        # 视觉识别头像 (用 vision 描述头像图)
        if self.bot.vision and self.bot.vision_client:
            try:
                avatar_desc = await self.bot.vision.describe(avatar_url)
                if avatar_desc:
                    out += f"\n\n--- 头像视觉识别 ---\n{avatar_desc[:300]}"
            except Exception as e:
                out += f"\n(头像识别失败: {e})"

        return out

    # ── 主动消息子指令 ───────────────────────────────
    def _proactive(self, user_id: int, message: str) -> str:
        if message.startswith("#主动"):
            args_raw = message[len("#主动"):].strip()
        else:
            args_raw = message[len("#proactive"):].strip()
        return self.bot._handle_proactive_command(user_id, args_raw)
