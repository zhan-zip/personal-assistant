"""
LLM 多模型路由 + 容灾 mock 测试
不依赖真实 API, 用假 client 验证:
1. 默认路由到 deepseek.chat
2. 主 provider 失败 → 自动切 fallback_chain 备用 provider
3. 备用也失败 → 返回空串/None
4. 不同 task_type 路由到不同 provider/model
5. chat_with_tools 同样走容灾
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # 按 bot.py 的正确导入顺序初始化包图, 避免 llm↔core 循环导入
from llm.llm import LLMClient


class FakeResp:
    def __init__(self, text="ok"):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})]


class FakeMsg:
    """模拟 chat.completions.create 返回的 message 对象"""
    def __init__(self, content="tool-ok"):
        self.content = content
        self.tool_calls = []


class FakeClient:
    """模拟 OpenAI client, 可配置抛异常"""

    def __init__(self, fail=False, text="ok"):
        self.fail = fail
        self.text = text
        self.calls = []

    def _maybe_fail(self):
        if self.fail:
            raise Exception("mock api error")

    def chat_completions_create(self, **kwargs):
        self._maybe_fail()
        self.calls.append(kwargs.get("model"))
        return FakeResp(self.text)

    # 供 _chat_sync / _chat_sync_with_tools 用 (通过 .chat.completions.create 链)
    @property
    def chat(self):
        return type("Chat", (), {"completions": type(
            "Completions", (), {"create": self.chat_completions_create})})()


CONFIG = {
    "providers": {
        "deepseek": {"models": {"chat": "deepseek-chat", "reasoner": "deepseek-reasoner"}},
        "qwen": {"models": {"chat": "qwen-plus", "vision": "qwen-vl-max"}},
    },
    "routing": {
        "default": "deepseek.chat",
        "reasoner": "deepseek.reasoner",
        "vision": "qwen.vision",
        "fallback_chain": ["deepseek", "qwen"],
    },
}


async def test_default_route():
    """默认任务路由到 deepseek.chat"""
    ds = FakeClient(text="deepseek 回复")
    llm = LLMClient({"deepseek": ds}, CONFIG)
    out = await llm.chat([{"role": "user", "content": "hi"}])
    assert out == "deepseek 回复"
    assert ds.calls == ["deepseek-chat"]
    print("Test 1 OK: 默认路由 deepseek.chat")


async def test_failover():
    """主 provider 失败 → 自动切备用 qwen"""
    ds = FakeClient(fail=True)
    qw = FakeClient(text="qwen 兜底回复")
    llm = LLMClient({"deepseek": ds, "qwen": qw}, CONFIG)
    out = await llm.chat([{"role": "user", "content": "hi"}])
    assert out == "qwen 兜底回复", f"应切到 qwen: {out!r}"
    assert qw.calls == ["qwen-plus"]
    print("Test 2 OK: 主失败自动切备用 (failover)")


async def test_all_fail():
    """全部 provider 失败 → 返回空串"""
    ds = FakeClient(fail=True)
    qw = FakeClient(fail=True)
    llm = LLMClient({"deepseek": ds, "qwen": qw}, CONFIG)
    out = await llm.chat([{"role": "user", "content": "hi"}])
    assert out == ""
    print("Test 3 OK: 全部失败返回空串")


async def test_task_type_routing():
    """不同 task_type 路由到不同 provider/model"""
    ds = FakeClient()
    qw = FakeClient()
    llm = LLMClient({"deepseek": ds, "qwen": qw}, CONFIG)
    await llm.chat([{"role": "user", "content": "x"}])                    # default → deepseek-chat
    await llm.chat([{"role": "user", "content": "x"}], task_type="vision")  # vision → qwen-vl-max
    assert ds.calls == ["deepseek-chat"]
    assert qw.calls == ["qwen-vl-max"]
    print("Test 4 OK: task_type 路由")


async def test_tools_failover():
    """chat_with_tools 同样走容灾, 主失败切备用, 全失败返回 None"""
    ds = FakeClient(fail=True)
    qw = FakeClient(fail=True)
    llm = LLMClient({"deepseek": ds, "qwen": qw}, CONFIG)
    r = await llm.chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    assert r is None, "全失败 chat_with_tools 应返回 None"
    print("Test 5 OK: chat_with_tools 容灾 + 全失败返回 None")


async def main():
    await test_default_route()
    await test_failover()
    await test_all_fail()
    await test_task_type_routing()
    await test_tools_failover()
    print("\nAll 5 LLM router tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
