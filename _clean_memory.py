"""
清理 chat_memory.json 中的污染数据:
1. 任何 user 消息内容匹配 [系统...] / [联网搜索结果] 等注入前缀 → 改成 [已过滤]
2. 任何 assistant 消息包含违禁表达"你发了两遍"等 → 整条删除
"""
import json
import re
from pathlib import Path

P = Path(r'd:\Desktop\test\test\qq-ai\chat_memory.json')

_BAN = re.compile(
    r"你发了两遍|你又发了|又发了一遍|你发了两条|复读机|收到了(\d|两|三|几)遍|"
    r"我确实收到了.*遍|你确实发了.*遍|重复了|又是这句|你卡了|来来回回|"
    r"重复发|你重发了|你又重发|发了好几遍|发了(\d|两|三|几)遍|"
    r"开始重复|开始.*重复|就是从.*开始|那句开始.*的|从.*开始.*重复|"
    r"喊两遍|嗯嗯两遍|你发(\d|两|三|几)遍|你搁这"
)

_USER_BAD = (
    "[系统自动查询的结果", "[联网搜索结果]", "[链接内容]",
    "[图片]", "[用户发来了一张图片", "[用户正在回复",
)

d = json.loads(P.read_text(encoding="utf-8"))
total_removed = 0
total_fixed = 0
for key, msgs in d.items():
    new = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        if role == "user" and any(content.startswith(p) for p in _USER_BAD):
            m = dict(m)
            m["content"] = "[已过滤的系统注入消息]"
            total_fixed += 1
        if role == "assistant" and _BAN.search(content):
            total_removed += 1
            continue
        new.append(m)
    d[key] = new

P.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已删除 {total_removed} 条污染的 assistant 消息")
print(f"已修复 {total_fixed} 条污染的 user 消息")
