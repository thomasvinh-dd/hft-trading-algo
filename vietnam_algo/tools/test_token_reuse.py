"""Test if __RequestVerificationToken is single-use."""
import sys, urllib.parse, urllib.request
sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.libs.corporate_actions import fetch_vietstock_session

session = fetch_vietstock_session(timeout=15.0)
print(f"session token: {session.token[:20]}...")
# Find the cookiejar handler
from urllib.request import HTTPCookieProcessor
cookie_handler = next((h for h in session.opener.handlers if isinstance(h, HTTPCookieProcessor)), None)
if cookie_handler:
    print(f"cookies: {[(c.name, c.value[:30]) for c in cookie_handler.cookiejar]}")
else:
    print("(no cookie handler)")
print()


def post(fDate, tDate, label):
    body = urllib.parse.urlencode({
        "eventTypeID": 1, "channelID": 0, "code": "", "catID": -1,
        "fDate": fDate, "tDate": tDate,
        "page": 1, "pageSize": 1, "orderBy": "Date1", "orderDir": "DESC",
        "__RequestVerificationToken": session.token,
    }).encode()
    req = urllib.request.Request(
        "https://finance.vietstock.vn/data/eventstypedata",
        data=body, method="POST",
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://finance.vietstock.vn",
            "Referer": "https://finance.vietstock.vn/lich-su-kien.htm",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 Chrome/151",
        },
    )
    try:
        with session.opener.open(req, timeout=15) as r:
            text = r.read().decode("utf-8", errors="replace")
            print(f"{label}: len={len(text)}, body[:80]={text[:80]!r}")
    except Exception as e:
        print(f"{label}: FAIL {type(e).__name__}: {e}")


print("=== Three sequential POSTs, same session, same token ===")
post("2026-09-02", "2026-09-09", "POST 1 (7d range)")
post("2026-09-02", "2026-09-09", "POST 2 (same range, same token)")
post("2026-09-02", "2026-09-09", "POST 3 (same)")
print()
print("=== Different range, same session/token ===")
post("2026-08-18", "2026-09-09", "POST 4 (3-week range)")
