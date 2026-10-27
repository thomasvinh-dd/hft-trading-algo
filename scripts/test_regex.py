import urllib.request, re
html = urllib.request.urlopen(
    urllib.request.Request("https://finance.vietstock.vn/lich-su-kien.htm",
        headers={"User-Agent": "Mozilla/5.0 Chrome/151"}),
    timeout=15,
).read().decode("utf-8", errors="replace")
print(f"html len: {len(html)}")

# Try each pattern from corporate_actions.py
patterns = [
    r'name=__RequestVerificationToken\s+type=hidden\s+value=([^\s>]+)',
    r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
    r'value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']',
]
for i, p in enumerate(patterns, 1):
    m = re.search(p, html)
    print(f"Pattern {i}: matched={bool(m)}")
    if m:
        print(f"  value: {m.group(1)[:30]}...")

# Try simpler regex
m = re.search(r"__RequestVerificationToken[^>]*value=[\"']([^\"']+)[\"']", html)
print(f"Simpler regex (attribute order tolerance): matched={bool(m)}")
if m:
    print(f"  value: {m.group(1)[:30]}...")

# How many tokens are there?
all_tokens = re.findall(r"__RequestVerificationToken", html)
print(f"Total tokens in page: {len(all_tokens)}")
