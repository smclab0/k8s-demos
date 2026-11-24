"""What DMOSHP's online shop (../shop) and in-store self-checkout (../sok)
share: the catalogue, live stock and orders in shop-redis, card payments
and the page chrome (banner, footer, logo).

Payments: a DMOBNK card goes to the bank's cards-svc on this cluster
(namespace dmobnk, POST /authorise), so freezing the card in online banking
declines it here. Any other card is a simulated approval. Paid orders with
the DMOBNK card also reach DMOBNK's CRM, when one runs on the cluster
(crm_feed.py, CRM_NAMESPACE=dmobnk).

Mounted from the dmoshp-lib ConfigMap at /opt/dmobnk, next to copies of
DMOBNK's shared modules (see ../sync-shared.sh)."""
import html
import json
import os
import secrets
import time
import urllib.error
import urllib.request

import crm_feed
from redis_lite import Redis

r = Redis(os.environ.get("REDIS_HOST", "shop-redis"))
BANK = os.environ.get("BANK_CARDS_URL", "http://cards-svc.dmobnk.svc:8080")
BANK_CARD = {"last4": "4821", "customer": "c1"}   # the DMOBNK online customer's card (dmobnk/base/services.yaml)
CREDIT = "Created By Steve McBride"
BANNER = "DMOSHP is a demo shop, part of the fictional DMOCRP group. Nothing here is for sale and no real money moves."


def e(s):
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------- catalogue
CATEGORIES = [("fresh", "Fresh"), ("bakery", "Bakery"), ("drinks", "Drinks"),
              ("pantry", "Pantry"), ("home", "Home"), ("tech", "Tech")]
# id (a demo barcode), name, category, price (GBP), size, colour
_P = [
    ("5060001000011", "British strawberries", "fresh", 3.25, "400 g", "#d9334a"),
    ("5060001000028", "Hass avocados", "fresh", 2.50, "pack of 2", "#4f7d2b"),
    ("5060001000035", "Braeburn apples", "fresh", 2.10, "6 pack", "#c0392b"),
    ("5060001000042", "Baby spinach", "fresh", 1.65, "200 g", "#2e7d32"),
    ("5060002000010", "Sourdough loaf", "bakery", 3.40, "800 g", "#b07a3c"),
    ("5060002000027", "All-butter croissants", "bakery", 2.75, "4 pack", "#d39b45"),
    ("5060002000034", "Cinnamon buns", "bakery", 3.10, "2 pack", "#9c5b2e"),
    ("5060002000041", "Seeded rye bread", "bakery", 2.95, "500 g", "#7b5a3a"),
    ("5060003000019", "Cold brew coffee", "drinks", 2.80, "330 ml", "#3e2723"),
    ("5060003000026", "Sparkling water", "drinks", 0.85, "1 L", "#1e88e5"),
    ("5060003000033", "Fresh orange juice", "drinks", 2.60, "1 L", "#f39c12"),
    ("5060003000040", "Oat milk barista", "drinks", 1.90, "1 L", "#c8a97e"),
    ("5060004000018", "Extra virgin olive oil", "pantry", 6.50, "500 ml", "#7a8b23"),
    ("5060004000025", "Fusilli pasta", "pantry", 1.20, "500 g", "#e0b040"),
    ("5060004000032", "Arborio rice", "pantry", 2.30, "1 kg", "#a59d8f"),
    ("5060004000049", "Dark chocolate 70%", "pantry", 2.25, "100 g", "#4e342e"),
    ("5060005000017", "Eco washing-up liquid", "home", 2.40, "500 ml", "#26a69a"),
    ("5060005000024", "Bamboo kitchen roll", "home", 3.60, "3 rolls", "#8d9e5c"),
    ("5060005000031", "Soy wax candle", "home", 8.00, "200 g", "#b39ddb"),
    ("5060005000048", "Linen tea towels", "home", 9.50, "pair", "#90a4ae"),
    ("5060006000016", "USB-C cable", "tech", 7.99, "2 m", "#37474f"),
    ("5060006000023", "Wireless earbuds", "tech", 39.00, "", "#263238"),
    ("5060006000030", "Power bank", "tech", 24.00, "10,000 mAh", "#455a64"),
    ("5060006000047", "AA batteries", "tech", 4.50, "8 pack", "#f9a825"),
]
PRODUCTS = {p[0]: {"id": p[0], "name": p[1], "cat": p[2], "price": p[3], "size": p[4], "color": p[5]} for p in _P}
START_STOCK = 30

# One simple line icon per category, drawn on each product's coloured tile.
ICONS = {
    "fresh": '<path d="M12 21c-4.5 0-7-3.4-7-7.5C5 9.6 8 7 12 8c4-1 7 1.6 7 5.5S16.5 21 12 21z"/><path d="M12 8c0-2.2 1-4 3-5"/>',
    "bakery": '<path d="M4 11c0-3.3 3.6-6 8-6s8 2.7 8 6v1H4z"/><path d="M5 12v6.5c0 .8.7 1.5 1.5 1.5h11c.8 0 1.5-.7 1.5-1.5V12"/>',
    "drinks": '<path d="M7 3h10l-1.5 17.5a1.5 1.5 0 0 1-1.5 1.5h-4a1.5 1.5 0 0 1-1.5-1.5z"/><path d="M7.6 9h8.8"/>',
    "pantry": '<rect x="5" y="7" width="14" height="14" rx="2"/><path d="M8 3h8v4H8z"/><path d="M9 12h6"/>',
    "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/><path d="M10 20v-5h4v5"/>',
    "tech": '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18h2"/>',
}
LOGO = ('<svg viewBox="0 0 32 32" width="34" height="34" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#0f7a4f"/>'
        '<path d="M9 12h14l-1.2 11.2a2 2 0 0 1-2 1.8h-7.6a2 2 0 0 1-2-1.8z" fill="#fff"/>'
        '<path d="M12.5 12V10a3.5 3.5 0 0 1 7 0v2" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="16" cy="18" r="2.2" fill="#0f7a4f"/></svg>')


def catalogue():
    stock = stock_all()
    return [dict(p, stock=stock.get(pid, 0)) for pid, p in PRODUCTS.items()]


def seed():
    if not r("EXISTS", "shop:stock"):
        args = []
        for pid in PRODUCTS:
            args += [pid, START_STOCK]
        r("HSET", "shop:stock", *args)


def stock_all():
    raw = r("HGETALL", "shop:stock") or []
    return {raw[i]: int(raw[i + 1]) for i in range(0, len(raw), 2)}


def restock():
    """Back to the starting stock (Kube Power-style demo reset, from the shop's /api/restock)."""
    r("DEL", "shop:stock")
    seed()


# ---------------------------------------------------------------- basket and payment
def money(x):
    return f"£{x:,.2f}"


def price_lines(items):
    """items: [{id, qty}] from the browser -> (lines, total) or raises ValueError."""
    if not isinstance(items, list) or not items or len(items) > 40:
        raise ValueError("Your basket is empty.")
    lines, total = [], 0.0
    for it in items:
        p = PRODUCTS.get(str((it or {}).get("id", "")))
        qty = (it or {}).get("qty")
        if not p or not isinstance(qty, int) or not 1 <= qty <= 20:
            raise ValueError("Something in your basket isn't right. Refresh and try again.")
        lines.append({"id": p["id"], "name": p["name"], "qty": qty, "price": p["price"],
                      "amount": round(p["price"] * qty, 2)})
        total += p["price"] * qty
    return lines, round(total, 2)


def take_stock(lines):
    """Reserves the items; returns "" or the name of what ran out (nothing taken)."""
    taken = []
    for ln in lines:
        left = r("HINCRBY", "shop:stock", ln["id"], -ln["qty"])
        taken.append(ln)
        if left < 0:
            for t in taken:
                r("HINCRBY", "shop:stock", t["id"], t["qty"])
            return ln["name"]
    return ""


def give_back(lines):
    for ln in lines:
        r("HINCRBY", "shop:stock", ln["id"], ln["qty"])


_bank = {"t": 0.0, "card": None}


def bank_card():
    """The DMOBNK card as cards-svc reports it, or None when the bank isn't
    on this cluster. Cached for 15 s."""
    if time.time() - _bank["t"] > 15:
        try:
            with urllib.request.urlopen(BANK + "/card", timeout=2) as resp:
                c = json.loads(resp.read())
            _bank["card"] = {"last4": c.get("last4"), "frozen": bool(c.get("frozen")),
                             "available": round(c.get("limit_gbp", 0) - c.get("balance_gbp", 0), 2)}
        except Exception:
            _bank["card"] = None
        _bank["t"] = time.time()
    return _bank["card"]


def pay_options():
    c = bank_card()
    return {"dmobnk": c, "other": True}


def authorise(method, amount, merchant):
    """-> {"approved": bool, "reason": str, "auth": str, "card": str, "by": str}"""
    if method == "dmobnk":
        body = json.dumps({"last4": BANK_CARD["last4"], "amount": amount, "merchant": merchant}).encode()
        req = urllib.request.Request(BANK + "/authorise", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                res = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            try:
                res = json.loads(err.read())
            except ValueError:
                res = {"reason": f"The bank said {err.code}"}
        except Exception:
            return {"approved": False, "reason": "DMOBNK can't be reached right now. Try another card.",
                    "card": BANK_CARD["last4"], "by": "DMOBNK"}
        _bank["t"] = 0       # refresh the card's state next time
        return {"approved": bool(res.get("approved")), "reason": res.get("reason", ""), "auth": res.get("auth", ""),
                "card": BANK_CARD["last4"], "by": "DMOBNK" + (f" · {res['pod']}" if res.get("pod") else "")}
    if method == "other":
        return {"approved": True, "reason": "", "auth": secrets.token_hex(3).upper(), "card": "0000",
                "by": "Simulated card"}
    return {"approved": False, "reason": "Pick a way to pay."}


def place(items, method, channel, where, customer_name=""):
    """Prices, reserves and pays for a basket. channel: "shop" or "sok".
    -> (http code, body)."""
    try:
        lines, total = price_lines(items)
    except ValueError as err:
        return 400, {"error": str(err)}
    out = take_stock(lines)
    if out:
        return 409, {"error": f"Sorry, we've just run out of {out}."}
    pay = authorise(method, total, f"DMOSHP {where}")
    if not pay["approved"]:
        give_back(lines)
        return 402, {"error": pay["reason"] or "Payment declined.", "declined": True}
    ref = ("W" if channel == "shop" else "S") + secrets.token_hex(3).upper()
    order = {"ref": ref, "t": int(time.time()), "channel": channel, "where": where, "lines": lines,
             "total": total, "card": pay["card"], "auth": pay.get("auth", ""), "by": pay["by"],
             "method": method, "name": customer_name[:40], "pod": os.environ.get("POD_NAME", "")}
    r("SET", f"shop:order:{ref}", json.dumps(order), "EX", 7 * 86400)
    r("LPUSH", "shop:orders", ref)
    r("LTRIM", "shop:orders", 0, 199)
    r("INCRBYFLOAT", f"shop:sales:{time.strftime('%Y-%m-%d')}", total)
    n = sum(ln["qty"] for ln in lines)
    what = f"{n} item{'s' if n != 1 else ''}"
    if method == "dmobnk":
        crm_feed.send("card_payment", f"Paid {money(total)} for {what} at DMOSHP {where} with card •••• {pay['card']}.",
                      {"id": BANK_CARD["customer"], "name": ""}, amount=total, ref=ref,
                      tx=[{"acct": "card", "desc": f"DMOSHP {where}", "amount": -total}])
    else:
        crm_feed.send("purchase", f"A DMOSHP {where} customer bought {what} for {money(total)}.", None,
                      amount=total, ref=ref)
    return 200, order


def order(ref):
    raw = r("GET", f"shop:order:{str(ref)[:12]}")
    return json.loads(raw) if raw else None


def recent_orders(n=8):
    out = []
    for ref in r("LRANGE", "shop:orders", 0, n - 1) or []:
        o = order(ref)
        if o:
            out.append({k: o[k] for k in ("ref", "t", "channel", "where", "total")})
    return out


# ---------------------------------------------------------------- page chrome
CHROME_CSS = """
.demo{background:#fdecd0;color:#5c3a00;font-size:13px}
.demo>div{max-width:1240px;margin:0 auto;display:flex;gap:10px;align-items:center;padding:7px 16px}
.demo b{background:#8a4b00;color:#fff;border-radius:6px;padding:1px 8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;flex:none}
.credit{text-align:center;font-size:12px;color:#5d6b63;padding:14px 16px 20px}
"""


def banner():
    return f'<div class="demo" role="note"><div><b>Demo</b><span>{e(BANNER)}</span></div></div>'


def footer(extra=""):
    return f'<footer class="credit">{extra}{e(CREDIT)}</footer>'
