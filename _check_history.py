import json
d = json.load(open(r'd:\Desktop\test\test\qq-ai\chat_memory.json', encoding='utf-8'))
h = d.get('private_3496326306', [])
print(f'共 {len(h)} 条')
for m in h:
    ts = m.get('timestamp', '')[11:19]
    print(f'[{ts}] {m["role"]:9s}: {m["content"][:60]}')
