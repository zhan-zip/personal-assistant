"""
长期记忆模块:
- 每个用户一份 facts (性格, 偏好, 关系, 重要事件...)
- 永久存盘 user_facts.json
- 每次对话时把 facts 拼到 system prompt 里
"""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from core.utils import BEIJING_TZ, now_iso

logger = logging.getLogger("long_memory")

USER_FACTS_FILE = Path("user_facts.json")

# facts 提取 prompt: 让 LLM 从一条对话里抽出关键事实
# 注意: 所有 { / } 在 .format() 调用时必须转义成 {{ / }}, 否则字面量 JSON 示例会被当成命名占位符
_EXTRACT_PROMPT = """从下面这段对话里提取关于用户(说话者)的关键事实, 用于长期记忆。
只记录有价值的事实, 例如:
- 身份/职业: 程序员, 学生, 老师
- 关系: 你的开发者, 老朋友, 第一次聊
- 偏好/习惯: 喜欢深夜聊天, 关注二次元
- 重要事件: 即将考试, 失恋, 养了只猫
- 性格特征: 内向, 毒舌, 容易焦虑

不要记录:
- 临时性的当下问题(例如"今天天气怎么样")
- 对方重复发送这种无意义的事实
- 任何本不应该长期记住的细节

输出 JSON 格式: {{"facts": ["事实1", "事实2"]}} 或 {{"facts": []}} 表示没有值得记的
如果对话里没有任何有效事实, 直接输出 {{"facts": []}}

对话:
{transcript}
"""


class LongMemory:
    """per-user 长期记忆管理"""

    def __init__(self, file_path: Path = USER_FACTS_FILE):
        self.file_path = file_path
        self._cache: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if self.file_path.exists():
            try:
                self._cache = json.loads(self.file_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"加载 {self.file_path} 失败: {e}")
                self._cache = {}
        else:
            self._cache = {}

    def _save(self):
        self.file_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_facts(self, user_id: int) -> List[str]:
        """取某用户的 facts 列表"""
        key = str(user_id)
        info = self._cache.get(key, {})
        return info.get("facts", [])

    def format_for_prompt(self, user_id: int) -> str:
        """格式化成可拼进 system prompt 的字符串"""
        facts = self.get_facts(user_id)
        if not facts:
            return ""
        lines = "\n".join(f"- {f}" for f in facts)
        return f"\n【长期记忆】\n关于这个用户你已知:\n{lines}\n"

    def add_facts(self, user_id: int, new_facts: List[str], dedup: bool = True):
        """添加事实 (可选去重)"""
        if not new_facts:
            return
        key = str(user_id)
        info = self._cache.setdefault(key, {"facts": [], "updated_at": ""})
        existing = set(info.get("facts", [])) if dedup else set()
        for f in new_facts:
            f = f.strip()
            if not f:
                continue
            if dedup and f in existing:
                continue
            # 简单去重: 子串/包含关系
            if dedup and any(
                f in e or e in f for e in existing
            ):
                continue
            info["facts"].append(f)
            existing.add(f)
        info["updated_at"] = now_iso()
        self._save()

    def clear(self, user_id: int):
        key = str(user_id)
        if key in self._cache:
            del self._cache[key]
            self._save()

    async def extract_and_store(self, user_id: int, transcript: str, llm_client) -> int:
        """调用 LLM 从一段对话中抽取 facts, 存盘

        返回: 实际新增的 fact 数
        """
        if not transcript or len(transcript) < 20:
            return 0
        prompt = _EXTRACT_PROMPT.format(transcript=transcript[:1500])
        try:
            import asyncio
            content = await llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是事实抽取助手, 只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=200,
            )
            m = re.search(r'\{[\s\S]*\}', content)
            if not m:
                return 0
            data = json.loads(m.group())
            new_facts = data.get("facts", [])
            if not isinstance(new_facts, list):
                return 0
            before = len(self.get_facts(user_id))
            self.add_facts(user_id, [str(f) for f in new_facts])
            after = len(self.get_facts(user_id))
            added = after - before
            if added:
                logger.info(
                    f"[LONG_MEM] user={user_id} 新增 {added} 条事实: "
                    f"{new_facts[:3]}"
                )
            return added
        except Exception as e:
            logger.error(f"extract_and_store 失败: {e}")
            return 0
