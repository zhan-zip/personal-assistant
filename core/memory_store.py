"""
记忆存储层 (SQLite + FTS5)

架构: 内存缓存 + SQLite 持久化
- 短期会话历史 messages 表 (替代 chat_memory.json)
- 长期记忆 facts 表 (替代 user_facts.json)
- 结构化: events(事件时间线) / todos(待办, 预留 Phase 4)
- FTS5 全文索引 (trigram tokenizer) 支持中文关键词检索
- 全部异步 (aiosqlite), 不阻塞事件循环

使用: bot 启动时 init → 从 SQLite 载入内存缓存; 写入时同步改内存 + 异步落盘。
"""
import json
import logging
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger("memory_store")

DB_FILE = "memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS facts (
    user_id TEXT PRIMARY KEY,
    facts TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, session_id, role,
    tokenize = 'trigram'
);
"""


class MemoryStore:
    """SQLite 持久化层 (异步)"""

    def __init__(self, db_file: str = DB_FILE):
        self.db_file = db_file
        self._conn: Optional[aiosqlite.Connection] = None

    # ── 生命周期 ──
    async def init(self):
        """打开连接 + 建表"""
        if self._conn is not None and self._conn._connection is not None:
            return
        self._conn = await aiosqlite.connect(self.db_file)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self):
        return self._conn

    # ── 短期会话历史 ──
    async def add_message(self, session_id: str, role: str, content: str,
                          timestamp: str, message_id=None):
        await self.init()
        await self._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, message_id) "
            "VALUES (?,?,?,?,?)",
            (session_id, role, content, timestamp, message_id),
        )
        # 同步写 FTS 索引 (供关键词检索)
        try:
            await self._conn.execute(
                "INSERT INTO messages_fts (content, session_id, role) VALUES (?,?,?)",
                (content, session_id, role),
            )
        except Exception as e:
            logger.warning(f"FTS 索引写入失败: {e}")
        await self._conn.commit()

    async def get_history(self, session_id: str, limit: int = 20) -> List[Dict]:
        await self.init()
        cur = await self._conn.execute(
            "SELECT role, content, timestamp, message_id FROM messages "
            "WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cur.fetchall()
        rows.reverse()  # 恢复时间正序
        out = []
        for role, content, ts, mid in rows:
            m = {"role": role, "content": content, "timestamp": ts}
            if mid:
                m["message_id"] = mid
            out.append(m)
        return out

    async def clear_history(self, session_id: str):
        await self.init()
        await self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM messages_fts WHERE session_id=?", (session_id,))
        await self._conn.commit()

    async def load_all_messages(self) -> Dict[str, List[Dict]]:
        """全量载入会话历史到内存缓存 (启动时用)"""
        await self.init()
        cur = await self._conn.execute(
            "SELECT session_id, role, content, timestamp, message_id FROM messages ORDER BY id"
        )
        rows = await cur.fetchall()
        out: Dict[str, List[Dict]] = {}
        for session_id, role, content, ts, mid in rows:
            m = {"role": role, "content": content, "timestamp": ts}
            if mid:
                m["message_id"] = mid
            out.setdefault(session_id, []).append(m)
        return out

    # ── 长期记忆 facts ──
    async def get_facts(self, user_id) -> List[str]:
        await self.init()
        cur = await self._conn.execute(
            "SELECT facts FROM facts WHERE user_id=?", (str(user_id),)
        )
        row = await cur.fetchone()
        if not row:
            return []
        try:
            return json.loads(row[0])
        except Exception:
            return []

    async def save_facts(self, user_id, fact_list: List[str], updated_at: str):
        await self.init()
        await self._conn.execute(
            "INSERT OR REPLACE INTO facts (user_id, facts, updated_at) VALUES (?,?,?)",
            (str(user_id), json.dumps(fact_list, ensure_ascii=False), updated_at),
        )
        await self._conn.commit()

    async def clear_facts(self, user_id):
        await self.init()
        await self._conn.execute("DELETE FROM facts WHERE user_id=?", (str(user_id),))
        await self._conn.commit()

    async def load_all_facts(self) -> Dict[str, Dict]:
        """全量载入长期记忆到内存缓存 (启动时用)"""
        await self.init()
        cur = await self._conn.execute("SELECT user_id, facts, updated_at FROM facts")
        rows = await cur.fetchall()
        out: Dict[str, Dict] = {}
        for uid, facts_json, updated_at in rows:
            try:
                facts = json.loads(facts_json)
            except Exception:
                facts = []
            out[uid] = {"facts": facts, "updated_at": updated_at}
        return out

    # ── 待办 todos ──
    async def add_todo(self, user_id, content: str, created_at: str) -> Optional[int]:
        """新增一条待办, 返回 id"""
        await self.init()
        cur = await self._conn.execute(
            "INSERT INTO todos (user_id, content, status, created_at) VALUES (?,?,?,?)",
            (str(user_id), content, "pending", created_at),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def list_todos(self, user_id, status: Optional[str] = None) -> List[Dict]:
        """列出某用户待办 (可按状态过滤), 按创建时间倒序"""
        await self.init()
        sql = ("SELECT id, content, status, created_at FROM todos WHERE user_id=?")
        params = [str(user_id)]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC"
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return [{"id": r[0], "content": r[1], "status": r[2], "created_at": r[3]}
                for r in rows]

    async def update_todo(self, todo_id: int, status: str) -> bool:
        """更新待办状态 (pending/done/cancelled), 返回是否命中"""
        await self.init()
        cur = await self._conn.execute(
            "UPDATE todos SET status=? WHERE id=?", (status, todo_id)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    # ── FTS5 关键词检索 ──
    async def search_memory(self, query: str, session_id: Optional[str] = None,
                            limit: int = 8) -> List[Dict]:
        """关键词检索历史消息 (FTS5 trigram + LIKE 兜底), 返回消息列表"""
        await self.init()
        q = (query or "").strip()
        if not q:
            return []

        # FTS5 MATCH (trigram 支持中文子串匹配; 短词可能失败 → LIKE 兜底)
        try:
            sql = ("SELECT content, session_id, role, timestamp FROM messages_fts "
                   "WHERE messages_fts MATCH ?")
            params: list = [q]
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " LIMIT ?"
            params.append(limit)
            cur = await self._conn.execute(sql, params)
            rows = await cur.fetchall()
            if rows:
                return [{"content": r[0], "session_id": r[1], "role": r[2],
                         "timestamp": r[3]} for r in rows]
        except Exception as e:
            logger.warning(f"FTS5 检索失败, 改用 LIKE 兜底: {e}")

        # LIKE 兜底
        sql = ("SELECT content, session_id, role, timestamp FROM messages "
               "WHERE content LIKE ?")
        params = [f"%{q}%"]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return [{"content": r[0], "session_id": r[1], "role": r[2],
                 "timestamp": r[3]} for r in rows]

    # ── 迁移 ──
    async def is_empty(self) -> bool:
        await self.init()
        cur = await self._conn.execute("SELECT COUNT(*) FROM messages")
        row = await cur.fetchone()
        return (row[0] or 0) == 0

    async def migrate_from_json(self, chat_memory: Dict, user_facts: Dict):
        """把旧 JSON 数据迁入 SQLite (仅当 SQLite 为空时调用)"""
        if not await self.is_empty():
            return 0, 0
        count_msgs = 0
        for session_id, msgs in (chat_memory or {}).items():
            for m in msgs or []:
                await self.add_message(
                    session_id,
                    m.get("role", "user"),
                    str(m.get("content", "")),
                    m.get("timestamp", ""),
                    m.get("message_id"),
                )
                count_msgs += 1
        count_facts = 0
        for user_id, info in (user_facts or {}).items():
            facts = info.get("facts", []) if isinstance(info, dict) else []
            if facts:
                await self.save_facts(user_id, facts, info.get("updated_at", ""))
                count_facts += 1
        if count_msgs or count_facts:
            logger.info(f"迁移完成: {count_msgs} 条消息, {count_facts} 个用户事实")
        return count_msgs, count_facts


# 全局单例
_store: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    """获取全局 MemoryStore 单例 (bot 启动时使用)"""
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
