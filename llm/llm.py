"""
LLM 客户端 + 高层对话助手

所有调用大模型的入口都收口在本模块:
- LLMClient:    同步 LLM 调用 (用 to_thread 异步执行)
- chat_with_persona: 把人设 + 用户消息打包, 调用 LLM, 返回纯文本
- extract_json_decision: 容错从 LLM 输出中抠出 JSON
"""
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

from openai import OpenAI

logger = logging.getLogger("llm")


def extract_json_decision(content: str) -> Optional[Dict]:
    """容错解析 LLM 输出的 JSON (整段 JSON / 嵌入文本的 JSON / 失败返回 None)"""
    if not content:
        return None
    # 1. 整段就是 JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 2. 从文本里抠 {...} (贪婪取最外层)
    m = re.search(r'\{[\s\S]*\}', content)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


class LLMClient:
    """LLM 对话客户端

    - 构造时绑定 OpenAI 客户端 + 默认参数
    - chat(): 同步调用, 包成异步 (to_thread)
    - chat_with_persona(): 高层人设对话助手
    """

    def __init__(self, client: OpenAI, model: str,
                 temperature: float = 0.6,
                 max_tokens: int = 2048):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ── 同步调用 (内部) ──────────────────────────────
    def _chat_sync(self, messages: List[Dict],
                   temperature: Optional[float] = None,
                   max_tokens: Optional[int] = None) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            stream=False,
        )
        return resp.choices[0].message.content.strip()

    async def chat(self, messages: List[Dict],
                   temperature: Optional[float] = None,
                   max_tokens: Optional[int] = None) -> str:
        try:
            return await asyncio.to_thread(
                self._chat_sync, messages, temperature, max_tokens
            )
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return ""

    # ── Tool-calling 支持 ────────────────────────────
    def _chat_sync_with_tools(self, messages: List[Dict],
                              tools: List[Dict],
                              temperature: Optional[float] = None,
                              max_tokens: Optional[int] = None):
        """同步调用 LLM (带 tools), 返回完整 message 对象

        调用方通过 result.tool_calls / result.content 判断是否有工具调用
        """
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            stream=False,
        )
        return resp.choices[0].message

    async def chat_with_tools(self, messages: List[Dict],
                              tools: List[Dict],
                              temperature: Optional[float] = None,
                              max_tokens: Optional[int] = None):
        """异步 LLM 调用 (带 tools), 返回 message 对象

        Returns:
            ChatCompletionMessage | None (失败时返回 None)
        """
        try:
            return await asyncio.to_thread(
                self._chat_sync_with_tools, messages, tools, temperature, max_tokens
            )
        except Exception as e:
            logger.error(f"LLM tool-calling 失败: {e}")
            return None

    # ── 高层助手 ──────────────────────────────────
    async def chat_with_persona(self, persona: str, system_extra: str,
                                user_content: str,
                                temperature: Optional[float] = None,
                                max_tokens: Optional[int] = None) -> str:
        """把人设 + 系统上下文 + 用户内容打包, 单轮 LLM 调用"""
        system = persona.strip()
        if system_extra:
            system = f"{system}\n\n{system_extra.strip()}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        return await self.chat(messages, temperature=temperature, max_tokens=max_tokens)

    async def _chat_json(self, prompt: str,
                         temperature: float = 0.9,
                         max_tokens: int = 300):
        """发送 prompt 并期望返回 JSON, 返回解析后的 Python 对象或 None"""
        messages = [
            {"role": "system", "content": "你只输出JSON，不要任何解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
            if not raw:
                return None
            # 容错：去除 markdown code block
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r'^```\w*\n?', '', raw)
                raw = re.sub(r'\n?```$', '', raw)
            return json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"_chat_json 解析失败: {e}, raw={raw[:200]}")
            return None
