from qzone_browser import QZoneBrowser

browser = QZoneBrowser.__new__(QZoneBrowser)

text1 = '_Callback({"code":0,"data":{"data":[{"uin":"3","key":"c","appid":"311"}]}});'
print("text1 repr:", repr(text1))
print("text1 len:", len(text1))
print("parsed1:", browser._parse_jsonp(text1))

text2 = '_Callback({"code":0,"data":' + \
        '{"data":[{"uin":"3","key":"c","appid":"311"}]}' + \
        '}});'
print()
print("text2 repr:", repr(text2))
print("text2 len:", len(text2))
print("parsed2:", browser._parse_jsonp(text2))
print()
print("equal:", text1 == text2)
