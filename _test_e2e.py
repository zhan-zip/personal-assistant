"""端到端流程测试: 模拟 NapCat 推送的消息事件, 验证全链路不崩

每个测试用例使用独立 user_id, 避免与真实数据污染
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# 测试专用: 隔离 chat_memory 读写
TEST_DIR = os.path.dirname(__file__)

from bot import QQBot


class MockWebSocket:
    """模拟 WebSocket, 只记录 send 出去的消息, 不真正连接"""
    def __init__(self):
        self.sent = []
        self._closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self._closed = True

    def __aiter__(self):
        return self
        yield  # 永远不抛消息


def _make_bot() -> QQBot:
    """创建测试 bot, 替换 LLM + WS 为 mock"""
    b = QQBot()
    b.websocket = MockWebSocket()
    b.running = True

    # 让 _send_ws_request 立即返回, 不等 future
    async def fake_send_ws_request(action, params=None, timeout=30):
        return {"status": "ok", "data": {"message_id": 999999999}}

    b._send_ws_request = fake_send_ws_request
    return b


def _patch_llm(b: QQBot, chat_return: str = "ok", persona_return: str = "ok-persona"):
    async def fake_chat(messages, temperature=None, max_tokens=None):
        return chat_return

    async def fake_chat_with_persona(persona, system_extra, user_content,
                                      temperature=None, max_tokens=None):
        return persona_return

    b.llm.chat = fake_chat
    b.llm.chat_with_persona = fake_chat_with_persona


# 使用唯一的 user_id 防止与历史数据冲突
UID = 99001
GID = 88888


async def test_end_to_end_message_flow():
    """模拟一个完整的私聊消息从接收 → 增强 → LLM → 发送的过程"""
    b = _make_bot()
    _patch_llm(b, "你好，我收到了。", "我主动发起的。")

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": UID,
        "group_id": None,
        "message_id": 100001,
        "raw_message": "hello",
        "message": "hello",
        "sender": {"user_id": UID},
        "self_id": 2259016269,
    }

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.5)

    key = b._get_cache_key(UID, None)
    msgs = b.chat_memory.get(key, [])
    assert len(msgs) >= 2, f"历史应至少 2 条 (user+assistant), 实际 {len(msgs)}"
    assert msgs[-2]["role"] == "user" and msgs[-2]["content"] == "hello"
    assert msgs[-1]["role"] == "assistant" and "你好" in msgs[-1]["content"]
    print("Test E2E-1 OK: 私聊消息 → 历史 → LLM → 写入历史")

    assert 100001 in b.message_id_buffer
    print("Test E2E-2 OK: message_id_buffer 已记录")

    assert str(UID) in b.proactive_cache
    print("Test E2E-3 OK: 新好友自动加入 proactive 缓存")


async def test_end_to_end_dedup():
    """同一 message_id 来两次, 第二次应该被去重"""
    b = _make_bot()
    _patch_llm(b, "ok")
    uid = 99002

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": uid,
        "group_id": None,
        "message_id": 200002,
        "raw_message": "world",
        "message": "world",
        "sender": {"user_id": uid},
        "self_id": 2259016269,
    }

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)

    key = b._get_cache_key(uid, None)
    before = len(b.chat_memory.get(key, []))

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)
    after = len(b.chat_memory.get(key, []))

    assert before == after, f"重复消息造成了新增: {before} -> {after}"
    print("Test E2E-4 OK: 同一 message_id 第二次被去重")


async def test_end_to_end_command():
    """测试 # 帮助命令"""
    b = _make_bot()
    _patch_llm(b, "LLM 兜底回复")
    uid = 99003

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": uid,
        "group_id": None,
        "message_id": 300003,
        "raw_message": "#帮助",
        "message": "#帮助",
        "sender": {"user_id": uid},
        "self_id": 2259016269,
    }

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)

    key = b._get_cache_key(uid, None)
    history = b.chat_memory.get(key, [])
    for m in history:
        assert "LLM 兜底" not in m["content"], "命令被错误地走到了 LLM"
    print("Test E2E-5 OK: #帮助 命令没有走 LLM")


async def test_end_to_end_group_at_only():
    """群聊只响应 @机器人"""
    b = _make_bot()
    _patch_llm(b, "不应走到 LLM")
    uid = 99004

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": uid,
        "group_id": GID,
        "message_id": 400004,
        "raw_message": "普通聊天",
        "message": "普通聊天",
        "sender": {"user_id": uid},
        "self_id": 2259016269,
    }

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)

    key = b._get_cache_key(uid, GID)
    history = b.chat_memory.get(key, [])
    for m in history:
        assert "不应走到" not in m["content"], "群聊无 @ 时不应调 LLM"
    print("Test E2E-6 OK: 群聊无 @ 时不调 LLM")


async def test_end_to_end_group_with_at():
    """群聊 @机器人 正常走 LLM"""
    b = _make_bot()
    _patch_llm(b, "我在。")
    uid = 99005

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": uid,
        "group_id": GID,
        "message_id": 400005,
        "raw_message": "[CQ:at,qq=2259016269] 你好",
        "message": [{"type": "at", "data": {"qq": "2259016269"}},
                    {"type": "text", "data": {"text": " 你好"}}],
        "sender": {"user_id": uid},
        "self_id": 2259016269,
    }

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)

    key = b._get_cache_key(uid, GID)
    history = b.chat_memory.get(key, [])
    assert any("我在" in m["content"] for m in history), "群聊 @ 应正常调 LLM"
    print("Test E2E-7 OK: 群聊 @机器人 正常走 LLM")


async def test_end_to_end_message_sent_ignored():
    """自己发的消息应该被忽略 (post_type=message_sent)"""
    b = _make_bot()
    uid = 99006

    event = {
        "post_type": "message_sent",
        "message_type": "private",
        "user_id": uid,
        "message_id": 500005,
        "raw_message": "bot 自己发的",
        "message": "bot 自己发的",
    }
    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)
    key = b._get_cache_key(uid, None)
    history = [m for m in b.chat_memory.get(key, []) if m["content"] == "bot 自己发的"]
    assert len(history) == 0
    print("Test E2E-8 OK: message_sent 被忽略")


async def test_end_to_end_recv_stats():
    """验证接收层统计能正确识别"被推两遍"的情况"""
    b = _make_bot()
    _patch_llm(b, "ok")
    uid = 99007
    mid = 600001

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": uid,
        "group_id": None,
        "message_id": mid,
        "raw_message": "测试一遍",
        "message": "测试一遍",
        "sender": {"user_id": uid},
        "self_id": 2259016269,
    }

    # 推一次
    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.2)

    stats = b.event_handler.get_recv_stats()
    assert stats["total_received"] == 1, f"应收到 1 次, 实际 {stats['total_received']}"
    assert mid not in stats["double_pushed_message_ids"], "不应被标记为多次推送"

    # 再推一次 (模拟 NapCat 推两遍)
    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.2)

    stats = b.event_handler.get_recv_stats()
    assert stats["total_received"] == 2, f"应收到 2 次, 实际 {stats['total_received']}"
    assert mid in stats["double_pushed_message_ids"], "同一 message_id 出现 2 次, 应被标记"
    assert stats["double_pushed_message_ids"][mid] == 2
    assert stats["filtered"]["by_dedup_id"] >= 1, "第二次应被 ID 去重"
    print("Test E2E-9 OK: 接收层统计正确识别双重推送")


async def test_end_to_end_contaminated_user_defense():
    """验证 message_processor 拒绝写入被污染的 user 消息"""
    b = _make_bot()
    _patch_llm(b, "ok")
    uid = 99008

    # 构造一个已经被污染的 'message' (模拟脏数据流)
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": uid,
        "group_id": None,
        "message_id": 700007,
        "raw_message": "[联网搜索结果]:\n1. 今天是7月17日",
        "message": "[联网搜索结果]:\n1. 今天是7月17日",
        "sender": {"user_id": uid},
        "self_id": 2259016269,
    }

    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.3)

    key = b._get_cache_key(uid, None)
    history = b.chat_memory.get(key, [])
    for m in history:
        # user 角色但内容是系统注入, 应被过滤替换为 [已过滤的系统注入消息]
        if m["role"] == "user":
            assert "[联网搜索结果]" not in m["content"], \
                f"污染的 user 消息被写入历史: {m['content'][:60]!r}"
    print("Test E2E-10 OK: message_processor 拒绝污染的 user 消息")


async def test_end_to_end_status_command():
    """验证 #状态 指令能返回接收层诊断"""
    b = _make_bot()
    _patch_llm(b, "ok")
    out = b.command_handler._stats_text()
    assert "接收层诊断" in out
    assert "累计接收" in out
    print("Test E2E-11 OK: #状态 指令返回诊断")


if __name__ == "__main__":
    tests = [
        test_end_to_end_message_flow,
        test_end_to_end_dedup,
        test_end_to_end_command,
        test_end_to_end_group_at_only,
        test_end_to_end_group_with_at,
        test_end_to_end_message_sent_ignored,
        test_end_to_end_recv_stats,
        test_end_to_end_contaminated_user_defense,
        test_end_to_end_status_command,
    ]
    for t in tests:
        asyncio.run(t())
    print()
    print("=" * 50)
    print(f"All {len(tests)} E2E tests passed!")
