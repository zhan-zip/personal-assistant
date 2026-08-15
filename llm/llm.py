"""
LLM 客户端 + 高层对话助手

所有调用大模型的入口都收口在本模块:
- LLMClient: 多 provider 路由 + 容灾 (failover)
    - 每个 provider 一个 OpenAI client (deepseek / qwen / ...)
    - 按 task_type 路由到指定 provider + model
    - 主 provider 调用失败时自动切 fallback_chain 里的备用 provider
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
    """多 provider LLM 客户端

    - providers: {"deepseek": OpenAI-client, "qwen": OpenAI-client, ...}
    - config: llm 配置段, 含 providers / routing / fallback_chain
    - routing 决定 task_type → (provider, model_role) 的映射:
        e.g. routing.default = "deepseek.chat"
             routing.reasoner = "deepseek.reasoner"   (预留位, 暂不启用)
             routing.vision = "qwen.vision"
    - fallback_chain: ["deepseek", "qwen"] 主 provider 失败时按序切换
    """

    def __init__(self, providers: Dict[str, OpenAI], config: Dict,
                 temperature: float = 0.6,
                 max_tokens: int = 2048):
        self.providers = providers
        self.config = config or {}
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ── 路由 ──────────────────────────────────────────
    def _resolve(self, task_type: str = "default"):
        """返回 (主 provider, model_role, 容灾 chain)"""
        routing = self.config.get("routing", {}) or {}
        key = routing.get(task_type) or routing.get("default") or "deepseek.chat"
        prov, role = key.split(".", 1)
        chain = routing.get("fallback_chain") or [prov]
        return prov, role, chain

    def _model_name(self, prov: str, role: str) -> str:
        prov_cfg = self.config.get("providers", {}).get(prov, {})
        models = prov_cfg.get("models", {}) or {}
        return models.get(role) or role

    def _iter_providers(self, task_type: str):
        """按优先级 yield (provider_name, client, model_name)"""
        prov, role, chain = self._resolve(task_type)
        seen = set()
        order = [prov] + [c for c in chain if c != prov]
        for name in order:
            if name in seen:
                continue
            seen.add(name)
            client = self.providers.get(name)
            if client is None:
                continue
            yield name, client, self._model_name(name, role)

    # ── 同步调用 (内部) ──────────────────────────────
    def _chat_sync(self, client: OpenAI, model: str, messages: List[Dict],
                   temperature: Optional[float] = None,
                   max_tokens: Optional[int] = None) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            stream=False,
        )
        return resp.choices[0].message.content.strip()

    async def chat(self, messages: List[Dict],
                   temperature: Optional[float] = None,
                   max_tokens: Optional[int] = None,
                   task_type: str = "default") -> str:
        """普通对话, 失败自动切备用 provider, 全部失败返回空串"""
        last_err = None
        for name, client, model in self._iter_providers(task_type):
            try:
                return await asyncio.to_thread(
                    self._chat_sync, client, model, messages, temperature, max_tokens
                )
            except Exception as e:
                last_err = e
                logger.error(f"LLM {name}/{model} 调用失败: {e}")
        logger.error(f"LLM 所有 provider 均失败: {last_err}")
        return ""

    # ── Tool-calling 支持 ────────────────────────────
    def _chat_sync_with_tools(self, client: OpenAI, model: str,
                              messages: List[Dict], tools: List[Dict],
                              temperature: Optional[float] = None,
                              max_tokens: Optional[int] = None):
        """同步调用 LLM (带 tools), 返回完整 message 对象"""
        resp = client.chat.completions.create(
            model=model,
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
                              max_tokens: Optional[int] = None,
                              task_type: str = "default"):
        """带工具对话, 失败自动切备用 provider, 全部失败返回 None"""
        last_err = None
        for name, client, model in self._iter_providers(task_type):
            try:
                return await asyncio.to_thread(
                    self._chat_sync_with_tools, client, model,
                    messages, tools, temperature, max_tokens,
                )
            except Exception as e:
                last_err = e
                logger.error(f"LLM tool-calling {name}/{model} 失败: {e}")
        logger.error(f"LLM tool-calling 所有 provider 均失败: {last_err}")
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
