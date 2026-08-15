"""
Phase 4 工具测试: 沙箱文件工具 + 待办工具 (SQLite)
- 文件: write_file / read_file / append_file / list_files + 越界防护
- 待办: create_todo / list_todos / update_todo (走 memory_store todos 表)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as bot_module
from core.memory_store import MemoryStore
from llm.tools import execute_tool


def _new_bot(ws_dir):
    b = bot_module.QQBot.__new__(bot_module.QQBot)
    b.config = {"workspace": {"dir": ws_dir}}
    b.memory_store = MemoryStore(":memory:")
    return b


async def test_file_tools():
    with tempfile.TemporaryDirectory() as d:
        b = _new_bot(d)
        # write → read
        r = await execute_tool(b, "write_file", {"filename": "notes/idea.txt", "content": "第一行"}, 1)
        assert "已写入" in r, r
        r2 = await execute_tool(b, "read_file", {"filename": "notes/idea.txt"}, 1)
        assert "第一行" in r2, r2
        # append
        r3 = await execute_tool(b, "append_file", {"filename": "notes/idea.txt", "content": "第二行"}, 1)
        assert "已追加" in r3, r3
        r4 = await execute_tool(b, "read_file", {"filename": "notes/idea.txt"}, 1)
        assert "第一行" in r4 and "第二行" in r4, r4
        # list
        r5 = await execute_tool(b, "list_files", {}, 1)
        assert "notes" in r5, r5
        r6 = await execute_tool(b, "list_files", {"path": "notes"}, 1)
        assert "idea.txt" in r6, r6
        # 越界防护
        r7 = await execute_tool(b, "read_file", {"filename": "../secret.txt"}, 1)
        assert "越出" in r7 or "无效" in r7, r7
        r8 = await execute_tool(b, "write_file", {"filename": "../../evil.txt", "content": "x"}, 1)
        assert "越出" in r8 or "无效" in r8, r8
        # 确认没有真的写到沙箱外
        assert not os.path.exists(os.path.join(d, "..", "secret.txt"))
    print("Test 1 OK: 沙箱文件工具")


async def test_todo_tools():
    b = _new_bot(tempfile.mkdtemp())
    try:
        r = await execute_tool(b, "create_todo", {"content": "买牛奶"}, 100)
        assert "已添加待办" in r, r
        await execute_tool(b, "create_todo", {"content": "写报告"}, 100)
        r3 = await execute_tool(b, "list_todos", {}, 100)
        assert "买牛奶" in r3 and "写报告" in r3, r3
        # 不同用户隔离
        r3b = await execute_tool(b, "list_todos", {}, 200)
        assert "暂无待办" in r3b, r3b
        # 更新状态
        todos = await b.memory_store.list_todos("100")
        assert len(todos) == 2
        tid = todos[0]["id"]
        r4 = await execute_tool(b, "update_todo", {"todo_id": tid, "status": "done"}, 100)
        assert "已更新" in r4, r4
        r5 = await execute_tool(b, "list_todos", {}, 100)
        assert "[x]" in r5, r5
        # 非法状态
        r6 = await execute_tool(b, "update_todo", {"todo_id": tid, "status": "bad"}, 100)
        assert "pending/done/cancelled" in r6, r6
    finally:
        await b.memory_store.close()
    print("Test 2 OK: 待办工具 (SQLite)")


async def main():
    await test_file_tools()
    await test_todo_tools()
    print("\nAll 2 Phase 4 tool tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
