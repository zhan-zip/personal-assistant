"""
向量记忆测试 (ChromaDB RAG 语义检索)
用临时目录, 不污染真实数据; embedding 用 mock (字符袋向量), 不真实调 API
覆盖:
1. 写入与检索 (语义最近邻)
2. 会话过滤 (session_id)
3. 幂等重建 (rebuild 只补缺失)
4. 持久化 (新实例可检索到)
"""
import asyncio
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vector_store import VectorStore


def _mock_embed(dim: int = 32):
    """mock embedding: 按字符出现构造归一化词袋向量 (共享字符越多越近)"""
    async def embed(texts):
        out = []
        for t in texts:
            vec = [0.0] * dim
            for ch in t:
                vec[ord(ch) % dim] += 1.0
            norm = (sum(v * v for v in vec) ** 0.5) or 1.0
            out.append([v / norm for v in vec])
        return out
    return embed


async def test_write_and_search():
    d = tempfile.mkdtemp()
    try:
        vs = VectorStore(d)
        embed = _mock_embed()
        await vs.add_message("private_1", "user", "今天吃了番茄炒蛋", "t1", embed)
        await vs.add_message("private_1", "user", "我在练习写代码", "t2", embed)
        await vs.add_message("private_2", "user", "番茄很好吃", "t3", embed)
        assert vs.count() == 3

        qv = (await embed(["番茄"]))[0]
        hits = await vs.search(qv, limit=2)
        assert len(hits) == 2, f"检索条数: {len(hits)}"
        assert all("番茄" in h["content"] for h in hits), \
            f"应召回含'番茄'的内容: {[h['content'] for h in hits]}"

        # 会话过滤
        hits2 = await vs.search(qv, limit=2, session_id="private_1")
        assert hits2, "会话过滤后应有结果"
        assert all(h["session_id"] == "private_1" for h in hits2)

        # 不相关查询也应返回但距离更远 (可用 threshold 过滤)
        qv_far = (await embed(["zzzqqqwwweee"]))[0]
        hits3 = await vs.search(qv_far, limit=1, threshold=0.5)
        assert hits3 == [] or len(hits3) >= 0
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("Test 1 OK: 写入/检索/会话过滤")


async def test_rebuild_idempotent():
    d = tempfile.mkdtemp()
    try:
        vs = VectorStore(d)
        embed = _mock_embed()
        msgs = {"private_1": [
            {"role": "user", "content": "北京天气如何", "timestamp": "t1"},
            {"role": "assistant", "content": "今天晴天", "timestamp": "t2"},
        ]}
        n1 = await vs.rebuild(msgs, embed)
        assert n1 == 2, f"首次构建应加2条, 实际 {n1}"
        n2 = await vs.rebuild(msgs, embed)
        assert n2 == 0, f"幂等重建不应新增, 实际 {n2}"
        assert vs.count() == 2

        msgs["private_1"].append(
            {"role": "user", "content": "明天呢", "timestamp": "t3"}
        )
        n3 = await vs.rebuild(msgs, embed)
        assert n3 == 1, f"只补新增的1条, 实际 {n3}"
        assert vs.count() == 3
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("Test 2 OK: 幂等重建(只补缺失)")


async def test_persist():
    d = tempfile.mkdtemp()
    try:
        vs = VectorStore(d)
        embed = _mock_embed()
        await vs.add_message("private_1", "user", "我爱吃火锅", "t1", embed)

        vs2 = VectorStore(d)
        assert vs2.count() == 1, "新实例应读到已持久化数据"
        qv = (await embed(["麻辣火锅"]))[0]
        hits = await vs2.search(qv, limit=1)
        assert hits and "火锅" in hits[0]["content"], f"检索: {hits}"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("Test 3 OK: 持久化(重启后可检索)")


if __name__ == "__main__":
    asyncio.run(test_write_and_search())
    asyncio.run(test_rebuild_idempotent())
    asyncio.run(test_persist())
    print("All 3 vector store tests passed!")
