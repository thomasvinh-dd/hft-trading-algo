"""Compare production headers vs simplified headers against Vietstock."""
import sys, urllib.parse, urllib.request
sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.libs.corporate_actions import fetch_vietstock_session

session = fetch_vietstock_session(timeout=15.0)


def post(fDate, tDate, label, headers):
    body = urllib.parse.urlencode({
        "eventTypeID": 1, "channelID": 0, "code": "", "catID": -1,
        "fDate": fDate, "tDate": tDate,
        "page": 1, "pageSize": 100, "orderBy": "Date1", "orderDir": "DESC",
        "__RequestVerificationToken": session.token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://finance.vietstock.vn/data/eventstypedata",
        data=body, method="POST", headers=headers,
    )
    try:
        with session.opener.open(req, timeout=15) as r:
            text = r.read().decode("utf-8", errors="replace")
            ct = r.headers.get("Content-Type")
            print(f"{label}: status={r.status}, ct={ct}, len={len(text)}")
            print(f"  body[:100]: {text[:100]!r}")
    except Exception as e:
        print(f"{label}: FAIL {e}")


print("=== Test 1: Production headers (long Edge UA, all headers) ===")
prod_headers = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://finance.vietstock.vn",
    "Referer": "https://finance.vietstock.vn/lich-su-kien.htm",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
    ),
    "X-Requested-With": "XMLHttpRequest",
}
post("2026-08-18", "2026-09-09", "PROD headers, 3-week range", prod_headers)

print()
print("=== Test 2: Simple Chrome UA, 3-week range ===")
simple_headers = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://finance.vietstock.vn",
    "Referer": "https://finance.vietstock.vn/lich-su-kien.htm",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 Chrome/151",
}
post("2026-08-18", "2026-09-09", "SIMPLE headers, 3-week range", simple_headers)
