"""
PROFILE_GUARD 单元测试: 验证 _is_explicit_profile_request
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from profile import ProfileManager


def make_pm():
    """构造一个 ProfileManager 实例, 不真正初始化 (只测 _is_explicit_profile_request)"""
    # 直接绕过 __init__, 因为我们只测静态方法式逻辑
    class _Stub:
        pass
    stub = _Stub()
    # 拿个 _is_explicit_profile_request 引用
    # ProfileManager 是类方法, 不依赖 self 状态
    return ProfileManager


def test_explicit_nickname_change():
    """显式 '把昵称改成 X' 应该返回 True"""
    PM = make_pm()
    # 我们要拿一个实例才能调, 但 _is_explicit_profile_request 不依赖 self
    # 简单的方式: 拿个空实例
    class _Dummy:
        pass
    inst = PM.__new__(PM)
    inst.user_text = ""
    # 显式动词
    assert inst._is_explicit_profile_request("把昵称改成 小明", "nickname") == True
    assert inst._is_explicit_profile_request("把名字改成小李", "nickname") == True
    assert inst._is_explicit_profile_request("换个名字叫小张", "nickname") == True
    assert inst._is_explicit_profile_request("设置昵称为小王", "nickname") == True
    print("Test 1 OK: 显式 '把昵称改成X' 返回 True")


def test_implicit_nickname_should_fail():
    """未含改动词的句子应该返回 False"""
    PM = make_pm()
    inst = PM.__new__(PM)
    # 用户问名字/问什么 → 不应触发改
    assert inst._is_explicit_profile_request("我叫什么名字", "nickname") == False
    assert inst._is_explicit_profile_request("什么", "nickname") == False
    assert inst._is_explicit_profile_request("你叫什么", "nickname") == False
    assert inst._is_explicit_profile_request("今天天气怎么样", "nickname") == False
    print("Test 2 OK: 问名字/闲聊 不触发改昵称")


def test_change_kind_mismatch():
    """改了 X 不等于改了 Y"""
    PM = make_pm()
    inst = PM.__new__(PM)
    # 用户说"改签名", 但 LLM 改的是 nickname → 应该拒绝
    assert inst._is_explicit_profile_request("把签名改成 abc", "nickname") == False
    # 用户说"改头像", 但 LLM 改的是 signature → 应该拒绝
    assert inst._is_explicit_profile_request("换个头像", "signature") == False
    print("Test 3 OK: 改 X ≠ 改 Y 类型校验生效")


def test_avatar_and_background():
    PM = make_pm()
    inst = PM.__new__(PM)
    # 头像
    assert inst._is_explicit_profile_request("换个头像", "avatar") == True
    assert inst._is_explicit_profile_request("把头像改成 X", "avatar") == True
    assert inst._is_explicit_profile_request("换个头像", "background") == False
    # 背景
    assert inst._is_explicit_profile_request("换背景", "background") == True
    assert inst._is_explicit_profile_request("把背景改成 X", "background") == True
    assert inst._is_explicit_profile_request("换背景", "avatar") == False
    print("Test 4 OK: 头像/背景 类型校验生效")


def test_empty_text():
    PM = make_pm()
    inst = PM.__new__(PM)
    assert inst._is_explicit_profile_request("", "nickname") == False
    assert inst._is_explicit_profile_request(None, "nickname") == False
    print("Test 5 OK: 空/None 文本直接拒绝")


if __name__ == "__main__":
    test_explicit_nickname_change()
    test_implicit_nickname_should_fail()
    test_change_kind_mismatch()
    test_avatar_and_background()
    test_empty_text()
    print("\nAll 5 PROFILE_GUARD tests passed!")
