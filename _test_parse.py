"""快速测试 _fix_js_object 能否处理 infocenter JSONP 结构"""
import re, json

s = '{"code":0,"data":\n{main:{needFold:\'\',searchtype:\'\',vFeeds:[{uin:\'2899893413\',key:\'abc\',summary:\'test content\'}]}}}'

def _fix(s):
    s = re.sub(r'([{,]\s*)([a-zA-Z_]\w*)(\s*:)', r'"\1"\2"\3', s)
    s = re.sub(r"(:\s*)'([^']*)'", r'\1"\2"', s)
    s = re.sub(r',\s*([}\]])', r'\1', s)
    s = re.sub(r':\s*undefined\b', ':null', s)
    return s

fixed = _fix(s)
print('FIXED:', fixed[:200])
try:
    d = json.loads(fixed)
    print('PARSED keys:', list(d.keys()))
    inner = d.get('data', {})
    print('inner keys:', list(inner.keys()))
    main = inner.get('main', {})
    print('main keys:', list(main.keys()))
    vfeeds = main.get('vFeeds', [])
    print('vFeeds:', vfeeds)
except Exception as e:
    print('FAIL:', e)
