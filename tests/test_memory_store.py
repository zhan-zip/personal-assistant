"""
记忆存储层测试 (SQLite + FTS5)
用 :memory: 数据库, 不落盘, 不污染真实数据
覆盖:
1. 短期会话历史: add_message / get_history / clear_history / load_all_messages
2. 长期记忆 facts: save_facts / get_facts / clear_facts / load_all_facts
3. FTS5 关键词检索 (search_memory, 中文)
4. 旧 JSON 迁移 (migrate_from_json)
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.memory_store import MemoryStore


async def test_messages():
    store = MemoryStore(":memory:")
    await store.init()
    await store.add_message("private_1", "user", "今天天气不错", "t1", "10")
    await store.add_message("private_1", "assistant", "是啊，适合出去玩", "t2", "11")
    await store.add_message("private_2", "user", "现在几点了", "t3", "12")

    hist = await store.get_history("private_1", limit=20)
    assert len(hist) == 2, f"历史条数: {len(hist)}"
    assert hist[0]["role"] == "user" and hist[0]["content"] == "今天天气不错"
    assert hist[1]["content"] == "是啊，适合出去玩"
    assert hist[0]["message_id"] == "10"

    allm = await store.load_all_messages()
    assert set(allm.keys()) == {"private_1", "private_2"}
    assert len(allm["private_1"]) == 2

    await store.clear_history("private_1")
    assert await store.get_history("private_1") == []
    assert await store.get_history("private_2") != []
    await store.close()
    print("Test 1 OK: 会话历史 读写/全量/清空")


async def test_facts():
    store = MemoryStore(":memory:")
    await store.init()
    await store.save_facts("3496326306", ["喜欢熬夜", "是学生"], "now")
    facts = await store.get_facts("3496326306")
    assert facts == ["喜欢熬夜", "是学生"], f"facts: {facts}"
    assert await store.get_facts("none") == []

    allf = await store.load_all_facts()
    assert "3496326306" in allf and allf["3496326306"]["facts"] == ["喜欢熬夜", "是学生"]

    await store.clear_facts("3496326306")
    assert await store.get_facts("3496326306") == []
    await store.close()
    print("Test 2 OK: 长期记忆 facts 读写/全量/清空")


async def test_search():
    store = MemoryStore(":memory:")
    await store.init()
    await store.add_message("private_1", "user", "我下个月要去北京出差", "t1", "1")
    await store.add_message("private_1", "assistant", "北京最近挺冷的", "t2", "2")
    await store.add_message("private_2", "user", "上海天气如何", "t3", "3")

    # 全库搜
    r = await store.search_memory("北京")
    assert r, "FTS/LIKE 应搜到北京相关"
    contents = [x["content"] for x in r]
    assert any("北京" in c for c in contents), f"未命中北京: {contents}"

    # 限定会话
    r2 = await store.search_memory("北京", session_id="private_1")
    assert r2 and all(x["session_id"] == "private_1" for x in r2)
    r3 = await store.search_memory("北京", session_id="private_2")
    assert r3 == [], f"private_2 不应有北京: {r3}"

    # 不存在的词
    r4 = await store.search_memory("不存在的词xyz")
    assert r4 == []
    await store.close()
    print("Test 3 OK: FTS5 关键词检索 (中文 + 会话过滤)")


async def test_migrate():
    store = MemoryStore(":memory:")
    await store.init()
    chat = {
        "private_3496326306": [
            {"role": "user", "content": "你好", "timestamp": "t1", "message_id": 1},
            {"role": "assistant", "content": "你好呀", "timestamp": "t2"},
        ]
    }
    facts = {"3496326306": {"facts": ["喜欢猫"], "updated_at": "now"}}
    n1, n2 = await store.migrate_from_json(chat, facts)
    assert n1 == 2 and n2 == 1, f"迁移数: {n1},{n2}"
    hist = await store.get_history("private_3496326306")
    assert len(hist) == 2 and hist[0]["content"] == "你好"
    assert await store.get_facts("3496326306") == ["喜欢猫"]

    # 已非空时不再迁移
    n3, n4 = await store.migrate_from_json(chat, facts)
    assert n3 == 0 and n4 == 0, "非空库不应重复迁移"
    await store.close()
    print("Test 4 OK: 旧 JSON 迁移")


async def main():
    await test_messages()
    await test_facts()
    await test_search()
    await test_migrate()
    print("\nAll 4 memory store tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
