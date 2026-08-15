"""
自制网页协议适配器
- aiohttp Web 服务: 提供聊天页面 + WebSocket 端点
- 网页用户连上 WebSocket 发文本 → 转统一 Message → 交给核心
- 核心回复经 send_private(user_id) 路由回对应的 WebSocket 连接
"""
import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

from aiohttp import web

from protocols.base import BaseAdapter, Message

logger = logging.getLogger("protocols.web")

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>AI 助手</title>
<style>
body{font-family:sans-serif;max-width:700px;margin:40px auto;padding:0 16px;background:#f7f7f8}
h2{color:#333}
#log{border:1px solid #ddd;background:#fff;height:420px;overflow-y:auto;padding:12px;margin-bottom:12px;border-radius:8px}
.msg{margin:6px 0;padding:6px 10px;border-radius:8px;max-width:85%}
.you{margin-left:auto;background:#e3f2fd}
.bot{background:#f1f1f1}
.bot .who{color:#0a6;font-weight:bold;margin-right:6px}
.you .who{color:#1565c0;font-weight:bold;margin-right:6px}
#row{display:flex;gap:8px}
#box{flex:1;padding:10px;border:1px solid #ddd;border-radius:8px}
button{padding:10px 18px;border:none;border-radius:8px;background:#0a6;color:#fff;cursor:pointer}
</style>
</head>
<body>
<h2>AI 助手</h2>
<div id="log"></div>
<div id="row">
  <input id="box" placeholder="说点什么...">
  <button id="send">发送</button>
</div>
<script>
const log=document.getElementById('log'),box=document.getElementById('box');
const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
ws.onopen=()=>add('sys','已连接');
ws.onmessage=e=>{const d=JSON.parse(e.data);if(d.text)add('bot',d.text)};
ws.onclose=()=>add('sys','连接已断开');
function add(who,t){
  const d=document.createElement('div');
  d.className='msg '+who;
  d.innerHTML='<span class="who">'+(who==='you'?'你':'AI')+'</span>'+t;
  log.appendChild(d);log.scrollTop=log.scrollHeight;
}
function send(){
  const t=box.value.trim();if(!t)return;
  add('you',t);ws.send(JSON.stringify({text:t}));box.value='';
}
document.getElementById('send').onclick=send;
box.addEventListener('keydown',e=>{if(e.key==='Enter')send()});
</script>
</body>
</html>"""


class WebAdapter(BaseAdapter):
    """自制网页聊天界面适配器"""

    channel = "web"

    def __init__(self, bot: Any, host: str = "127.0.0.1", port: int = 8080):
        super().__init__(bot)
        self.host = host
        self.port = port
        self._runner: Optional[web.AppRunner] = None
        self._closed = False
        # user_id → WebSocket 连接（路由核心回复用）
        self._connections: Dict[str, web.WebSocketResponse] = {}

    def _make_message(self, user_id: str, text: str) -> Message:
        return Message(
            channel="web",
            session_id=Message.make_session_id("web", user_id),
            user_id=user_id,
            text=text,
            message_id=f"web-{uuid.uuid4().hex[:12]}",
            raw={},
        )

    async def _index(self, request):
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        user_id = f"web-{uuid.uuid4().hex[:10]}"
        self._connections[user_id] = ws
        logger.info(f"[web] 用户 {user_id} 上线")
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        text = str(data.get("text", "")).strip()
                    except Exception:
                        text = str(msg.data).strip()
                    if text and self._message_callback:
                        m = self._make_message(user_id, text)
                        await self._message_callback(m)
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            self._connections.pop(user_id, None)
            logger.info(f"[web] 用户 {user_id} 下线")
        return ws

    async def connect(self, on_event=None, on_message=None, on_ready=None) -> bool:
        self._event_callback = on_event
        self._message_callback = on_message
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._runner = runner
        self._closed = False
        try:
            # port=0 时端口是随机分配的，回填实际端口
            self.port = site._server.sockets[0].getsockname()[1]
        except Exception:
            pass
        logger.info(f"[web] 网页服务已启动: http://{self.host}:{self.port}")
        if on_ready:
            await on_ready()
        # 保持服务运行，直到 close()
        while not self._closed:
            await asyncio.sleep(3600)
        return True

    async def close(self) -> None:
        self._closed = True
        for ws in list(self._connections.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    # ── 发送 ──
    async def send_private(self, user_id, message: str) -> Optional[int]:
        ws = self._connections.get(str(user_id))
        if ws is None:
            logger.warning(f"[web] 连接不存在: {user_id}")
            return None
        await ws.send_str(json.dumps({"text": message}, ensure_ascii=False))
        return 0

    async def send_group(self, group_id, message: str) -> Optional[int]:
        return None

    async def api_call(self, action: str, params=None, timeout: float = 30):
        return None
