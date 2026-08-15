"""
核心回归测试: 验证协议层分离后 命令清除历史 / 撤回链路 是否正常
- #cls 应清空当前会话短期对话历史 (chat_memory)
- 撤回 notice → handle_recall 应被触发并发送回应
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as bot_module
from core.message_processor import MessageProcessor
from core.commands import CommandHandler
from core.event_handler import EventHandler
from core.progress_reporter import ProgressReporter
from profile.profile import ProfileManager
from protocols.base import Message


class MockStore:
    """假存储层: 测试逻辑用, 不碰真实 SQLite"""
    async def add_message(self, *a, **k):
        pass

    async def clear_history(self, *a, **k):
        pass


def _new_bot():
    b = bot_module.QQBot.__new__(bot_module.QQBot)
    b.config = {
        "message": {"group_mention_only": True, "ignore_self": True},
        "persona": {"file_path": "persona.txt"},
        "search": {"enabled": True, "auto_trigger": True, "trigger_words": ["搜索", "查"]},
        "vision": {"enabled": True},
        "whitelist": {"enabled": False},
        "blacklist": {"enabled": False},
        "llm": {"max_history": 20},
        "recall": {"require_proactive_enabled": False, "cooldown_seconds": 60},
        "proactive": {"enabled": False},
    }
    b.chat_memory = {"private_3496326306": [
        {"role": "user", "content": "旧对话内容"},
        {"role": "assistant", "content": "旧回复"},
    ]}
    b.message_id_buffer = {}
    b.message_recv_time = {}
    b._verified_recall_ids = set()
    b.recall_cooldown = {}
    b._bg_tasks = []
    b.proactive_cache = {}
    b.current_nickname = None
    b.current_signature = None
    b.vision_client = None
    b.persona_cache = None
    b.persona_mtime = 0.0
    b._cleared_facts = []

    class FakeLM:
        def clear(self, uid):
            b._cleared_facts.append(uid)

        def format_for_prompt(self, uid):
            return ""

    b.long_memory = FakeLM()

    b.command_handler = CommandHandler(b)
    b.profile_manager = ProfileManager(b)
    b.progress = ProgressReporter(b)
    b.message_processor = MessageProcessor(b)
    b.event_handler = EventHandler(b)
    b.memory_store = MockStore()   # 假存储, 不落盘

    sent = []

    async def fake_send(channel, user_id, group_id, msg):
        sent.append((channel, user_id, group_id, msg))
        return 0

    async def fake_web_search(q, count=5):
        return None

    async def fake_fetch(u):
        return None

    async def fake_dl(img):
        return None

    async def fake_vision(u):
        return None

    b.send_text = fake_send
    b._web_search = fake_web_search
    b._fetch_url = fake_fetch
    b._download_image_from_qq = fake_dl
    b._call_vision = fake_vision
    b.sent = sent
    return b


async def test_cls_clears_history():
    """#cls 应清空当前会话短期历史并回复"""
    b = _new_bot()
    m = Message(channel="onebot", session_id="onebot:private:3496326306",
                user_id="3496326306", text="#cls", message_id="1")
    await b.message_processor.process(m)
    await asyncio.sleep(0.3)
    assert not b.chat_memory.get("private_3496326306"), \
        f"#cls 未清除历史: {b.chat_memory.get('private_3496326306')}"
    assert any("记忆已清除" in s[3] for s in b.sent), f"#cls 未回复: {b.sent}"
    print("Test 1 OK: #cls 清历史 + 回复")


async def test_clear_history_command():
    """直接测 CommandHandler #cls 分支 (短期 + 长期记忆都要清)"""
    b = _new_bot()
    r = await b.command_handler.handle("3496326306", "#cls", None)
    assert r == "记忆已清除。"
    assert not b.chat_memory.get("private_3496326306")
    assert "3496326306" in b._cleared_facts, "长期记忆也应被清除"
    print("Test 2 OK: CommandHandler #cls 分支 (短期+长期)")


async def test_recall_notice_flow():
    """friend_recall notice → handle_recall 被调用, LLM 决定回复 → 发送"""
    b = _new_bot()
    b.recall_cooldown = {}
    b.message_id_buffer = {"1481862108": "被撤回的原文"}
    b.message_recv_time = {"1481862108": asyncio.get_event_loop().time()}

    # mock LLM: 决定回应
    async def fake_chat(*args, **kwargs):
        return "我看到你撤回了哦~"

    b.llm = type("LLM", (), {"chat": fake_chat})()
    b.long_memory = type("LM", (), {"format_for_prompt": lambda self, uid: ""})()

    async def fake_send_private(user_id, message):
        b.sent.append(("private", user_id, message))
        return 999

    b._send_private_msg = fake_send_private
    b._send_ws_request = fake_send_private  # 未用到

    event = {"post_type": "notice", "notice_type": "friend_recall",
             "user_id": 3496326306, "message_id": 1481862108, "group_id": 0}
    await b.event_handler.handle_event(event)
    await asyncio.sleep(0.5)
    assert any("撤回了" in s[2] or "我看到" in s[2] for s in b.sent), \
        f"撤回未回应: {b.sent}"
    assert "1481862108" in b._verified_recall_ids
    print("Test 3 OK: 撤回 notice → 回应")


async def main():
    await test_cls_clears_history()
    await test_clear_history_command()
    await test_recall_notice_flow()
    print("\nAll 3 core regression tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
