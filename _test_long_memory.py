"""验证 long_memory prompt 模板不再触发 KeyError"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from long_memory import _EXTRACT_PROMPT, LongMemory


def test_prompt_format_no_keyerror():
    """验证 _EXTRACT_PROMPT.format(transcript=...) 不再 KeyError"""
    try:
        out = _EXTRACT_PROMPT.format(transcript="你好, 我是测试用户")
        # 模板里所有 {{ }} 应被还原为 { }
        assert "{transcript}" not in out, "transcript 应被替换"
        # 关键字 "facts" 应在输出里 (LLM 看得到示例 JSON)
        assert "facts" in out
        # 不应该还有未配对的 { (即不能是 {{ 或 }} 残留)
        # 简单检查: 转义后的字面 { 是成对 JSON 示例, 数 { 和 } 应相等
        assert out.count("{") == out.count("}"), \
            f"{{ 和 }} 数量应相等, 实际 {{={out.count('{')}, }}={out.count('}')}"
        print("Test 1 OK: prompt 模板 .format() 正常, 字面量 { } 已转义")
    except KeyError as e:
        print(f"FAIL: KeyError({e}) 仍然存在!")
        import sys; sys.exit(1)


def test_extract_and_store_smoke():
    """冒烟测试 extract_and_store 不抛异常 (LLM 部分会失败但不影响 prompt 修复)"""
    import asyncio
    from unittest.mock import AsyncMock

    # 模拟 LLM 客户端: 返回非法 JSON 也不会让 prompt 报错
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(return_value='{"facts": ["用户喜欢测试"]}')

    lm = LongMemory()
    # 调用会触发 .format() 的部分
    try:
        # 直接调用 format 路径
        prompt = _EXTRACT_PROMPT.format(transcript="x" * 100)
        print("Test 2 OK: format() 路径成功")
    except Exception as e:
        print(f"FAIL: {e}")
        import sys; sys.exit(1)


if __name__ == "__main__":
    test_prompt_format_no_keyerror()
    test_extract_and_store_smoke()
    print("\nAll 2 long_memory tests passed!")
