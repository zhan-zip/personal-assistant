"""
协议层 mock 测试：不依赖真实 NapCat / QQ，验证 OneBotAdapter 与 bot.py 转发逻辑。

覆盖：
1. adapter 请求-响应 (api_call) —— 通过本地假 NapCat 服务器
2. adapter 事件转发 (on_event 回调)
3. adapter 发送私聊/群聊 (send_private / send_group)
4. 发送节流（同一用户相同内容 1.5 秒内只发一次）
5. bot.py 转发方法 (_send_ws_request / _send_private_msg / _send_group_msg)
6. bot._on_adapter_event / _on_adapter_ready
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from protocols.onebot import OneBotAdapter
import bot as bot_module


class FakeNapCatServer:
    """本地假 NapCat: 收请求回响应, 可主动推事件"""

    def __init__(self):
        self.received_requests = []
        self.responses = {}          # action → data
        self._server = None
        self._ws = None
        self.port = None

    async def start(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _handler(self, ws):
        self._ws = ws
        async for raw in ws:
            req = json.loads(raw)
            self.received_requests.append(req)
            data = self.responses.get(req.get("action"), {"ok": True})
            await ws.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "echo": req.get("echo"),
                "data": data,
            }))

    async def push_event(self, event):
        await self._ws.send(json.dumps(event))

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


class StubBot:
    """adapter 需要的 bot 最小外壳"""

    def __init__(self, ws_url):
        self.config = {"self_id": None, "napcat": {"ws_url": ws_url}}


def _new_bot():
    """构造一个未初始化的 QQBot 对象 (绕过 __init__, 手动设属性)"""
    return bot_module.QQBot.__new__(bot_module.QQBot)


def _adapter(port):
    url = f"ws://127.0.0.1:{port}"
    return OneBotAdapter(StubBot(url), ws_url=url)


async def _connect(adapter, on_event=None):
    """起一个 connect task 并等连接建立"""
    task = asyncio.create_task(adapter.connect(on_event=on_event))
    await asyncio.sleep(0.3)
    return task


async def test_api_call():
    """请求-响应: bot 发请求带 echo, 假 NapCat 返回 data"""
    server = await FakeNapCatServer().start()
    server.responses["get_stranger_info"] = {"user_id": 1, "nickname": "test"}
    adapter = _adapter(server.port)
    conn = await _connect(adapter)
    try:
        result = await adapter.api_call("get_stranger_info", {"user_id": 1})
        assert result == {"user_id": 1, "nickname": "test"}, f"结果错误: {result}"
        req = server.received_requests[0]
        assert req["action"] == "get_stranger_info"
        assert req["params"] == {"user_id": 1}
        assert req.get("echo") is not None, "请求应带 echo"
        print("Test 1 OK: api_call 请求-响应")
    finally:
        conn.cancel()
        await server.stop()


async def test_event_forward():
    """假 NapCat 推事件 → on_event 回调收到"""
    server = await FakeNapCatServer().start()
    adapter = _adapter(server.port)
    events = []

    async def on_event(e):
        events.append(e)

    conn = await _connect(adapter, on_event)
    try:
        await server.push_event({
            "post_type": "message", "message_id": 777,
            "user_id": 1, "message_type": "private",
            "message": "hi", "raw_message": "hi",
        })
        await asyncio.sleep(0.3)
        assert len(events) == 1, f"事件未转发: {len(events)}"
        assert events[0]["message_id"] == 777
        print("Test 2 OK: 事件转发 on_event")
    finally:
        conn.cancel()
        await server.stop()


async def test_send():
    """发送私聊/群聊 → 假 NapCat 收到对应动作请求"""
    server = await FakeNapCatServer().start()
    server.responses["send_private_msg"] = {"message_id": 99}
    adapter = _adapter(server.port)
    conn = await _connect(adapter)
    try:
        mid = await adapter.send_private(12345, "hello")
        assert mid == 99, f"私聊 message_id 错误: {mid}"
        assert server.received_requests[-1]["action"] == "send_private_msg"

        await adapter.send_group(54321, "hello group")
        assert server.received_requests[-1]["action"] == "send_group_msg"
        assert server.received_requests[-1]["params"]["group_id"] == 54321
        print("Test 3 OK: send_private / send_group")
    finally:
        conn.cancel()
        await server.stop()


async def test_throttle():
    """同一用户相同内容 1.5s 内只发一次（第二次被节流拦截）"""
    server = await FakeNapCatServer().start()
    adapter = _adapter(server.port)
    conn = await _connect(adapter)
    try:
        await adapter.send_private(100, "same")
        await adapter.send_private(100, "same")   # 紧接再发
        count = sum(1 for r in server.received_requests
                    if r["action"] == "send_private_msg")
        assert count == 1, f"节流未生效: 发送了 {count} 次"
        print("Test 4 OK: 发送节流")
    finally:
        conn.cancel()
        await server.stop()


async def test_bot_forward():
    """bot 转发方法应调用 adapter 的对应能力"""
    class FakeAdapter:
        def __init__(self):
            self.calls = []

        async def api_call(self, action, params=None, timeout=30):
            self.calls.append(("api_call", action, params))
            return {"message_id": 1}

        async def send_private(self, user_id, message):
            self.calls.append(("send_private", user_id, message))
            return 42

        async def send_group(self, group_id, message):
            self.calls.append(("send_group", group_id, message))
            return 43

    b = _new_bot()
    b.adapter = FakeAdapter()

    r1 = await b._send_ws_request("get_msg", {"message_id": 3})
    assert b.adapter.calls[-1] == ("api_call", "get_msg", {"message_id": 3})
    assert r1 == {"message_id": 1}

    r2 = await b._send_private_msg(100, "hi")
    assert b.adapter.calls[-1] == ("send_private", 100, "hi")
    assert r2 == 42

    r3 = await b._send_group_msg(200, "hi")
    assert b.adapter.calls[-1] == ("send_group", 200, "hi")
    assert r3 == 43
    print("Test 5 OK: bot 转发方法")


async def test_bot_event_callbacks():
    """_on_adapter_event 转发给 event_handler; _on_adapter_ready 初始化核心"""
    b = _new_bot()

    # _on_adapter_event
    handled = []

    class FakeEH:
        async def handle_event(self, event):
            handled.append(event)

    b.event_handler = FakeEH()
    await b._on_adapter_event({"post_type": "message", "message_id": 5})
    await asyncio.sleep(0.1)
    assert handled and handled[0]["message_id"] == 5, "事件未转到 event_handler"

    # _on_adapter_ready
    b._retry_count = 5
    b.running = False
    calls = []

    async def fake_init():
        calls.append("init_profile")

    async def fake_start_bg():
        calls.append("start_bg")

    b._init_profile_cache = fake_init
    b._start_background_tasks = fake_start_bg

    class FakeProgress:
        async def init_pool(self):
            calls.append("init_pool")

    b.progress = FakeProgress()

    await b._on_adapter_ready()
    await asyncio.sleep(0.1)   # 等 init_pool 后台任务跑完
    assert b.running is True, "应置 running=True"
    assert b._retry_count == 0, "应重置重试计数"
    assert calls[0] == "init_profile"
    assert "init_pool" in calls
    assert "start_bg" in calls
    print("Test 6 OK: _on_adapter_event / _on_adapter_ready")


async def main():
    await test_api_call()
    await test_event_forward()
    await test_send()
    await test_throttle()
    await test_bot_forward()
    await test_bot_event_callbacks()
    print("\nAll 6 protocol mock tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
