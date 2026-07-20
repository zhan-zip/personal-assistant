import json
from qzone_browser import QZoneBrowser

text = '_Callback({"code":0,"data":{"data":[{"uin":"3","key":"c","appid":"311"}]}});'
browser = QZoneBrowser.__new__(QZoneBrowser)
data = browser._parse_jsonp(text)
print('parsed:', data)
inner = data.get('data', {})
print('inner:', inner)
print('type:', type(inner))
entries = inner.get('data') or inner.get('feedlist') or []
print('entries:', entries)
print('type:', type(entries))

print()
text2 = '_Callback({"code":0,"data":{"main":{},"data":[{"uin":"1","key":"a","appid":"311"},{"uin":"2","key":"b","appid":"311"}]}});'
data2 = browser._parse_jsonp(text2)
inner2 = data2.get('data', {})
print('inner2:', inner2)
entries2 = inner2.get('data') or inner2.get('feedlist') or []
print('entries2:', entries2)
print('type:', type(entries2))
