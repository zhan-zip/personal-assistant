"""
公共工具: 北京时区 / 时间格式化 / 文件安全读写 / MIME 映射
"""
from datetime import datetime, timezone, timedelta
from html import unescape
import re
from typing import Optional

BEIJING_TZ = timezone(timedelta(hours=8))

# 文件后缀 → MIME 类型 (供图片 base64 data URL 使用)
MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp",
}


def now_iso() -> str:
    """返回当前北京时间的 ISO 字符串"""
    return datetime.now(BEIJING_TZ).isoformat()


def format_time(abstime, feedstime: str = "") -> str:
    """把 abstime (秒级时间戳) 或 feedstime (字符串) 格式化为 MM-DD HH:MM"""
    if abstime and str(abstime).isdigit():
        try:
            dt = datetime.fromtimestamp(int(abstime), BEIJING_TZ)
            return dt.strftime("%m-%d %H:%M")
        except (ValueError, OSError):
            return str(abstime)
    return str(feedstime or "").strip()


def strip_html(html: str) -> str:
    """把一段 html 转成纯文本: 解码转义 → 移除标签 → 折叠空白"""
    if not html:
        return ""
    text = html.replace("\\x3C", "<").replace("\\x3E", ">").replace("\\x22", '"')
    text = unescape(text)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[\s ]*\n+", "\n", text)
    return text.strip()


# ── LLM 输出硬过滤 ─────────────────────────────────
# 防止 LLM 偶尔幻觉, 把"用户发了多遍"等违禁表达塞进回复
# 这是兜底层, 不依赖 persona 提示
_BAN_PATTERNS = [
    r"你发了两遍", r"你又发了", r"又发了一遍", r"你发了两条",
    r"你卡了", r"你卡了吧", r"复读机", r"重复了",
    r"来来回回", r"你重发了", r"你又重发", r"发了好几遍",
    r"发了(\d|两|三|几)遍", r"重复发", r"又是这句",
    r"你确实发了.*遍", r"我确实收到了.*遍",
    r"收到了两遍", r"收到了三遍", r"收到了多遍",
    r"收到了(\d|两|三|几)遍",
]
_BAN_RE = re.compile("|".join(_BAN_PATTERNS))

_SANITIZE_FALLBACK = "嗯, 继续聊吧。"


def sanitize_llm_response(response: str) -> str:
    """扫描 LLM 输出, 命中违禁表达就替换为兜底文案

    优先保留可读性: 命中点超过一半才整体替换, 否则只删违禁子句
    """
    if not response:
        return response
    if not _BAN_RE.search(response):
        return response
    # 整体替换, 因为这种话一出现基本就是在强行解释, 整句没意义
    return _SANITIZE_FALLBACK
