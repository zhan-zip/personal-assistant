"""
nick_patterns 单元测试: 验证 detect_and_handle_intent 中的昵称正则
- "你叫什么" 不应触发改名
- "把昵称改成小李" 应能正确解析出新名字
- "改名叫小李" 边界场景
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import re


# 直接复制 profile.py 中的 nick_patterns (避免依赖)
NICK_PATTERNS = [
    r'(?:修改|改|换|设置)(?:一下|一个)?(?:你的?)?(?:昵称|名字|称呼|名称)(?:为|成|是|：|:)?\s*(.+)',
    r'(?:把|将)(?:你的?)?(?:昵称|名字|称呼|名称)(?:修改|改|换|设为|设置为|设为|改成|换成)(?:为|成|是|：|:)?\s*(.+)',
    r'改名[为成]?\s*(.+)',
    r'换名字[为成]?\s*(.+)',
]

# 复制 _looks_like_question 逻辑
QUESTION_HINTS = (
    "什么", "谁", "怎么", "为啥", "为什么", "哪", "吗", "呢",
    "?", "？", "如何", "几岁", "多大", "谁啊", "哪位",
)
NAME_KEYWORDS = ("昵称", "名字", "名称", "称呼", "叫")


def looks_like_question(text: str) -> bool:
    if not text:
        return True
    if any(text.startswith(q) for q in ("什么", "谁", "哪", "怎么", "为", "为什", "多", "几", "如", "?", "？")):
        return True
    for q in QUESTION_HINTS:
        if q in text:
            return True
    if any(w in text for w in ("你", "我", "他", "她", "它", "谁", "自己")):
        return True
    if "叫" in text and not any(n in text for n in NAME_KEYWORDS):
        if not any(n in text for n in ("昵称", "名字", "名称", "称呼")):
            return True
    return False


def extract_new_nick(text: str):
    """模拟 detect_and_handle_intent 的解析"""
    text = text.strip()
    for pat in NICK_PATTERNS:
        m = re.search(pat, text)
        if m:
            value = m.group(1).strip().rstrip("。！!，, ")
            if value and len(value) <= 20 and not looks_like_question(value):
                return value
            return None  # 拒绝
    return None


def test_explicit_change_works():
    """显式改名应能正确解析"""
    cases = [
        ("把昵称改成小李", "小李"),
        ("改名字为小明", "小明"),
        ("修改昵称为小红", "小红"),
        ("改名成小王", "小王"),
        ("把名字改成小张", "小张"),
        ("把昵称设置为小赵", "小赵"),
        ("换个名字叫小钱", None),  # pattern 没"叫", 应该 no match
    ]
    for text, expected in cases:
        result = extract_new_nick(text)
        if expected is None:
            # 应该 no match (返回 None)
            assert result is None, f"FAIL: {text!r} should not trigger (got {result!r})"
        else:
            assert result == expected, f"FAIL: {text!r} -> {result!r}, expected {expected!r}"
    print("Test 1 OK: 显式改名正确解析")


def test_question_never_triggers():
    """疑问句绝不能触发改名"""
    questions = [
        "你叫什么",
        "你叫什么名字",
        "你叫啥",
        "你的名字是什么",
        "你叫什么呀",
        "我叫什么",
        "什么",
        "你多大了",
        "今天天气怎么样",
    ]
    for q in questions:
        result = extract_new_nick(q)
        assert result is None, f"FAIL: {q!r} should NOT trigger change (got {result!r})"
    print("Test 2 OK: 疑问句/闲聊不触发改名")


def test_explicit_with_emoji():
    """带表情的显式改名"""
    cases = [
        ("把昵称改成小李吧", "小李吧"),
        ("设置昵称: 小明", "小明"),
    ]
    for text, expected in cases:
        result = extract_new_nick(text)
        assert result == expected, f"FAIL: {text!r} -> {result!r}, expected {expected!r}"
    print("Test 3 OK: 带表情/标点显式改名")


if __name__ == "__main__":
    test_explicit_change_works()
    test_question_never_triggers()
    test_explicit_with_emoji()
    print("\nAll 3 nick_patterns tests passed!")
