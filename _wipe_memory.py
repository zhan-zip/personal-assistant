"""
一键清空 chat_memory.json (所有用户, 包括群聊)
"""
import json
from pathlib import Path

P = Path(r'd:\Desktop\test\test\qq-ai\chat_memory.json')
d = json.loads(P.read_text(encoding='utf-8'))
keys = list(d.keys())
d = {}
P.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"已清空 {len(keys)} 个会话记忆: {keys}")
