"""回归测试: 模拟各模块的纯函数, 不依赖 LLM / NapCat"""
import sys
import os

# 把工作目录加到 sys.path
sys.path.insert(0, os.path.dirname(__file__))

from bot import QQBot
from utils import format_time, now_iso, strip_html
from llm import extract_json_decision
from qzone_browser import _extract_f_info_text, QZoneBrowser


def test_parse_event_text():
    b = QQBot()
    msg, has_img, imgs, reply = b.message_processor.parse_event({
        "message_type": "private", "user_id": 1,
        "message": "hello world"
    })
    assert msg == "hello world" and not has_img
    print("Test 1 OK: parse_event 纯文本")


def test_parse_event_with_image():
    b = QQBot()
    msg, has_img, imgs, reply = b.message_processor.parse_event({
        "message_type": "private", "user_id": 1,
        "message": [
            {"type": "text", "data": {"text": "看这张图"}},
            {"type": "image", "data": {"url": "http://x.com/1.jpg"}}
        ]
    })
    assert msg == "看这张图" and has_img and len(imgs) == 1
    print("Test 2 OK: parse_event 含图片段")


def test_parse_event_with_reply():
    b = QQBot()
    msg, has_img, imgs, reply = b.message_processor.parse_event({
        "message_type": "private", "user_id": 1,
        "message": [
            {"type": "reply", "data": {"id": "12345"}},
            {"type": "text", "data": {"text": "你是?"}}
        ]
    })
    assert reply is not None and reply.get("data", {}).get("id") == "12345"
    print("Test 3 OK: parse_event 含 reply")


def test_dedup():
    b = QQBot()
    assert not b.event_handler.is_duplicate(1, "hello", 100)
    assert b.event_handler.is_duplicate(1, "hello", 100)
    assert b.event_handler.is_duplicate(1, "hello", 101)
    print("Test 4 OK: 消息去重")


def test_format_time():
    assert format_time("1700000000", "") != ""
    assert format_time("", "19:10") == "19:10"
    print("Test 5 OK: format_time")


def test_extract_json():
    assert extract_json_decision('{"a":1}')["a"] == 1
    assert extract_json_decision('输出: {"a":2} 完毕')["a"] == 2
    assert extract_json_decision("not json") is None
    assert extract_json_decision("") is None
    print("Test 6 OK: extract_json_decision")


def test_extract_f_info():
    html = '<li><div class="f-info">依旧是测试动态</div></li>'
    assert "依旧是测试动态" in _extract_f_info_text(html)
    html2 = r'<li><div class="f-info">\x3Cb\x3Ehi\x3C/b\x3E</div></li>'
    assert "hi" in _extract_f_info_text(html2)
    print("Test 7 OK: _extract_f_info_text")


def test_dedupe_entries():
    entries = [
        {"uin": "123", "key": "a", "appid": "311"},
        {"uin": "123", "key": "a", "appid": "311"},
        {"uin": "456", "key": "b", "appid": "311"},
    ]
    deduped = QZoneBrowser._dedupe_entries(entries)
    assert len(deduped) == 2
    print("Test 8 OK: _dedupe_entries")


def test_proactive_register_idempotent():
    b = QQBot()
    b._register_proactive_user(99999, None)
    assert "99999" in b.proactive_cache
    prev = b.proactive_cache.copy()
    b._register_proactive_user(99999, None)
    assert b.proactive_cache == prev
    print("Test 9 OK: _register_proactive_user 幂等")


def test_proactive_command():
    b = QQBot()
    out = b._handle_proactive_command(1, "列表")
    assert "好友" in out or "暂无" in out
    out = b._handle_proactive_command(1, "未知命令")
    assert "指令" in out
    print("Test 10 OK: #主动 指令")


def test_collect_entries():
    """模拟 QZone JSONP 响应, 验证 _collect_entries 能正确解析嵌套结构"""
    intercepted = [
        {"url": "x", "text": '_Callback({"code":0,"data":{"main":{},"data":[' +
                                  '{"uin":"1","key":"a","appid":"311"},' +
                                  '{"uin":"2","key":"b","appid":"311"}' +
                         ']}});'},
    ]
    browser = QZoneBrowser.__new__(QZoneBrowser)
    entries = browser._collect_entries(intercepted)
    assert len(entries) == 2
    assert entries[0]["uin"] == "1"
    print("Test 11 OK: _collect_entries 嵌套结构")


def test_collect_entries_alternative():
    """另一种结构: data.data 是 dict 套 data 列表"""
    intercepted = [
        {"url": "y", "text": '_Callback({"code":0,"data":' +
                                 '{"data":[{"uin":"3","key":"c","appid":"311"}]}' +
                       '});'},
    ]
    browser = QZoneBrowser.__new__(QZoneBrowser)
    entries = browser._collect_entries(intercepted)
    assert len(entries) == 1
    assert entries[0]["uin"] == "3"
    print("Test 12 OK: _collect_entries 嵌套 dict 套 data")


def test_now_iso():
    s = now_iso()
    assert "T" in s and "+08:00" in s
    print("Test 13 OK: now_iso")


def test_strip_html():
    out = strip_html("<p>hello <b>world</b></p>")
    assert "hello" in out and "world" in out
    print("Test 14 OK: strip_html")


if __name__ == "__main__":
    test_parse_event_text()
    test_parse_event_with_image()
    test_parse_event_with_reply()
    test_dedup()
    test_format_time()
    test_extract_json()
    test_extract_f_info()
    test_dedupe_entries()
    test_proactive_register_idempotent()
    test_proactive_command()
    test_collect_entries()
    test_collect_entries_alternative()
    test_now_iso()
    test_strip_html()
    print()
    print("=" * 50)
    print("All 14 functional tests passed!")
