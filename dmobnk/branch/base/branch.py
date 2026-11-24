"""DMOBNK branch kiosk: a self-service screen for one retail branch,
deployed by Fleet to clusters labelled branch=true.

Customers use it on their own. With a (demo) card and PIN they can check
their balance and print a mini statement, withdraw or pay in cash, move
money between their own accounts and pay a bill, each with a receipt.
Without signing in they can take a queue ticket, book an adviser and see the
opening hours. The queue moves on by itself, as if advisers were serving.

Shared state lives in branch-redis, so any replica can serve any request.
All text, the currency, the demo customers and the limits come from the
mounted locale ConfigMap ("b" and "kiosk"). The app also points its own
Ingress at branch.<cluster external address>.sslip.io, or at the host set
in the optional dmobnk-branch ConfigMap (see branch.yaml and
../../common/ingress_host.py)."""
import hashlib
import html
import json
import os
import random
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/dmobnk")
import ingress_host  # noqa: E402  (mounted from the dmobnk-ingress-host ConfigMap)
from redis_lite import Redis  # noqa: E402  (the same)
import crm_feed  # noqa: E402  (the same: sends events to a linked CRM)

L = json.load(open("/locale/locale.json", encoding="utf-8"))
B = L["b"]
T = L["t"]
K = L["kiosk"]
BRANCH = os.environ.get("BRANCH_NAME", "").strip() or B["branch_name"]
SERVICES = ["A", "B", "C"]
MINUTES_PER_CUSTOMER = 4
ADVANCE_EVERY = 45          # seconds between "advisers" calling the next ticket
SESSION_S = 180             # a signed-in session ends after 3 idle minutes
PIN = "1234"                # the demo PIN for every demo card
PIN_TRIES = 3
NOTE = int(K["note"]) * 100
DAILY_LIMIT = int(K["limit"]) * 100
MAX_AMOUNT = int(K["max"]) * 100
KIOSK_CASH = int(L["branch_cash_start"]) * 100
CUSTOMERS = {c["id"]: c for c in K["customers"]}
ACCOUNTS = {a["id"]: (c["id"], a) for c in K["customers"] for a in c["accounts"]}
SLOT_TIMES = ["09:30", "10:30", "11:30", "12:30", "14:00", "15:00", "16:00"]


# ---------------------------------------------------------------- redis
r = Redis(os.environ.get("REDIS_HOST", "branch-redis"))


# Atomic money moves (amounts in minor units). Return the account's new
# balance, or -1 not enough money, -2 not enough cash in the kiosk,
# -3 over today's cash limit.
WITHDRAW_LUA = """
local amt = tonumber(ARGV[1])
local bal = tonumber(redis.call('GET', KEYS[1]))
if bal == nil or bal < amt then return -1 end
local spent = tonumber(redis.call('GET', KEYS[3]) or '0')
if spent + amt > tonumber(ARGV[2]) then return -3 end
local cash = tonumber(redis.call('GET', KEYS[2]) or ARGV[3])
if cash < amt then return -2 end
redis.call('SET', KEYS[1], bal - amt)
redis.call('SET', KEYS[2], cash - amt)
redis.call('INCRBY', KEYS[3], amt)
redis.call('EXPIRE', KEYS[3], 172800)
return bal - amt
"""
MOVE_LUA = """
local amt = tonumber(ARGV[1])
local bal = tonumber(redis.call('GET', KEYS[1]))
if bal == nil or bal < amt then return -1 end
redis.call('SET', KEYS[1], bal - amt)
if #KEYS > 1 then redis.call('INCRBY', KEYS[2], amt) end
return bal - amt
"""


def day():
    return time.strftime("%Y%m%d")


def k(name):
    return f"branch:{day()}:{name}"


# ---------------------------------------------------------------- accounts
def seed():
    """Opening balances and a few example transactions for the demo
    customers. Only fills in what's missing, so it's safe to call often."""
    now = int(time.time())
    for cid, c in CUSTOMERS.items():
        for a in c["accounts"]:
            r("SET", f"branch:bal:{a['id']}", int(a["balance"] * 100), "NX")
        if r("SET", f"branch:seeded:{cid}", 1, "NX"):
            rnd = random.Random(cid)
            cur = c["accounts"][0]["id"]
            for i, merchant in enumerate(K["seed_tx"]):
                amt = -rnd.randint(300, 6500)
                r("RPUSH", f"branch:tx:{cid}",
                  json.dumps({"t": now - (i + 1) * 86400 // 2, "acct": cur, "desc": merchant, "amount": amt / 100}))
    r("SET", "branch:cash", KIOSK_CASH, "NX")


def balance(acct):
    v = r("GET", f"branch:bal:{acct}")
    if v is None:
        seed()
        v = r("GET", f"branch:bal:{acct}")
    return int(v)


def add_tx(cid, acct, desc, minor):
    r("LPUSH", f"branch:tx:{cid}", json.dumps({"t": int(time.time()), "acct": acct, "desc": desc, "amount": minor / 100}))
    r("LTRIM", f"branch:tx:{cid}", 0, 29)


def accounts_view(cid):
    c = CUSTOMERS[cid]
    return {"name": c["name"], "last4": c["last4"],
            "accounts": [{"id": a["id"], "name": a["name"], "balance": balance(a["id"]) / 100} for a in c["accounts"]],
            "payees": c["payees"],
            "tx": [json.loads(x) for x in r("LRANGE", f"branch:tx:{cid}", 0, 9)]}


def ref():
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))


def receipt(title, cid, acct, lines, new_balance):
    c = CUSTOMERS[cid]
    return {"receipt": {"title": title, "ref": ref(), "t": int(time.time()), "card": c["last4"],
                        "lines": lines + [[B["r_new"] + " · " + ACCOUNTS[acct][1]["name"], new_balance / 100, "money"]]}}


def parse_amount(v, notes=False):
    """Minor units, or an error message."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
        return None, "amount"
    minor = int(round(v * 100))
    low = NOTE if notes else 1
    if not low <= minor <= MAX_AMOUNT:
        return None, "amount"
    if notes and minor % NOTE:
        return None, "notes"
    return minor, None


def own(cid, acct):
    return acct in ACCOUNTS and ACCOUNTS[acct][0] == cid


# ---------------------------------------------------------------- sessions
def sign_in(card, pin):
    cid = next((c for c, v in CUSTOMERS.items() if v["last4"] == card), None)
    if not cid:
        return 400, {"error": B["error"]}
    tries_key = f"branch:pin:{cid}"
    tries = int(r("GET", tries_key) or 0)
    if tries >= PIN_TRIES:
        return 423, {"error": B["pin_locked"], "locked": True}
    if not (isinstance(pin, str) and secrets.compare_digest(pin, PIN)):
        tries = r("INCR", tries_key)
        r("EXPIRE", tries_key, 60)
        left = PIN_TRIES - tries
        if left <= 0:
            return 423, {"error": B["pin_locked"], "locked": True}
        return 401, {"error": B["pin_bad"].replace("{n}", str(left))}
    r("DEL", tries_key)
    token = secrets.token_urlsafe(18)
    r("SET", f"branch:sess:{token}", cid, "EX", SESSION_S)
    crm_feed.send("kiosk_signin", "Signed in at the kiosk.", cust(cid))
    return 200, {"token": token, **accounts_view(cid)}


def session(h):
    token = h.headers.get("X-Kiosk-Session", "")
    if not token or len(token) > 64:
        return None
    cid = r("GET", f"branch:sess:{token}")
    if cid:
        r("EXPIRE", f"branch:sess:{token}", SESSION_S)
    return cid


# ---------------------------------------------------------------- money
def withdraw(cid, body):
    acct = body.get("acct")
    if not own(cid, acct):
        return 400, {"error": B["error"]}
    minor, err = parse_amount(body.get("amount"), notes=True)
    if err:
        return 400, {"error": err}
    left = r("EVAL", WITHDRAW_LUA, 3, f"branch:bal:{acct}", "branch:cash", k(f"withdrawn:{cid}"),
             minor, DAILY_LIMIT, KIOSK_CASH)
    if left < 0:
        return 409, {"error": {-1: B["err_funds"], -2: B["err_cash"], -3: "limit"}[left]}
    add_tx(cid, acct, B["tx_withdraw"], -minor)
    feed("withdraw", f"Withdrew {money(minor)} cash at the kiosk.", cid, [(acct, left)], [(acct, B["tx_withdraw"], -minor)])
    return 200, receipt(B["withdraw_h"], cid, acct, [[B["tx_withdraw"], minor / 100, "money"]], left)


def deposit(cid, body):
    acct = body.get("acct")
    if not own(cid, acct):
        return 400, {"error": B["error"]}
    minor, err = parse_amount(body.get("amount"), notes=True)
    if err:
        return 400, {"error": err}
    balance(acct)
    new = r("INCRBY", f"branch:bal:{acct}", minor)
    r("SET", "branch:cash", KIOSK_CASH, "NX")
    r("INCRBY", "branch:cash", minor)
    add_tx(cid, acct, B["tx_deposit"], minor)
    feed("deposit", f"Paid in {money(minor)} cash at the kiosk.", cid, [(acct, new)], [(acct, B["tx_deposit"], minor)])
    return 200, receipt(B["deposit_h"], cid, acct, [[B["tx_deposit"], minor / 100, "money"]], new)


def transfer(cid, body):
    src, dst = body.get("from"), body.get("to")
    if not (own(cid, src) and own(cid, dst)):
        return 400, {"error": B["error"]}
    if src == dst:
        return 400, {"error": B["err_same"]}
    minor, err = parse_amount(body.get("amount"))
    if err:
        return 400, {"error": err}
    balance(src), balance(dst)
    left = r("EVAL", MOVE_LUA, 2, f"branch:bal:{src}", f"branch:bal:{dst}", minor)
    if left < 0:
        return 409, {"error": B["err_funds"]}
    sname, dname = ACCOUNTS[src][1]["name"], ACCOUNTS[dst][1]["name"]
    add_tx(cid, src, B["tx_out"].replace("{acct}", dname), -minor)
    add_tx(cid, dst, B["tx_in"].replace("{acct}", sname), minor)
    feed("transfer", f"Moved {money(minor)} from {sname} to {dname} at the kiosk.", cid,
         [(src, left), (dst, balance(dst))], [(src, B["tx_out"].replace("{acct}", dname), -minor),
                                             (dst, B["tx_in"].replace("{acct}", sname), minor)])
    return 200, receipt(B["transfer_h"], cid, src,
                        [[B["from"], sname, "text"], [B["to"], dname, "text"], [B["amount"], minor / 100, "money"]], left)


def pay_bill(cid, body):
    acct = body.get("acct")
    payee = next((p for p in CUSTOMERS[cid]["payees"] if p["id"] == body.get("payee")), None)
    if not own(cid, acct) or not payee:
        return 400, {"error": B["error"]}
    minor, err = parse_amount(body.get("amount"))
    if err:
        return 400, {"error": err}
    balance(acct)
    left = r("EVAL", MOVE_LUA, 1, f"branch:bal:{acct}", minor)
    if left < 0:
        return 409, {"error": B["err_funds"]}
    add_tx(cid, acct, B["tx_bill"].replace("{payee}", payee["name"]), -minor)
    feed("bill", f"Paid {money(minor)} to {payee['name']} at the kiosk.", cid, [(acct, left)],
         [(acct, B["tx_bill"].replace("{payee}", payee["name"]), -minor)])
    return 200, receipt(B["bill_h"], cid, acct, [[B["payee"], payee["name"], "text"], [B["amount"], minor / 100, "money"]], left)


# ---------------------------------------------------------------- queue
def take_ticket(svc):
    n = r("INCR", k(f"seq:{svc}"))
    r("EXPIRE", k(f"seq:{svc}"), 172800)
    ahead = r("RPUSH", k("queue"), json.dumps({"no": f"{svc}{n:03d}", "svc": svc, "t": int(time.time())})) - 1
    r("EXPIRE", k("queue"), 172800)
    crm_feed.send("ticket", f"Ticket {svc}{n:03d} taken for {B['svc_' + svc]} ({ahead} waiting ahead).")
    return {"no": f"{svc}{n:03d}", "svc": svc, "ahead": ahead,
            "minutes": max(1, -(-ahead * ADVANCE_EVERY // 60))}


def advance_loop():
    """No staff screens any more, so the queue moves on by itself: one
    replica at a time (a short Redis lock) calls the next ticket."""
    while True:
        try:
            if r("SET", "branch:advance-lock", os.environ.get("POD_NAME", "pod"), "NX", "EX", ADVANCE_EVERY - 5):
                raw = r("LPOP", k("queue"))
                if raw:
                    r("SET", k("serving"), json.loads(raw)["no"], "EX", 172800)
        except Exception as err:
            print(f"queue advance failed: {err}", flush=True)
        time.sleep(ADVANCE_EVERY)


# ---------------------------------------------------------------- appointments
def opening(d):
    """(open, close) as "HH:MM" for that date, or None when closed."""
    row = K["hours"][d.weekday()]
    return (row[1], row[2]) if row[1] else None


def slots(offset):
    d = date.today() + timedelta(days=offset)
    hours = opening(d)
    if not hours:
        return d, []
    booked = set(r("HKEYS", f"branch:appt:{d.isoformat()}") or [])
    now = datetime.now().strftime("%H:%M")
    out = []
    for t in SLOT_TIMES:
        if not hours[0] <= t < hours[1] or (offset == 0 and t <= now) or t in booked:
            continue
        # About a third of the times are already taken by other (example) customers.
        if int(hashlib.sha256(f"{d}{t}".encode()).hexdigest(), 16) % 3 == 0:
            continue
        out.append(t)
    return d, out


def book(body):
    offset, t, topic = body.get("day"), body.get("time"), body.get("topic")
    name = str(body.get("name", "")).strip()[:30]
    if offset not in (0, 1, 2) or topic not in range(len(K["topics"])):
        return 400, {"error": B["error"]}
    if not name or any(ch in name for ch in "<>{}"):
        return 400, {"error": B["err_name"]}
    d, free = slots(offset)
    if t not in free or not r("HSETNX", f"branch:appt:{d.isoformat()}", t, json.dumps({"name": name, "topic": topic})):
        return 409, {"error": B["slot_gone"]}
    r("EXPIRE", f"branch:appt:{d.isoformat()}", 4 * 86400)
    crm_feed.send("appointment", f"{name} booked an appointment ({K['topics'][topic]}) for {d.isoformat()} at {t}.",
                  None, name=name, topic=topic, topic_name=K["topics"][topic], date=d.isoformat(), time=t)
    return 200, {"date": d.isoformat(), "time": t, "topic": K["topics"][topic], "ref": ref()}


# ---------------------------------------------------------------- page
def e(s):
    return html.escape(str(s), quote=True)


def cust(cid):
    return {"id": cid, "name": CUSTOMERS[cid]["name"]}


def feed(kind, text, cid, balances, txs):
    """A kiosk transaction for the CRM: what happened, the new balances and
    the statement lines (amounts in minor units)."""
    crm_feed.send(kind, text, cust(cid), balances=[{"id": a, "balance": v / 100} for a, v in balances],
                  tx=[{"acct": a, "desc": d, "amount": v / 100} for a, d, v in txs])


def money(minor):
    """Server-side twin of the page's money(), for error messages."""
    m = L["money"]
    x = minor / 100
    s = f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"
    s = s.replace(",", "\0").replace(".", m["dec"]).replace("\0", m["group"])
    return (m["symbol"] + (" " if m["space"] else "") + s) if m["pos"] == "pre" else (s + (" " if m["space"] else "") + m["symbol"])


ERRORS = {"amount": lambda: B["err_amount"].replace("{min}", money(1)).replace("{max}", money(MAX_AMOUNT)),
          "notes": lambda: B["err_notes"].replace("{n}", money(NOTE)),
          "limit": lambda: B["err_limit"].replace("{limit}", money(DAILY_LIMIT))}

CSS = """
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#eef2fa;color:#0b1b3a;font:17px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column;-webkit-tap-highlight-color:transparent;user-select:none}
button,input{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:3px solid #7fa2ff;outline-offset:3px}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace}
.disp{font-family:"Space Grotesk",Inter,sans-serif}
.demo{background:#fdecd3;border-bottom:1px solid #f0c58a;color:#5c3300;font-size:13px}
.demo div{max-width:1100px;margin:0 auto;display:flex;gap:10px;align-items:flex-start;padding:8px 16px}
.demo b{flex-shrink:0;background:#8a4b00;color:#fff;border-radius:6px;padding:1px 8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-top:1px}
header{background:#0b1b3a;color:#fff}
header div.in{max-width:1100px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 16px}
.logo{display:flex;align-items:center;gap:10px}
.logo b{font:700 22px/1.1 "Space Grotesk",sans-serif;letter-spacing:.04em;display:block}
.logo small{font-size:12px;color:#b9c6e4;font-weight:600}
.clock{font:700 26px "Space Grotesk",sans-serif}
main{flex:1;width:100%;max-width:1100px;margin:0 auto;padding:28px 16px 32px;display:flex;flex-direction:column}
.screen{display:flex;flex-direction:column;gap:22px;flex:1}
.screen[hidden]{display:none}
h1{margin:0;font:700 clamp(30px,4.6vw,48px)/1.08 "Space Grotesk",sans-serif;letter-spacing:-1.2px;text-wrap:balance}
h2{margin:0;font:700 22px/1.2 "Space Grotesk",sans-serif}
p{margin:0}
.muted{color:#4a5a7d}
.lede{font-size:19px;color:#3d4c6d}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}
.tile{border:2px solid #dfe5f1;background:#fff;border-radius:22px;padding:22px;min-height:150px;display:flex;flex-direction:column;gap:8px;text-align:left;transition:border-color .15s,transform .1s}
.tile:active{transform:scale(.98)}
.tile:hover{border-color:#2453ff}
.tile .ic{width:52px;height:52px;border-radius:15px;display:grid;place-items:center;background:#e6ecff;color:#1f4fe0;font:700 24px "Space Grotesk",sans-serif}
.tile b{font-size:20px}
.tile span.muted{font-size:15px}
.tile.primary{background:#0b1b3a;color:#fff;border-color:#0b1b3a}
.tile.primary .muted{color:#b9c6e4}
.tile.primary .ic{background:#2453ff;color:#fff}
.bar{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center;margin-top:auto;padding:14px 18px;border-radius:16px;background:#fff;border:1px solid #dfe5f1;font-weight:600}
.serving{font:700 22px "Space Grotesk",sans-serif}
.nav{display:flex;flex-wrap:wrap;gap:12px;margin-top:auto}
.btn{min-height:60px;padding:0 26px;border:0;border-radius:16px;font-weight:700;font-size:18px;background:#2453ff;color:#fff;display:inline-flex;align-items:center;justify-content:center;gap:8px}
.btn.navy{background:#0b1b3a}.btn.ghost{background:#fff;color:#0b1b3a;border:2px solid #c9d3e8}.btn.amber{background:#e8890c;color:#1d1200}
.btn.wide{flex:1}
.btn:disabled{opacity:.5}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}
.cc{all:unset;cursor:pointer;aspect-ratio:1.586;border-radius:18px;color:#fff;padding:20px;display:flex;flex-direction:column;justify-content:space-between;box-shadow:0 10px 26px rgba(11,27,58,.18);transition:transform .1s}
.cc:active{transform:scale(.97)}
.cc:focus-visible{outline:3px solid #7fa2ff;outline-offset:3px}
.cc b{font:700 20px "Space Grotesk",sans-serif;letter-spacing:.06em}
.cc .chip{width:42px;height:30px;border-radius:7px;background:linear-gradient(135deg,#f6d27a,#c8962c)}
.pinwrap{display:flex;flex-direction:column;align-items:center;gap:18px}
.dots{display:flex;gap:16px;min-height:28px}
.dots i{width:22px;height:22px;border-radius:50%;border:3px solid #0b1b3a}
.dots i.on{background:#0b1b3a}
.pad{display:grid;grid-template-columns:repeat(3,88px);gap:12px}
.pad button{height:72px;border:0;border-radius:18px;background:#fff;font:700 28px "Space Grotesk",sans-serif;box-shadow:0 1px 0 #c9d3e8}
.pad button.k-ok{background:#12804a;color:#fff;font-size:20px}
.pad button.k-clr{background:#fde4e7;color:#9b1c2c;font-size:18px}
.msg{min-height:1.5em;font-weight:700;color:#b42335;text-align:center}
.panel{background:#fff;border:1px solid #dfe5f1;border-radius:20px;padding:20px;display:flex;flex-direction:column;gap:14px}
.accts{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.acct{background:#0b1b3a;color:#fff;border-radius:18px;padding:18px;display:flex;flex-direction:column;gap:4px}
.acct span{color:#b9c6e4;font-size:14px}
.acct b{font:700 32px "Space Grotesk",sans-serif;letter-spacing:-.5px}
table{width:100%;border-collapse:collapse;font-size:16px}
td{padding:10px 8px;border-bottom:1px solid #eef2f8;font-variant-numeric:tabular-nums}
td.num{text-align:right;white-space:nowrap}
td.in{color:#12804a;font-weight:700}
.label{font-size:14px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:#4a5a7d}
.choices{display:flex;flex-wrap:wrap;gap:10px}
.choice{min-height:60px;padding:0 22px;border:2px solid #c9d3e8;border-radius:16px;background:#fff;font-weight:700;font-size:18px}
.choice[aria-pressed=true]{background:#0b1b3a;border-color:#0b1b3a;color:#fff}
.choice:disabled{opacity:.4}
input.big{height:64px;border:2px solid #c9d3e8;border-radius:16px;padding:0 18px;font:700 26px "Space Grotesk",sans-serif;width:min(320px,100%);background:#fff;user-select:text}
.ticket{background:#0b1b3a;color:#fff;border-radius:26px;padding:34px;display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;max-width:520px;width:100%;margin:0 auto}
.ticket .no{font:700 clamp(80px,16vw,128px)/1 "Space Grotesk",sans-serif;letter-spacing:-3px}
.hours td:first-child{font-weight:600}
.hours tr.today td{background:#eaf0ff}
.badge{display:inline-block;font-size:14px;font-weight:700;padding:4px 12px;border-radius:99px;background:#e3f4ea;color:#0d6b3c}
.badge.off{background:#fde4e7;color:#9b1c2c}
.overlay{position:fixed;inset:0;background:rgba(11,27,58,.6);display:grid;place-items:center;padding:16px;z-index:10}
.overlay[hidden]{display:none}
.slip{background:#fff;width:min(380px,100%);max-height:calc(100vh - 32px);overflow:auto;padding:22px 22px 18px;font:500 14px/1.5 "JetBrains Mono",monospace;color:#111;border-radius:4px;box-shadow:0 20px 50px rgba(0,0,0,.35);-webkit-mask:radial-gradient(circle 6px at 50% 100%,transparent 98%,#000) 0 0/16px 100%;display:flex;flex-direction:column;gap:6px}
.slip .row{display:flex;justify-content:space-between;gap:10px}
.slip hr{border:0;border-top:1px dashed #999;margin:6px 0;width:100%}
.slip .c{text-align:center}
.dialog{background:#fff;border-radius:22px;padding:26px;width:min(460px,100%);display:flex;flex-direction:column;gap:14px;text-align:center}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:420px){.pad{grid-template-columns:repeat(3,1fr);width:100%}.btn{min-height:56px}}
"""

LOGO = ('<svg width="34" height="34" viewBox="0 0 32 32" aria-hidden="true"><rect x="2" y="2" width="28" height="28" rx="9" fill="#fff"/>'
        '<rect x="8" y="17" width="4" height="7" rx="1" fill="#2453ff"/><rect x="14" y="12" width="4" height="12" rx="1" fill="#2453ff"/>'
        '<rect x="20" y="8" width="4" height="16" rx="1" fill="#e8890c"/></svg>')
SVC_COLORS = {"A": "#2453ff", "B": "#0e9f6e", "C": "#e8890c"}


def tile(action, icon, title, desc, cls=""):
    return (f'<button type="button" class="tile {cls}" data-go="{action}"><span class="ic" aria-hidden="true">{icon}</span>'
            f'<b>{e(title)}</b><span class="muted">{e(desc)}</span></button>')


def kiosk_page():
    cards = "".join(f'<button class="cc" type="button" data-card="{e(c["last4"])}" style="background:linear-gradient(135deg,{c["color"]},#13274f)">'
                    f'<span style="display:flex;justify-content:space-between;align-items:center"><b>DMOBNK</b><span class="chip"></span></span>'
                    f'<span class="mono" style="font-size:18px;letter-spacing:.12em">•••• {e(c["last4"])}</span>'
                    f'<span style="font-weight:600">{e(c["name"])}</span></button>' for c in K["customers"])
    pad = "".join(f'<button type="button" data-k="{n}">{n}</button>' for n in "123456789")
    pad += f'<button type="button" class="k-clr" data-k="clr">{e(B["clear"])}</button><button type="button" data-k="0">0</button><button type="button" class="k-ok" data-k="ok">{e(B["ok"])}</button>'
    svc = "".join(f'<button type="button" class="tile" data-svc="{s}"><span class="ic" style="background:{SVC_COLORS[s]};color:#fff">{s}</span>'
                  f'<b>{e(B["svc_" + s])}</b><span class="muted">{e(B["svc_" + s + "_d"])}</span></button>' for s in SERVICES)
    back = f'<div class="nav"><button type="button" class="btn ghost" data-go="home">← {e(B["back"])}</button></div>'
    mback = f'<div class="nav"><button type="button" class="btn ghost" data-go="menu">← {e(B["back"])}</button></div>'
    body = f"""
<section class="screen" id="s-home">
  <div style="display:flex;flex-direction:column;gap:8px"><h1>{e(B['welcome_h'].replace('{branch}', BRANCH))}</h1><p class="lede">{e(B['welcome_lede'])}</p></div>
  <div class="tiles">
    {tile('card', '▭', B['use_card'], B['use_card_d'], 'primary')}
    {tile('ticket', '#', B['ticket'], B['ticket_d'])}
    {tile('book', '◷', B['book'], B['book_d'])}
    {tile('hours', 'ⓘ', B['hours'], B['hours_d'])}
  </div>
  <div class="bar"><span class="serving" id="serving"></span><span class="muted" id="waiting"></span></div>
</section>

<section class="screen" id="s-card" hidden>
  <div style="display:flex;flex-direction:column;gap:6px"><h1>{e(B['card_h'])}</h1><p class="lede">{e(B['card_d'])}</p></div>
  <div class="cards">{cards}</div>{back}
</section>

<section class="screen" id="s-pin" hidden>
  <div class="pinwrap">
    <h1>{e(B['pin_h'])}</h1><p class="muted">{e(B['pin_hint'])}</p>
    <div class="dots" id="dots" aria-live="polite"><i></i><i></i><i></i><i></i></div>
    <p class="msg" id="pin-msg" role="alert"></p>
    <div class="pad" id="pad">{pad}</div>
  </div>{back}
</section>

<section class="screen" id="s-menu" hidden>
  <div style="display:flex;flex-direction:column;gap:6px"><h1 id="hello"></h1><p class="lede">{e(B['menu_h'])}</p></div>
  <div class="tiles">
    {tile('balance', '≡', B['m_balance'], '')}
    {tile('withdraw', '↑', B['m_withdraw'], '')}
    {tile('deposit', '↓', B['m_deposit'], '')}
    {tile('transfer', '⇄', B['m_transfer'], '')}
    {tile('bill', '✓', B['m_bill'], '')}
  </div>
  <div class="nav"><button type="button" class="btn amber wide" data-go="finish">{e(B['finish'])}</button></div>
</section>

<section class="screen" id="s-balance" hidden>
  <h1>{e(B['m_balance'])}</h1>
  <div class="accts" id="accts"></div>
  <div class="panel"><h2>{e(B['recent'])}</h2><table><tbody id="tx"></tbody></table></div>
  <div class="nav"><button type="button" class="btn ghost" data-go="menu">← {e(B['back'])}</button><button type="button" class="btn navy" id="print">{e(B['print'])}</button></div>
</section>

<section class="screen" id="s-withdraw" hidden>
  <h1>{e(B['withdraw_h'])}</h1>
  <div class="panel"><span class="label">{e(B['account'])}</span><div class="choices" data-pick="acct" id="w-acct"></div>
    <span class="label">{e(B['amount'])}</span><div class="choices" id="w-presets"></div>
    <label class="label" for="w-other">{e(B['other'])}</label><input class="big" id="w-other" type="number" inputmode="numeric" min="0"></div>
  <p class="msg" id="w-msg" role="alert"></p>
  {mback.replace('</div>', f'<button type="button" class="btn wide" id="w-go">{e(B["confirm"])}</button></div>')}
</section>

<section class="screen" id="s-deposit" hidden>
  <h1>{e(B['deposit_h'])}</h1><p class="lede">{e(B['deposit_d'])}</p>
  <div class="panel"><span class="label">{e(B['account'])}</span><div class="choices" data-pick="acct" id="d-acct"></div>
    <label class="label" for="d-amt">{e(B['amount'])}</label><input class="big" id="d-amt" type="number" inputmode="numeric" min="0"></div>
  <p class="msg" id="d-msg" role="alert"></p>
  {mback.replace('</div>', f'<button type="button" class="btn wide" id="d-go">{e(B["confirm"])}</button></div>')}
</section>

<section class="screen" id="s-transfer" hidden>
  <h1>{e(B['transfer_h'])}</h1>
  <div class="panel"><span class="label">{e(B['from'])}</span><div class="choices" data-pick="acct" id="t-from"></div>
    <span class="label">{e(B['to'])}</span><div class="choices" data-pick="acct" id="t-to"></div>
    <label class="label" for="t-amt">{e(B['amount'])}</label><input class="big" id="t-amt" type="number" inputmode="decimal" min="0" step="0.01"></div>
  <p class="msg" id="t-msg" role="alert"></p>
  {mback.replace('</div>', f'<button type="button" class="btn wide" id="t-go">{e(B["confirm"])}</button></div>')}
</section>

<section class="screen" id="s-bill" hidden>
  <h1>{e(B['bill_h'])}</h1>
  <div class="panel"><span class="label">{e(B['payee'])}</span><div class="choices" id="b-payee"></div>
    <span class="label">{e(B['from'])}</span><div class="choices" data-pick="acct" id="b-acct"></div>
    <label class="label" for="b-amt">{e(B['amount'])}</label><input class="big" id="b-amt" type="number" inputmode="decimal" min="0" step="0.01"></div>
  <p class="msg" id="b-msg" role="alert"></p>
  {mback.replace('</div>', f'<button type="button" class="btn wide" id="b-go">{e(B["confirm"])}</button></div>')}
</section>

<section class="screen" id="s-ticket" hidden>
  <h1>{e(B['ticket_h'])}</h1><div class="tiles">{svc}</div><p class="msg" id="k-msg" role="alert"></p>{back}
</section>

<section class="screen" id="s-gotticket" hidden aria-live="polite">
  <div class="ticket"><span style="color:#b9c6e4;font-weight:600">{e(B['your_ticket'])}</span><span class="no" id="t-no"></span>
    <span id="t-svc" style="font-weight:700"></span><span id="t-ahead" style="color:#b9c6e4"></span>
    <button type="button" class="btn amber" data-go="home" style="margin-top:8px;min-width:180px">{e(B['done'])}</button></div>
</section>

<section class="screen" id="s-book" hidden>
  <h1>{e(B['book_h'])}</h1>
  <div class="panel">
    <span class="label">{e(B['topic'])}</span><div class="choices" id="a-topic"></div>
    <span class="label">{e(B['day'])}</span><div class="choices" id="a-day"></div>
    <span class="label">{e(B['time'])}</span><div class="choices" id="a-time"></div>
    <label class="label" for="a-name">{e(B['name'])}</label><input class="big" id="a-name" maxlength="30" autocomplete="off" style="font-size:22px">
  </div>
  <p class="msg" id="a-msg" role="alert"></p>
  {back.replace('</div>', f'<button type="button" class="btn wide" id="a-go">{e(B["book_go"])}</button></div>')}
</section>

<section class="screen" id="s-booked" hidden aria-live="polite">
  <div class="ticket"><span style="font-size:44px" aria-hidden="true">✓</span><h1 style="color:#fff">{e(B['booked_h'])}</h1>
    <p id="booked-d" style="font-size:19px"></p><span class="mono" id="booked-ref" style="color:#b9c6e4"></span>
    <button type="button" class="btn amber" data-go="home" style="margin-top:8px;min-width:180px">{e(B['done'])}</button></div>
</section>

<section class="screen" id="s-hours" hidden>
  <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center"><h1>{e(B['hours_h'])}</h1><span class="badge" id="open-badge"></span></div>
  <div class="panel"><table class="hours"><tbody id="hours"></tbody></table></div>{back}
</section>

<section class="screen" id="s-bye" hidden>
  <div class="ticket"><span style="font-size:44px" aria-hidden="true">▭</span><h1 style="color:#fff">{e(B['bye'])}</h1></div>
</section>

<div class="overlay" id="receipt" hidden><div style="display:flex;flex-direction:column;gap:14px;align-items:center;width:min(380px,100%)">
  <div class="slip" id="slip" role="dialog" aria-label="{e(B['receipt'])}"></div>
  <button type="button" class="btn amber" id="receipt-ok" style="width:100%">{e(B['take_receipt'])}</button></div></div>

<div class="overlay" id="idle" hidden><div class="dialog" role="alertdialog" aria-labelledby="idle-h">
  <h2 id="idle-h">{e(B['idle_h'])}</h2><p id="idle-d"></p>
  <button type="button" class="btn" id="idle-ok">{e(B['still_here'])}</button></div></div>"""
    cfg = {"lang": L["lang"] if L["country"] != "sim" else "en-GB", "money": L["money"], "b": B, "branch": BRANCH,
           "colors": SVC_COLORS, "presets": K["presets"], "hours": K["hours"], "topics": K["topics"]}
    cfg_json = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{e(L['lang'])}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(B['title'])} · {e(BRANCH)} · DMOBNK</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">
<style>{CSS}</style>
</head>
<body>
<div class="demo" role="note"><div><b>{e(T['demo_label'])}</b><span>{e(T['demo_banner'])}</span></div></div>
<header><div class="in"><div class="logo">{LOGO}<span><b>DMOBNK</b><small>{e(BRANCH)} · {e(B['title'])}</small></span></div><span class="clock" id="clock"></span></div></header>
<main>{body}</main>
<script id="cfg" type="application/json">{cfg_json}</script>
<script>{KIOSK_JS}</script>
</body>
</html>"""


KIOSK_JS = r"""
const CFG = JSON.parse(document.getElementById('cfg').textContent);
const B = CFG.b;
const $ = id => document.getElementById(id);
function tr(key, vals) { let s = B[key] || key; for (const k in (vals || {})) s = s.split('{' + k + '}').join(vals[k]); return s; }
function num(x, d) {
  const m = CFG.money; const neg = x < 0; x = Math.abs(x);
  let [i, f] = x.toFixed(d).split('.');
  i = i.replace(/\B(?=(\d{3})+(?!\d))/g, m.group);
  return (neg ? '-' : '') + (f ? i + m.dec + f : i);
}
function money(x, d) {
  const m = CFG.money; const s = num(x, d === undefined ? 2 : d);
  return m.pos === 'pre' ? (x < 0 ? '-' : '') + m.symbol + (m.space ? ' ' : '') + s.replace('-', '') : s + (m.space ? ' ' : '') + m.symbol;
}
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function fmtTime(epoch) { return new Date(epoch * 1000).toLocaleString(CFG.lang, {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}); }

let token = null, me = null;
async function api(path, body) {
  const headers = {'Content-Type': 'application/json'}; if (token) headers['X-Kiosk-Session'] = token;
  const resp = await fetch('/api/' + path, body === undefined ? {cache: 'no-store', headers} : {method: 'POST', cache: 'no-store', headers, body: JSON.stringify(body)});
  let data = {}; try { data = await resp.json(); } catch (err) {}
  if (resp.status === 440) { signOut(); throw new Error(B.error); }
  if (!resp.ok) { const e = new Error(data.error || B.error); e.data = data; throw e; }
  return data;
}

// ---- screens
let screen = 'home';
function show(name) {
  document.querySelectorAll('.screen').forEach(s => { s.hidden = s.id !== 's-' + name; });
  document.querySelectorAll('.msg').forEach(m => { m.textContent = ''; });
  screen = name; window.scrollTo(0, 0);
  const h = $('s-' + name).querySelector('h1'); if (h) { h.tabIndex = -1; h.focus({preventScroll: true}); }
  if (OPEN[name]) OPEN[name]();
}
const OPEN = {
  pin: () => { pin = ''; drawPin(); },
  menu: () => { $('hello').textContent = tr('hello', {name: me.name.split(' ')[0]}); },
  balance: drawBalance,
  withdraw: () => { pickAcct('w-acct'); drawPresets(); $('w-other').value = ''; },
  deposit: () => { pickAcct('d-acct'); $('d-amt').value = ''; },
  transfer: () => { pickAcct('t-from', 0); pickAcct('t-to', 1); $('t-amt').value = ''; },
  bill: () => { pickAcct('b-acct'); drawPayees(); $('b-amt').value = ''; },
  book: drawBook,
  hours: drawHours,
};
document.addEventListener('click', ev => {
  const go = ev.target.closest('[data-go]'); if (!go) return;
  const to = go.dataset.go;
  if (to === 'finish') return signOut(true);
  show(to);
});

// ---- card + PIN
let card = null, pin = '';
document.querySelectorAll('[data-card]').forEach(b => b.addEventListener('click', () => { card = b.dataset.card; show('pin'); }));
function drawPin() { [...$('dots').children].forEach((d, i) => d.classList.toggle('on', i < pin.length)); }
$('pad').addEventListener('click', async ev => {
  const k = ev.target.closest('[data-k]'); if (!k) return;
  const key = k.dataset.k;
  if (key === 'clr') { pin = ''; drawPin(); return; }
  if (key === 'ok') {
    if (pin.length !== 4) return;
    try { const res = await api('login', {card, pin}); token = res.token; me = res; show('menu'); }
    catch (err) { $('pin-msg').textContent = err.message; pin = ''; drawPin(); if (err.data && err.data.locked) setTimeout(() => show('home'), 4000); }
    return;
  }
  if (pin.length < 4) { pin += key; drawPin(); }
});
document.addEventListener('keydown', ev => {
  if (screen !== 'pin') return;
  if (/^\d$/.test(ev.key)) $('pad').querySelector('[data-k="' + ev.key + '"]').click();
  else if (ev.key === 'Enter') $('pad').querySelector('[data-k="ok"]').click();
  else if (ev.key === 'Backspace') { pin = pin.slice(0, -1); drawPin(); }
});
async function signOut(polite) {
  if (token) { try { await api('logout', {}); } catch (err) {} }
  token = null; me = null; $('idle').hidden = true; $('receipt').hidden = true;
  if (polite) { show('bye'); setTimeout(() => { if (screen === 'bye') show('home'); }, 3500); } else show('home');
}
async function refreshMe() { const res = await api('me'); Object.assign(me, res); }

// ---- choices
function choices(id, items, selected, onPick) {
  const box = $(id);
  box.replaceChildren(...items.map((it, i) => {
    const b = el('button', 'choice', it.label); b.type = 'button'; b.dataset.v = it.value;
    if (it.sub) { b.append(el('br')); const s = el('span', '', it.sub); s.style.cssText = 'font-weight:500;font-size:15px;opacity:.8'; b.append(s); b.style.padding = '10px 20px'; b.style.textAlign = 'left'; }
    b.disabled = !!it.disabled; b.setAttribute('aria-pressed', String(i === selected));
    b.onclick = () => { [...box.children].forEach(c => c.setAttribute('aria-pressed', String(c === b))); if (onPick) onPick(it.value); };
    return b;
  }));
}
const picked = id => { const b = $(id).querySelector('[aria-pressed=true]'); return b ? b.dataset.v : null; };
function pickAcct(id, i) { choices(id, me.accounts.map(a => ({value: a.id, label: a.name, sub: money(a.balance)})), i || 0); }

// ---- balance
function drawBalance() {
  $('accts').replaceChildren(...me.accounts.map(a => { const d = el('div', 'acct'); d.append(el('span', '', a.name), el('b', '', money(a.balance))); return d; }));
  const names = Object.fromEntries(me.accounts.map(a => [a.id, a.name]));
  $('tx').replaceChildren(...(me.tx.length ? me.tx.map(t => {
    const r = el('tr'); const d = el('td'); d.append(el('strong', '', t.desc), el('br'), el('span', 'muted', fmtTime(t.t) + ' · ' + names[t.acct]));
    r.append(d, el('td', 'num' + (t.amount > 0 ? ' in' : ''), (t.amount > 0 ? '+' : '') + money(t.amount))); return r;
  }) : [(() => { const r = el('tr'); r.append(el('td', 'muted', B.no_tx)); return r; })()]));
}
$('print').onclick = () => {
  const lines = me.accounts.map(a => [a.name, a.balance, 'money']);
  me.tx.slice(0, 6).forEach(t => lines.push([t.desc, t.amount, 'money']));
  showReceipt({title: B.m_balance, ref: '', t: Date.now() / 1000, card: me.last4, lines});
};

// ---- receipts
function showReceipt(r) {
  const s = $('slip');
  const row = (a, b) => { const d = el('div', 'row'); d.append(el('span', '', a), el('span', '', b)); return d; };
  const parts = [el('div', 'c', 'DMOBNK'), el('div', 'c', CFG.branch), el('div', 'c', r.title.toUpperCase()), el('hr'),
    row(B.r_time, fmtTime(r.t)), el('div', 'c', '•••• •••• •••• ' + r.card)];
  if (r.ref) parts.push(row(B.r_ref, r.ref));
  parts.push(el('hr'));
  for (const [label, value, kind] of r.lines) parts.push(row(label, kind === 'money' ? money(value) : value));
  parts.push(el('hr'), el('div', 'c', B.r_thanks), el('div', 'c', T_DEMO));
  s.replaceChildren(...parts);
  $('receipt').hidden = false; $('receipt-ok').focus();
}
const T_DEMO = '** DEMO **';
$('receipt-ok').onclick = () => { $('receipt').hidden = true; if (token) show('menu'); };

// ---- money actions
async function doMoney(path, body, msgId, btn) {
  btn.disabled = true; $(msgId).textContent = '';
  try { const res = await api(path, body); await refreshMe(); showReceipt(res.receipt); }
  catch (err) { $(msgId).textContent = err.message; }
  btn.disabled = false;
}
function amountOf(id) { const v = parseFloat($(id).value); return isFinite(v) ? v : NaN; }
function drawPresets() {
  choices('w-presets', CFG.presets.map(p => ({value: p, label: money(p, 0)})), -1, () => { $('w-other').value = ''; });
}
$('w-other').addEventListener('input', () => { [...$('w-presets').children].forEach(c => c.setAttribute('aria-pressed', 'false')); });
$('w-go').onclick = ev => {
  const amount = $('w-other').value ? amountOf('w-other') : +(picked('w-presets') || NaN);
  doMoney('withdraw', {acct: picked('w-acct'), amount}, 'w-msg', ev.target);
};
$('d-go').onclick = ev => doMoney('deposit', {acct: picked('d-acct'), amount: amountOf('d-amt')}, 'd-msg', ev.target);
$('t-go').onclick = ev => doMoney('transfer', {from: picked('t-from'), to: picked('t-to'), amount: amountOf('t-amt')}, 't-msg', ev.target);
function drawPayees() { choices('b-payee', me.payees.map(p => ({value: p.id, label: p.name})), 0); }
$('b-go').onclick = ev => doMoney('bill', {acct: picked('b-acct'), payee: picked('b-payee'), amount: amountOf('b-amt')}, 'b-msg', ev.target);

// ---- queue
document.querySelectorAll('[data-svc]').forEach(b => b.addEventListener('click', async () => {
  try {
    const t = await api('ticket', {svc: b.dataset.svc});
    $('t-no').textContent = t.no; $('t-svc').textContent = B['svc_' + t.svc];
    $('t-ahead').textContent = t.ahead ? tr('ahead', {n: t.ahead, min: t.minutes}) : B.ahead_none;
    show('gotticket');
  } catch (err) { $('k-msg').textContent = err.message; }
}));

// ---- appointments
let bookDay = 0;
function dayLabel(iso, i) {
  const d = new Date(iso + 'T12:00:00');
  const name = d.toLocaleDateString(CFG.lang, {weekday: 'short', day: 'numeric', month: 'short'});
  return i === 0 ? B.today + ' · ' + name : i === 1 ? B.tomorrow + ' · ' + name : name;
}
async function drawSlots() {
  const s = await api('slots?day=' + bookDay);
  choices('a-time', s.times.length ? s.times.map(t => ({value: t, label: t})) : [{value: '', label: B.no_slots, disabled: true}], -1);
}
async function drawBook() {
  choices('a-topic', CFG.topics.map((t, i) => ({value: i, label: t})), 0);
  const days = await api('days');
  choices('a-day', days.map((d, i) => ({value: i, label: dayLabel(d.date, i), disabled: !d.open})), days.findIndex(d => d.open),
          v => { bookDay = +v; drawSlots(); });
  bookDay = Math.max(0, days.findIndex(d => d.open));
  $('a-name').value = me ? me.name.split(' ')[0] : '';
  drawSlots();
}
$('a-go').onclick = async ev => {
  const name = $('a-name').value.trim(), time = picked('a-time');
  if (!time) { $('a-msg').textContent = B.time + '?'; return; }
  if (!name) { $('a-msg').textContent = B.err_name; return; }
  ev.target.disabled = true;
  try {
    const res = await api('book', {day: bookDay, time, topic: +picked('a-topic'), name});
    $('booked-d').textContent = tr('booked_d', {topic: res.topic, day: dayLabel(res.date, bookDay), time: res.time});
    $('booked-ref').textContent = B.r_ref + ' ' + res.ref;
    show('booked');
  } catch (err) { $('a-msg').textContent = err.message; drawSlots(); }
  ev.target.disabled = false;
};

// ---- opening hours
function isOpen() {
  const now = new Date(); const row = CFG.hours[(now.getDay() + 6) % 7];
  const hm = now.toTimeString().slice(0, 5);
  return !!row[1] && hm >= row[1] && hm < row[2];
}
function drawHours() {
  const today = (new Date().getDay() + 6) % 7;
  $('hours').replaceChildren(...CFG.hours.map((h, i) => {
    const r = el('tr', i === today ? 'today' : ''); r.append(el('td', '', h[0]), el('td', 'num', h[1] ? h[1] + ' – ' + h[2] : B.closed)); return r;
  }));
  const open = isOpen(); $('open-badge').textContent = open ? B.open_now : B.closed_now; $('open-badge').className = 'badge' + (open ? '' : ' off');
}

// ---- home strip + clock
async function homeTick() {
  try {
    const s = await api('status');
    $('serving').textContent = s.serving ? tr('now_serving', {no: s.serving}) : s.waiting ? '' : B.no_queue;
    $('waiting').textContent = s.waiting ? tr('waiting', {n: s.waiting}) : '';
  } catch (err) {}
}
homeTick(); setInterval(homeTick, 5000);
function clock() { $('clock').textContent = new Date().toLocaleTimeString(CFG.lang, {hour: '2-digit', minute: '2-digit'}); }
clock(); setInterval(clock, 10000);

// ---- idle: signed in, warn after 45 s and sign out 15 s later; otherwise go home after 60 s
let lastTouch = Date.now(), warnTimer = null;
['pointerdown', 'keydown', 'input'].forEach(t => document.addEventListener(t, () => { lastTouch = Date.now(); }, true));
setInterval(() => {
  const idle = (Date.now() - lastTouch) / 1000;
  if (token && idle > 45 && $('idle').hidden) {
    let left = 15; $('idle-d').textContent = tr('idle_d', {s: left}); $('idle').hidden = false; $('idle-ok').focus();
    warnTimer = setInterval(() => { left--; $('idle-d').textContent = tr('idle_d', {s: left}); if (left <= 0) { clearInterval(warnTimer); signOut(true); } }, 1000);
  }
  if (!token && screen !== 'home' && idle > 60) show('home');
}, 1000);
$('idle-ok').onclick = () => { clearInterval(warnTimer); $('idle').hidden = true; lastTouch = Date.now(); };
"""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, v in (extra or {}).items():
            self.send_header(key, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        if isinstance(obj, dict) and isinstance(obj.get("error"), str) and obj["error"] in ERRORS:
            obj = dict(obj, error=ERRORS[obj["error"]]())
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = path.strip("/")
        if path == "healthz":
            return self._json(200, {"ok": True})
        try:
            if path == "":
                return self._send(200, "text/html; charset=utf-8", kiosk_page())
            if path in ("board", "teller"):  # the old staff screens
                return self._send(302, "text/plain", "", {"Location": "/"})
            if path == "api/status":
                return self._json(200, {"serving": r("GET", k("serving")), "waiting": r("LLEN", k("queue")),
                                        "pod": os.environ.get("POD_NAME", "")})
            if path == "api/days":
                out = []
                for i in range(3):
                    d = date.today() + timedelta(days=i)
                    out.append({"date": d.isoformat(), "open": bool(opening(d)) and (i > 0 or bool(slots(0)[1]))})
                return self._json(200, out)
            if path == "api/slots":
                offset = {"day=0": 0, "day=1": 1, "day=2": 2}.get(query, 0)
                d, free = slots(offset)
                return self._json(200, {"date": d.isoformat(), "times": free})
            if path == "api/me":
                cid = session(self)
                return self._json(200, accounts_view(cid)) if cid else self._json(440, {"error": B["error"]})
        except Exception as err:
            print(f"GET {path}: {err}", flush=True)
            return self._json(503, {"error": B["error"]})
        self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0].strip("/")
        length = min(int(self.headers.get("Content-Length") or 0), 2048)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": B["error"]})
        try:
            if path == "api/ticket":
                if body.get("svc") not in SERVICES:
                    return self._json(400, {"error": B["error"]})
                return self._json(200, take_ticket(body["svc"]))
            if path == "api/book":
                return self._json(*book(body))
            if path == "api/login":
                return self._json(*sign_in(str(body.get("card", "")), body.get("pin")))
            if path == "api/logout":
                token = self.headers.get("X-Kiosk-Session", "")
                if token:
                    r("DEL", f"branch:sess:{token}")
                return self._json(200, {"ok": True})
            actions = {"api/withdraw": withdraw, "api/deposit": deposit, "api/transfer": transfer, "api/bill": pay_bill}
            if path in actions:
                cid = session(self)
                if not cid:
                    return self._json(440, {"error": B["error"]})
                return self._json(*actions[path](cid, body))
        except Exception as err:
            print(f"POST {path}: {err}", flush=True)
            return self._json(503, {"error": B["error"]})
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


try:
    seed()
except Exception as err:  # redis not up yet: seed() runs again on first use
    print(f"seed postponed: {err}", flush=True)
threading.Thread(target=advance_loop, daemon=True).start()
crm_feed.SOURCE.update(app="kiosk", where=BRANCH)
ingress_host.start("branch-web", "branch", os.environ.get("BRANCH_HOST", ""), os.environ.get("INGRESS_CLASS", ""))
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
