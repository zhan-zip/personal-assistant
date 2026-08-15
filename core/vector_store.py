"""
向量记忆存储 — ChromaDB 封装 (语义检索 RAG)

与 core/memory_store.py (SQLite + FTS5 关键词检索) 互补:
- 每条消息 embedding 后 upsert 进 ChromaDB, 幂等去重
- 语义检索 query 的最近邻
- embedding 由外部注入 (llm/llm.py 的 embed_texts, DashScope text-embedding-v3 1024 维)

参考 Food-Time C3 修复: 构建必须幂等 (已存在跳过), 避免重复 delete/add 损坏持久化文件。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger("vector_store")


def _stable_id(session_id: str, role: str, content: str, timestamp: str) -> str:
    """稳定 ID: 同一会话同一内容只存一条 (去重键)"""
    digest = hashlib.md5(
        f"{role}|{content}|{timestamp}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{session_id}:{digest}"


class VectorStore:
    """ChromaDB 向量存储, 提供消息级语义检索

    用法:
        vs = VectorStore(persist_dir)
        await vs.rebuild(all_messages, embed_func)              # 启动时全量(幂等)
        await vs.add_message(session_id, role, content, ts, embed_func)  # 增量
        hits = await vs.search(query_vec, limit=5)
    """

    def __init__(self, persist_dir: str = "vector_db"):
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._col: Any = None

    @property
    def col(self) -> Any:
        if self._col is None:
            self._col = self._client.get_or_create_collection(name="messages")
        return self._col

    def count(self) -> int:
        try:
            return self.col.count()
        except Exception as e:
            logger.warning(f"[VEC] count 失败: {e}")
            return 0

    def _existing_ids(self) -> set:
        try:
            return set(self.col.get()["ids"])
        except Exception as e:
            logger.warning(f"[VEC] 获取已有 ids 失败: {e}")
            return set()

    async def rebuild(self, all_messages: Dict[str, List[Dict]],
                      embed_func) -> int:
        """幂等重建: 只补缺少的消息, 返回新增条数"""
        existing = self._existing_ids()
        todo: List[Dict] = []
        for session_id, msgs in all_messages.items():
            for m in msgs:
                mid = _stable_id(
                    session_id, m.get("role", ""),
                    m.get("content", ""), m.get("timestamp", ""),
                )
                if mid not in existing:
                    item = dict(m)
                    item["_sid"] = session_id
                    item["_id"] = mid
                    todo.append(item)
        if not todo:
            logger.info(f"[VEC] 向量库已是最新 ({len(existing)} 条)")
            return 0
        vecs = await embed_func([t["content"] for t in todo])
        if not vecs:
            logger.warning("[VEC] embedding 失败, 跳过向量构建")
            return 0
        await self._add(todo, vecs)
        logger.info(f"[VEC] 向量库增量构建 +{len(todo)} 条 (共 {self.count()})")
        return len(todo)

    async def add_message(self, session_id: str, role: str, content: str,
                          timestamp: str, embed_func) -> bool:
        """单条写入 (幂等: 已存在跳过)"""
        if not (content or "").strip():
            return False
        mid = _stable_id(session_id, role, content, timestamp)
        try:
            if mid in self._existing_ids():
                return False
        except Exception:
            pass
        vecs = await embed_func([content])
        if not vecs:
            return False
        try:
            await self._add(
                [{"_sid": session_id, "_id": mid, "role": role,
                  "content": content, "timestamp": timestamp}],
                vecs,
            )
            return True
        except Exception as e:
            logger.warning(f"[VEC] add_message 失败: {e}")
            return False

    async def _add(self, items: List[Dict], vecs: List[List[float]]) -> None:
        """批量写入 (embedding 已由调用方算好)"""
        await asyncio.to_thread(
            self.col.upsert,
            ids=[it["_id"] for it in items],
            documents=[it["content"] for it in items],
            embeddings=vecs,
            metadatas=[{
                "session_id": it["_sid"],
                "role": it.get("role", ""),
                "timestamp": it.get("timestamp", ""),
            } for it in items],
        )

    async def search(self, query_vec: List[float], limit: int = 5,
                     session_id: Optional[str] = None,
                     threshold: Optional[float] = None) -> List[Dict]:
        """语义检索, 返回 [{session_id, role, content, timestamp, distance}] 升序"""
        try:
            where = {"session_id": session_id} if session_id else None
            res = await asyncio.to_thread(
                lambda: self.col.query(
                    query_embeddings=[query_vec],
                    n_results=limit,
                    where=where,
                )
            )
        except Exception as e:
            logger.warning(f"[VEC] 检索失败: {e}")
            return []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        out: List[Dict] = []
        for i in range(len(ids)):
            d = float(dists[i]) if i < len(dists) else 1e9
            if threshold is not None and d > threshold:
                continue
            m = metas[i] if i < len(metas) and metas[i] else {}
            out.append({
                "session_id": m.get("session_id", ""),
                "role": m.get("role", ""),
                "content": docs[i] if i < len(docs) else "",
                "timestamp": m.get("timestamp", ""),
                "distance": d,
            })
        return out
