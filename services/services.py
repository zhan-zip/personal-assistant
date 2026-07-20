"""
外部 API 包装: 视觉模型 / 联网搜索 / 图片下载 / URL 抓取
LLM 客户端在 llm.py, 这里只做 LLM 之外的服务

所有 HTTP 请求统一走 aiohttp (异步非阻塞), 共享 bot.get_http_session() 连接池
"""
import asyncio
import aiohttp
import base64
import logging
import os
import re
from typing import TYPE_CHECKING, Optional, Dict

from bs4 import BeautifulSoup

from core.utils import MIME_BY_EXT

if TYPE_CHECKING:
    from bot import QQBot

logger = logging.getLogger("services")


class VisionClient:
    """通义千问视觉模型"""

    def __init__(self, vision_client, model: str):
        self.client = vision_client
        self.model = model

    async def describe(self, image_data_url: str) -> Optional[str]:
        if not self.client:
            return None
        logger.info(f"调用视觉模型: model={self.model} url_prefix={image_data_url[:60]}")

        def _sync_call():
            return self.client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": image_data_url}},
                        {"type": "text",
                         "text": "请用简洁的中文描述这张图片里有什么。只描述事实，不要联想。"},
                    ]
                }],
                max_tokens=500,
                temperature=0.1
            )

        try:
            response = await asyncio.to_thread(_sync_call)
            result = response.choices[0].message.content.strip()
            logger.info(f"视觉模型返回: {result[:100]}")
            return result
        except Exception as e:
            logger.error(f"视觉模型调用失败: {type(e).__name__}: {e}")
            return None


class SearchClient:
    """博查联网搜索 (aiohttp 异步)"""

    def __init__(self, api_key: str, base_url: str, freshness: str = "noLimit"):
        self.api_key = api_key
        self.base_url = base_url
        self.freshness = freshness

    async def search(self, query: str, count: int = 5) -> Optional[str]:
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "query": query,
                "freshness": self.freshness,
                "count": count,
                "answer": True
            }
            # 获取 bot 实例的共享 session (需要在运行时注入)
            # search 方法在 bot 实例化后被调用, 此时可通过全局 or 传参拿到 session
            # 这里用 _get_bot_session() 小工具函数
            session = _get_bot_session()
            async with session.post(self.base_url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()

            logger.info(f"搜索API响应(raw): {str(data)[:500]}")

            if data.get("code") != 200:
                logger.error(f"搜索API错误: {data}")
                return None

            lines = []
            result_data = data.get("data", {})
            if result_data.get("answer"):
                lines.append(str(result_data["answer"]))
            pages = (result_data.get("pages")
                     or result_data.get("webPages", {}).get("value", []))
            for i, page in enumerate(pages[:count], 1):
                title = page.get("title") or page.get("name", "")
                snippet = page.get("snippet", "")
                if title or snippet:
                    lines.append(f"{i}. {title}: {snippet}")
            result = "\n".join(lines) if lines else None
            if result:
                logger.info(f"联网搜索完成: {result[:100]}...")
            return result
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            return None


class MediaService:
    """图片下载 / URL 抓取 (aiohttp 异步)"""

    def __init__(self, bot: "QQBot"):
        self.bot = bot

    async def download_qq_image(self, image_info: Dict) -> Optional[str]:
        vcfg = self.bot.config.get("vision", {})
        if vcfg.get("download_via_napcat", True):
            try:
                file_param = image_info.get("file", "")
                data = await self.bot._send_ws_request("get_image", {
                    "file": file_param
                }, timeout=10)
                if data:
                    # 情况1: dict 中有 base64
                    if isinstance(data, dict):
                        if data.get("data"):
                            b64 = data["data"]
                            mime = data.get("type", "image/png")
                            return f"data:{mime};base64,{b64}"
                        # 情况2: dict 中只有 file → 本地路径
                        if data.get("file"):
                            return self._read_local_as_data_url(data["file"])
                    # 情况3: 字符串
                    if isinstance(data, str) and data:
                        if data.startswith("data:") or len(data) > 200:
                            return data
                        if os.path.exists(data):
                            return self._read_local_as_data_url(data)
            except Exception as e:
                logger.error(f"NapCat下载图片失败: {e}")

        img_url = image_info.get("url", "")
        if img_url:
            try:
                headers = {
                    "Referer": "https://qq.com/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                async with self.bot.get_http_session().get(
                    img_url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        img_data = base64.b64encode(img_bytes).decode()
                        mime = resp.headers.get("Content-Type", "image/png")
                        logger.info(f"下载图片成功: {img_url[:60]}...")
                        return f"data:{mime};base64,{img_data}"
            except Exception as e:
                logger.error(f"下载图片失败: {img_url[:60]}... {e}")

        return None

    @staticmethod
    def _read_local_as_data_url(file_path: str) -> Optional[str]:
        try:
            with open(file_path, "rb") as f:
                img_bytes = f.read()
            b64 = base64.b64encode(img_bytes).decode()
            ext = os.path.splitext(file_path)[1].lower()
            mime = MIME_BY_EXT.get(ext, "image/png")
            logger.info(f"本地图片转base64成功, size={len(img_bytes)}")
            return f"data:{mime};base64,{b64}"
        except Exception as e:
            logger.error(f"读取本地图片失败: {file_path} -> {e}")
            return None

    async def fetch_url(self, url: str) -> Optional[str]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            async with self.bot.get_http_session().get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")

                if "image" in content_type:
                    img_bytes = await resp.read()
                    img_data = base64.b64encode(img_bytes).decode()
                    ext = content_type.split("/")[-1].split(";")[0]
                    if ext in ("jpeg", "jpg", "jfif"):
                        mime = "image/jpeg"
                    elif ext == "png":
                        mime = "image/png"
                    elif ext == "gif":
                        mime = "image/gif"
                    elif ext == "webp":
                        mime = "image/webp"
                    else:
                        mime = f"image/{ext}"
                    data_url = f"data:{mime};base64,{img_data}"
                    desc = await self.bot.vision.describe(data_url)
                    return f"[图片]\n{desc}" if desc else "[图片]\n(无法识别)"
                else:
                    html = await resp.text()
                    # 使用 BeautifulSoup 提取可读文本
                    soup = BeautifulSoup(html, "html.parser")

                    # 移除无用标签
                    for tag in soup(["script", "style", "nav", "footer", "header",
                                     "noscript", "iframe", "svg", "form",
                                     "button", "input", "select"]):
                        tag.decompose()

                    # 提取标题
                    title = ""
                    if soup.title and soup.title.string:
                        title = soup.title.string.strip()

                    # 提取 meta description
                    meta_desc = ""
                    meta = soup.find("meta", attrs={"name": "description"})
                    if meta and meta.get("content"):
                        meta_desc = meta["content"].strip()
                    if not meta_desc:
                        meta = soup.find("meta", attrs={"property": "og:description"})
                        if meta and meta.get("content"):
                            meta_desc = meta["content"].strip()

                    # 提取正文文本
                    body = soup.body if soup.body else soup
                    text = body.get_text(separator="\n", strip=True)
                    # 清理空行和多余空白
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                    text = "\n".join(lines)

                    # 组装结果: 标题 + 描述 + 正文 (截断)
                    parts = []
                    if title:
                        parts.append(f"标题: {title}")
                    if meta_desc:
                        parts.append(f"摘要: {meta_desc[:200]}")
                    if text:
                        # 取前 1000 字
                        display = text[:1000] if len(text) > 1000 else text
                        if len(text) > 1000:
                            display += "\n...(内容过长已截断)"
                        parts.append(display)
                    result = "\n\n".join(parts) if parts else None
                    if result:
                        logger.info(f"fetch_url 网页摘要: {result[:150]}...")
                    return f"[网页摘要]\n{result}" if result else "[网页内容为空]"
        except Exception as e:
            logger.error(f"访问URL失败: {e}")
            return None


# ── 工具函数：获取 bot 的共享 session ──────────────────

_bot_instance: Optional["QQBot"] = None


def set_bot_instance(bot: "QQBot"):
    """在 bot 初始化后调用，让 services 模块能访问共享 session"""
    global _bot_instance
    _bot_instance = bot


def _get_bot_session() -> aiohttp.ClientSession:
    """获取 bot 的共享 aiohttp session"""
    if _bot_instance is None:
        raise RuntimeError("bot 实例尚未初始化，请先调用 set_bot_instance()")
    return _bot_instance.get_http_session()
