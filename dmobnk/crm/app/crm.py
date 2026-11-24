"""DMOBNK CRM: the bank staff's customer and sales app, deployed by Fleet to
clusters labelled crm=true.

Staff sign in with the password in the `dmobnk-crm` Secret (created with
the demo password, dmopwd1, on first start, see init) and say who they are. They can:
  - search customers and open a 360 view: details, accounts, cards and
    loans, recent transactions, opportunities, tasks and a timeline;
  - log notes, calls, visits and emails against a customer;
  - give and complete follow-up tasks;
  - work a sales pipeline (new, contacted, application, approved, won, lost).

The online bank and branch kiosks post events to /api/ingest: on the same
cluster automatically, on other clusters once linked (a `dmobnk-crm-link`
Secret there, see the Connect page and ../../common/crm_feed.py): kiosk sign-ins,
cash and payments update the customer's record; a kiosk appointment
creates a prospect, an opportunity and a task; an online mortgage
application moves the pipeline; a card freeze creates a call-back task.

The customers and history are examples, made up per country (the cluster's
dmobnk.smclab.net/country label picks the locale). State is in crm-redis."""
import hashlib
import hmac
import html
import json
import os
import random
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/dmobnk")
import ingress_host  # noqa: E402  (mounted from the dmobnk-ingress-host ConfigMap)
from redis_lite import Redis  # noqa: E402  (the same)

L = json.load(open("/locale/locale.json", encoding="utf-8"))
T = L["t"]
K = L["kiosk"]
COUNTRY = L["country"]
r = Redis(os.environ.get("REDIS_HOST", "crm-redis"))

SECRET = "dmobnk-crm"
# The demo password a new dmobnk-crm Secret starts with (the same for all of
# DMOBNK's demo sign-ins). The ingest token stays random.
DEMO_PASSWORD = "dmopwd1"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = os.environ.get("POD_NAMESPACE", "dmobnk")
COOKIE = "dmobnk_crm"
SESSION_S = 10 * 3600
PASSWORD = ""        # set by init()
TOKEN = ""           # the ingest token linked apps send
_KEY = b""
_fails = {}

STAGES = ["new", "contacted", "application", "approved", "won", "lost"]
STAGE_NAMES = {"new": "New", "contacted": "Contacted", "application": "Application",
               "approved": "Approved", "won": "Won", "lost": "Lost"}
STAGE_ODDS = {"new": .1, "contacted": .25, "application": .5, "approved": .8}
# product: (label, value range, owning role)
PRODUCTS = {
    "mortgage": ("Mortgage", (120000, 450000), "mortgage"),
    "loan": ("Personal loan", (3000, 25000), "rm"),
    "card": ("Credit card", (1000, 8000), "rm"),
    "savings": ("Savings", (5000, 60000), "rm"),
    "invest": ("Investments", (10000, 120000), "rm"),
    "current": ("Current account", (500, 3000), "rm"),
    "business": ("Business banking", (10000, 150000), "business"),
}
TOPIC_PRODUCT = ["mortgage", "invest", "current", "business"]   # the kiosk's appointment topics, in order
KINDS = {"note": "Note", "call": "Call", "visit": "Visit", "email": "Email", "event": "Live"}

DATA = {
    "uk": {
        "staff": [("s1", "Hannah Price", "Relationship manager", "rm"), ("s2", "Daniel Okoro", "Mortgage adviser", "mortgage"),
                  ("s3", "Sophie Turner", "Branch manager", "manager"), ("s4", "Arjun Mehta", "Business adviser", "business")],
        "first": ["Oliver", "Amelia", "Harry", "Isla", "George", "Ava", "Noah", "Mia", "Jack", "Sophie", "Leo", "Freya",
                  "Arthur", "Lily", "Oscar", "Chloe", "Ravi", "Hannah", "Ewan", "Niamh"],
        "last": ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Evans", "Patel", "Khan", "Walker", "Wright",
                 "Roberts", "Thompson", "Clarke", "Lewis", "Campbell"],
        "cities": ["London", "Manchester", "Leeds", "Bristol", "Glasgow", "Cardiff", "Birmingham"],
        "phone": lambda n: f"07700 900{n:03d}",
    },
    "us": {
        "staff": [("s1", "Rachel Kim", "Relationship manager", "rm"), ("s2", "Marcus Johnson", "Mortgage adviser", "mortgage"),
                  ("s3", "Emily Carter", "Branch manager", "manager"), ("s4", "David Nguyen", "Business adviser", "business")],
        "first": ["James", "Olivia", "Liam", "Emma", "Ethan", "Ava", "Mason", "Sofia", "Lucas", "Isabella", "Logan", "Mia",
                  "Daniel", "Harper", "Carlos", "Aaliyah", "Noah", "Chloe"],
        "last": ["Johnson", "Williams", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Anderson", "Lee", "Thomas",
                 "Jackson", "White", "Harris", "Clark"],
        "cities": ["New York", "Chicago", "Austin", "Seattle", "Denver", "Boston", "Atlanta"],
        "phone": lambda n: f"(212) 555-01{n % 100:02d}",
    },
    "de": {
        "staff": [("s1", "Julia Becker", "Relationship manager", "rm"), ("s2", "Tobias Wagner", "Mortgage adviser", "mortgage"),
                  ("s3", "Sabine Koch", "Branch manager", "manager"), ("s4", "Murat Demir", "Business adviser", "business")],
        "first": ["Lukas", "Mia", "Leon", "Emma", "Finn", "Hannah", "Paul", "Sophia", "Jonas", "Lena", "Felix", "Marie",
                  "Elias", "Clara", "Mehmet", "Greta"],
        "last": ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Hoffmann", "Schulz",
                 "Koch", "Richter", "Yilmaz", "Wolf"],
        "cities": ["Berlin", "München", "Hamburg", "Köln", "Frankfurt", "Leipzig", "Stuttgart"],
        "phone": lambda n: f"030 23125 {n:03d}",
    },
    "sim": {
        "staff": [("s1", "Wizzle Pantsby", "Relationship manager", "rm"), ("s2", "Dooma Flunk", "Mortgage adviser", "mortgage"),
                  ("s3", "Sula Bobbaroo", "Branch manager", "manager"), ("s4", "Nib Tootle", "Business adviser", "business")],
        "first": ["Pib", "Zoozi", "Flimba", "Dabbo", "Snooti", "Garbo", "Lulla", "Wooble", "Mimbo", "Tazzle", "Nooby",
                  "Fizzi", "Bobbin", "Sarbo", "Quimby", "Zibba"],
        "last": ["Wobblesworth", "Flinkerton", "Pumpernoodle", "Gibbleton", "Snorkby", "Dabbleford", "Mooncrumb",
                 "Tinkerbuns", "Fluffington", "Blibber"],
        "cities": ["Gubbleton", "Wibble Bay", "Snoozeville", "Fizzbury", "Noodle Falls"],
        "phone": lambda n: f"555-{n:04d}",
    },
}
D = DATA.get(COUNTRY, DATA["uk"])
STAFF = {s[0]: {"id": s[0], "name": s[1], "role": s[2], "kind": s[3]} for s in D["staff"]}
BY_KIND = {s["kind"]: s["id"] for s in STAFF.values()}
TAGS = ["Prefers phone", "Prefers email", "Paperless", "Uses the app", "Moving house", "New baby",
        "Retiring soon", "Started a business", "Saving for a deposit"]
NOTES = [("call", "Called about a card limit increase. Sent the form."),
         ("email", "Emailed the new savings rates."),
         ("visit", "Came into the branch to update their address."),
         ("call", "Asked about remortgaging in the spring. Booked a call back."),
         ("note", "Annual review done: happy with current products."),
         ("visit", "Paid in a cheque and asked about the app."),
         ("email", "Sent a reminder about their fixed rate ending."),
         ("note", "Interested in an ISA next tax year.")]
TASK_TITLES = ["Call {first} about their mortgage renewal", "Send {first} the latest savings rates",
               "Check {first}'s card limit request", "Annual review with {first}",
               "Chase documents for {first}'s application", "Welcome call for {first}"]


def staff_for(product):
    return BY_KIND.get(PRODUCTS[product][2], "s1")


def jget(key):
    v = r("GET", key)
    return json.loads(v) if v else None


def jset(key, obj):
    r("SET", key, json.dumps(obj))


def now():
    return int(time.time())


# ---------------------------------------------------------------- demo data
def seed():
    """Makes this country's example customers, pipeline, tasks and history
    once (crm:seeded guards it, so every replica can call this)."""
    if not r("SET", "crm:seeded", COUNTRY, "NX"):
        return
    rnd = random.Random("dmobnk-crm-" + COUNTRY)
    t0 = now()
    custs = []
    for i, kc in enumerate(K["customers"]):   # the kiosk's demo customers, same ids and accounts
        custs.append({"id": kc["id"], "name": kc["name"], "segment": ["Premier", "Everyday", "Premier"][i % 3],
                      "accounts": [{"id": a["id"], "name": a["name"], "balance": a["balance"]} for a in kc["accounts"]],
                      "card": {"last4": kc["last4"]}})
    used = {c["name"] for c in custs}
    n = len(custs)
    while len(custs) < 40:
        name = f"{rnd.choice(D['first'])} {rnd.choice(D['last'])}"
        if name in used:
            continue
        used.add(name)
        n += 1
        seg = rnd.choices(["Everyday", "Premier", "Business"], [60, 25, 15])[0]
        accts = [{"id": f"c{n}-cur", "name": "Current account", "balance": rnd.randint(200, 6000)}]
        if rnd.random() < .5:
            accts.append({"id": f"c{n}-sav", "name": "Savings", "balance": rnd.randint(1000, 40000)})
        if seg == "Business":
            accts.append({"id": f"c{n}-biz", "name": "Business account", "balance": rnd.randint(5000, 90000)})
        custs.append({"id": f"c{n}", "name": name, "segment": seg, "accounts": accts,
                      "card": {"last4": f"{rnd.randint(1000, 9999)}"} if rnd.random() < .7 else None})
    for i, c in enumerate(custs):
        first, last = c["name"].split(" ", 1)
        c.update({
            "email": f"{first}.{last}".lower().replace(" ", "").replace("ü", "ue").replace("ö", "oe").replace("ä", "ae") + "@example.com",
            "phone": D["phone"](100 + i), "city": rnd.choice(D["cities"]), "since": rnd.randint(1998, 2025),
            "owner": "s4" if c["segment"] == "Business" else rnd.choice(["s1", "s1", "s3"]),
            "tags": rnd.sample(TAGS, rnd.randint(0, 2)), "loans": []})
        if c.get("card"):
            lim = rnd.choice([1500, 2500, 4000, 6000, 8000])
            c["card"].update({"limit": lim, "balance": round(lim * rnd.random() * .6, 2)})
        if rnd.random() < .3:
            c["loans"].append({"name": "Mortgage", "balance": rnd.randint(80, 350) * 1000,
                               "rate": round(rnd.uniform(3.4, 5.6), 2), "years": rnd.randint(5, 25)})
        if rnd.random() < .15:
            c["loans"].append({"name": "Personal loan", "balance": rnd.randint(2, 20) * 1000,
                               "rate": round(rnd.uniform(6.5, 11.9), 1), "years": rnd.randint(1, 5)})
        jset(f"crm:cust:{c['id']}", c)
        r("RPUSH", "crm:cust:ids", c["id"])
        cur = c["accounts"][0]["id"]
        for j, merchant in enumerate(K["seed_tx"]):
            r("RPUSH", f"crm:tx:{c['id']}", json.dumps({"t": t0 - (j + 1) * 43200, "acct": cur, "desc": merchant,
                                                        "amount": -rnd.randint(300, 9000) / 100}))
        for k in range(rnd.randint(1, 3)):
            kind, text = rnd.choice(NOTES)
            r("RPUSH", f"crm:act:{c['id']}", json.dumps({"t": t0 - rnd.randint(2, 120) * 86400 - k, "kind": kind,
                                                         "by": rnd.choice(list(STAFF)), "text": text}))
    for _ in range(16):
        c = rnd.choice(custs)
        prod = rnd.choices(list(PRODUCTS), [5, 3, 2, 3, 2, 1, 2])[0]
        lo, hi = PRODUCTS[prod][1]
        stage = rnd.choices(STAGES, [4, 4, 3, 2, 2, 1])[0]
        created = t0 - rnd.randint(1, 40) * 86400
        new_opp(c["id"], prod, round(rnd.uniform(lo, hi) / 500) * 500, staff_for(prod), stage=stage, t=created,
                closed=t0 - rnd.randint(0, 9) * 86400 if stage in ("won", "lost") else None)
    for k in range(10):
        c = rnd.choice(custs)
        new_task(c["id"], rnd.choice(TASK_TITLES).replace("{first}", c["name"].split()[0]),
                 (date.today() + timedelta(days=rnd.randint(-3, 7))).isoformat(), rnd.choice(list(STAFF)),
                 by=rnd.choice(list(STAFF)), done=k >= 8)


def new_opp(cid, product, value, owner, stage="new", t=None, closed=None, source=""):
    oid = f"o{r('INCR', 'crm:seq:opp')}"
    jset(f"crm:opp:{oid}", {"id": oid, "cid": cid, "product": product, "value": value, "owner": owner,
                            "stage": stage, "t": t or now(), "moved": t or now(), "closed": closed, "source": source})
    r("RPUSH", "crm:opp:ids", oid)
    return oid


def new_task(cid, title, due, owner, by="", done=False):
    tid = f"t{r('INCR', 'crm:seq:task')}"
    jset(f"crm:task:{tid}", {"id": tid, "cid": cid, "title": title, "due": due, "owner": owner,
                             "by": by, "done": done, "t": now()})
    r("RPUSH", "crm:task:ids", tid)
    return tid


def add_act(cid, kind, text, by):
    r("LPUSH", f"crm:act:{cid}", json.dumps({"t": now(), "kind": kind, "by": by, "text": text}))
    r("LTRIM", f"crm:act:{cid}", 0, 199)


def all_of(kind):
    ids = r("LRANGE", f"crm:{kind}:ids", 0, -1) or []
    if not ids:
        return []
    return [json.loads(v) for v in r("MGET", *[f"crm:{kind}:{i}" for i in ids]) if v]


def customers():
    return {c["id"]: c for c in all_of("cust")}


def total(c):
    return round(sum(a["balance"] for a in c["accounts"]), 2)


def summary(c):
    return {"id": c["id"], "name": c["name"], "segment": c["segment"], "city": c["city"],
            "owner": c["owner"], "total": total(c), "email": c["email"], "phone": c["phone"]}


# ---------------------------------------------------------------- live events
def feed_add(item):
    item["id"] = r("INCR", "crm:seq:feed")
    item["t"] = now()
    r("LPUSH", "crm:feed", json.dumps(item))
    r("LTRIM", "crm:feed", 0, 299)
    if item.get("app"):
        r("HSET", "crm:sources", f"{item['app']}|{item.get('where', '')}", item["t"])


def _s(v, n):
    return str(v if v is not None else "")[:n]


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def ingest(ev):
    """Applies one event from a linked bank or kiosk (crm_feed.py)."""
    typ, text = _s(ev.get("type"), 40), _s(ev.get("text"), 240)
    src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
    app, where = _s(src.get("app"), 20), _s(src.get("where"), 60)
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    cust = ev.get("customer") if isinstance(ev.get("customer"), dict) else {}
    by = where or app or "Linked app"
    cid = _s(cust.get("id"), 20)
    c = jget(f"crm:cust:{cid}") if cid else None
    if c:
        # Balances the kiosk reports after a transaction.
        changed = False
        for upd in data.get("balances") or []:
            if not isinstance(upd, dict):
                continue
            for a in c["accounts"]:
                if a["id"] == upd.get("id") and _num(upd.get("balance")) is not None:
                    a["balance"] = round(_num(upd["balance"]), 2)
                    changed = True
        if changed:
            jset(f"crm:cust:{cid}", c)
        for tx in data.get("tx") or []:
            if isinstance(tx, dict) and _num(tx.get("amount")) is not None:
                r("LPUSH", f"crm:tx:{cid}", json.dumps({"t": now(), "acct": _s(tx.get("acct"), 20),
                                                        "desc": _s(tx.get("desc"), 60), "amount": _num(tx["amount"])}))
                r("LTRIM", f"crm:tx:{cid}", 0, 49)
        add_act(cid, "event", text, by)
    if typ == "appointment":
        # A prospect booked an adviser at a kiosk: a new customer record, an
        # opportunity and a task for the adviser.
        topic = data.get("topic")
        product = TOPIC_PRODUCT[topic] if isinstance(topic, int) and 0 <= topic < len(TOPIC_PRODUCT) else "current"
        owner = staff_for(product)
        name = _s(data.get("name"), 30).strip() or "Walk-in"
        n = r("INCR", "crm:seq:cust")
        cid = f"n{n}"
        c = {"id": cid, "name": name, "segment": "Prospect", "accounts": [], "card": None, "loans": [],
             "email": "", "phone": "", "city": where, "since": date.today().year, "owner": owner,
             "tags": ["Booked at a kiosk"]}
        jset(f"crm:cust:{cid}", c)
        r("RPUSH", "crm:cust:ids", cid)
        add_act(cid, "event", text, by)
        lo, hi = PRODUCTS[product][1]
        new_opp(cid, product, round((lo + hi) / 2 / 500) * 500, owner, source="kiosk")
        new_task(cid, f"Appointment with {name}: {_s(data.get('topic_name'), 40)}, {_s(data.get('date'), 10)} {_s(data.get('time'), 5)}",
                 _s(data.get("date"), 10) or date.today().isoformat(), owner, by=by)
    elif typ == "mortgage_aip" and c:
        opps = [o for o in all_of("opp") if o["cid"] == cid and o["product"] == "mortgage" and o["stage"] not in ("won", "lost")]
        if opps:
            o = opps[0]
            if STAGES.index(o["stage"]) < STAGES.index("application"):
                o.update(stage="application", moved=now())
                jset(f"crm:opp:{o['id']}", o)
        else:
            new_opp(cid, "mortgage", 250000, staff_for("mortgage"), stage="application", source="online")
        new_task(cid, f"Follow up {c['name'].split()[0]}'s mortgage application",
                 (date.today() + timedelta(days=1)).isoformat(), staff_for("mortgage"), by=by)
    elif typ == "card_payment" and c and c.get("card") and _num(data.get("amount")) is not None:
        # A DMOSHP purchase on the customer's DMOBNK card (../../../dmoshp).
        c["card"]["balance"] = round((c["card"].get("balance") or 0) + _num(data["amount"]), 2)
        jset(f"crm:cust:{cid}", c)
    elif typ == "card_freeze" and c and data.get("frozen"):
        new_task(cid, f"Call {c['name'].split()[0]}: card frozen in online banking", date.today().isoformat(),
                 c["owner"], by=by)
    feed_add({"app": app, "where": where, "type": typ, "text": text, "cid": cid if c else None,
              "name": c["name"] if c else ""})


# ---------------------------------------------------------------- password and sign-in
def init():
    """Loads the sign-in password and ingest token from the dmobnk-crm Secret,
    creating it with random values if there isn't one."""
    global PASSWORD, TOKEN, _KEY
    if not os.path.exists(f"{SA}/token"):     # local testing outside Kubernetes
        PASSWORD = os.environ.get("CRM_PASSWORD", "")
        TOKEN = os.environ.get("CRM_TOKEN", "")
        _KEY = hashlib.sha256(b"dmobnk-crm:" + PASSWORD.encode()).digest()
        return
    import base64
    while True:
        try:
            try:
                sec = ingress_host.k8s("GET", f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
            except urllib.error.HTTPError as err:
                if err.code != 404:
                    raise
                try:
                    ingress_host.k8s("POST", f"/api/v1/namespaces/{NS}/secrets", {
                        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": SECRET, "labels": {"app.kubernetes.io/part-of": "dmobnk"}},
                        "stringData": {"password": DEMO_PASSWORD,
                                       "token": secrets.token_urlsafe(24)}})
                    print(f"created the {SECRET} Secret with the demo password and a new ingest token", flush=True)
                except urllib.error.HTTPError as err2:
                    if err2.code != 409:      # 409: another replica made it first
                        raise
                sec = ingress_host.k8s("GET", f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
            d = sec["data"]
            PASSWORD = base64.b64decode(d["password"]).decode().strip()
            TOKEN = base64.b64decode(d.get("token", "")).decode().strip()
            _KEY = hashlib.sha256(b"dmobnk-crm:" + PASSWORD.encode()).digest()
            return
        except Exception as err:
            print(f"couldn't load the CRM password: {err}", flush=True)
            time.sleep(5)


def _sign(payload):
    return hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()


def new_session(staff):
    payload = f"{staff}.{now() + SESSION_S}"
    return f"{payload}.{_sign(payload)}"


def session_staff(h):
    raw = ""
    for part in h.headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            raw = v
    try:
        staff, exp, sig = raw.split(".")
    except ValueError:
        return None
    if not PASSWORD or not hmac.compare_digest(sig, _sign(f"{staff}.{exp}")) or int(exp) < now() or staff not in STAFF:
        return None
    return staff


def set_cookie(h, value, max_age):
    https = h.headers.get("X-Forwarded-Proto", "") == "https"
    return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly;{' Secure;' if https else ''} SameSite=Strict"


def client_ip(h):
    return (h.headers.get("X-Forwarded-For", "").split(",")[0].strip() or h.client_address[0])[:64]


def locked_out(ip):
    recent = [t for t in _fails.get(ip, []) if t > time.time() - 300]
    _fails[ip] = recent
    return len(recent) >= 5


# ---------------------------------------------------------------- API
def api_get(path, q, me):
    if path == "me":
        return 200, {"me": STAFF[me], "staff": list(STAFF.values()), "stages": STAGES, "stage_names": STAGE_NAMES,
                     "products": {k: v[0] for k, v in PRODUCTS.items()}, "kinds": KINDS}
    if path == "dashboard":
        custs, opps, tasks = customers(), all_of("opp"), all_of("task")
        month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = date.today().isoformat()
        open_opps = [o for o in opps if o["stage"] not in ("won", "lost")]
        mine = sorted([t for t in tasks if t["owner"] == me and not t["done"]], key=lambda t: t["due"])[:8]
        for t in mine:
            t["name"] = custs.get(t["cid"], {}).get("name", "")
        closed = [o for o in opps if o["stage"] in ("won", "lost") and (o.get("closed") or 0) >= month]
        return 200, {
            "customers": sum(1 for c in custs.values() if c["segment"] != "Prospect"),
            "prospects": sum(1 for c in custs.values() if c["segment"] == "Prospect"),
            "open_value": sum(o["value"] for o in open_opps), "open_count": len(open_opps),
            "won_value": sum(o["value"] for o in closed if o["stage"] == "won"),
            "won_count": sum(1 for o in closed if o["stage"] == "won"), "closed_count": len(closed),
            "due_today": sum(1 for t in tasks if not t["done"] and t["due"] == today),
            "overdue": sum(1 for t in tasks if not t["done"] and t["due"] < today),
            "stages": [{"stage": s, "count": sum(1 for o in opps if o["stage"] == s),
                        "value": sum(o["value"] for o in opps if o["stage"] == s)} for s in STAGES],
            "my_tasks": mine, "today": today}
    if path == "feed":
        since = int(q.get("since", "0") or 0) if str(q.get("since", "0")).isdigit() else 0
        items = [json.loads(x) for x in r("LRANGE", "crm:feed", 0, 59)]
        return 200, {"items": [i for i in items if i["id"] > since]}
    if path == "customers":
        needle = q.get("q", "").strip().lower()[:60]
        seg = q.get("seg", "")
        opps = all_of("opp")
        out = []
        for c in customers().values():
            if seg and c["segment"] != seg:
                continue
            hay = " ".join([c["name"], c["email"], c["phone"], c["city"], c["id"]]).lower()
            if needle and needle not in hay:
                continue
            s = summary(c)
            s["open"] = sum(1 for o in opps if o["cid"] == c["id"] and o["stage"] not in ("won", "lost"))
            out.append(s)
        out.sort(key=lambda s: (s["segment"] != "Prospect", s["name"]))
        return 200, {"customers": out}
    if path == "customer":
        cid = q.get("id", "")[:20]
        c = jget(f"crm:cust:{cid}")
        if not c:
            return 404, {"error": "No such customer."}
        tx = [json.loads(x) for x in r("LRANGE", f"crm:tx:{cid}", 0, 11)]
        acts = [json.loads(x) for x in r("LRANGE", f"crm:act:{cid}", 0, 99)]
        acts.sort(key=lambda a: -a["t"])
        opps = sorted([o for o in all_of("opp") if o["cid"] == cid], key=lambda o: -o["t"])
        tasks = sorted([t for t in all_of("task") if t["cid"] == cid], key=lambda t: (t["done"], t["due"]))
        names = {a["id"]: a["name"] for a in c["accounts"]}
        for t in tx:
            t["acct_name"] = names.get(t.get("acct"), "")
        return 200, {"customer": c, "total": total(c), "tx": tx, "activity": acts, "opps": opps, "tasks": tasks}
    if path == "pipeline":
        custs = customers()
        opps = all_of("opp")
        for o in opps:
            o["name"] = custs.get(o["cid"], {}).get("name", "?")
        return 200, {"opps": opps, "odds": STAGE_ODDS}
    if path == "tasks":
        custs = customers()
        tasks = all_of("task")
        for t in tasks:
            t["name"] = custs.get(t["cid"], {}).get("name", "")
        tasks.sort(key=lambda t: (t["done"], t["due"]))
        return 200, {"tasks": tasks, "today": date.today().isoformat()}
    if path == "connect":
        srcs = r("HGETALL", "crm:sources") or []
        pairs = [(srcs[i], int(srcs[i + 1])) for i in range(0, len(srcs), 2)]
        return 200, {"token": TOKEN, "sources": [{"app": k.split("|", 1)[0], "where": k.split("|", 1)[1], "t": t}
                                                 for k, t in sorted(pairs, key=lambda p: -p[1])]}
    return 404, {"error": "not found"}


def api_post(path, body, me):
    custs = None
    if path == "note":
        cid, kind, text = _s(body.get("cid"), 20), body.get("kind"), _s(body.get("text"), 1000).strip()
        if not jget(f"crm:cust:{cid}") or kind not in ("note", "call", "visit", "email") or not text:
            return 400, {"error": "Write something first."}
        add_act(cid, kind, text, me)
        return 200, {"ok": True}
    if path == "opp":
        cid, product, value = _s(body.get("cid"), 20), body.get("product"), _num(body.get("value"))
        if not jget(f"crm:cust:{cid}") or product not in PRODUCTS or value is None or not 0 < value <= 10_000_000:
            return 400, {"error": "Pick a customer, a product and a value."}
        oid = new_opp(cid, product, round(value, 2), _s(body.get("owner"), 5) if body.get("owner") in STAFF else me)
        add_act(cid, "note", f"New {PRODUCTS[product][0].lower()} opportunity.", me)
        return 200, {"id": oid}
    if path == "opp/move":
        oid, stage = _s(body.get("id"), 12), body.get("stage")
        o = jget(f"crm:opp:{oid}")
        if not o or stage not in STAGES:
            return 400, {"error": "That opportunity has gone."}
        if o["stage"] != stage:
            o.update(stage=stage, moved=now(), closed=now() if stage in ("won", "lost") else None)
            jset(f"crm:opp:{oid}", o)
            add_act(o["cid"], "note", f"{PRODUCTS[o['product']][0]} opportunity moved to {STAGE_NAMES[stage]}.", me)
        return 200, {"ok": True}
    if path == "task":
        cid, title, due, owner = _s(body.get("cid"), 20), _s(body.get("title"), 140).strip(), _s(body.get("due"), 10), body.get("owner")
        try:
            date.fromisoformat(due)
        except ValueError:
            return 400, {"error": "Pick a due date."}
        if not title or owner not in STAFF or (cid and not jget(f"crm:cust:{cid}")):
            return 400, {"error": "Give the task a title and an owner."}
        return 200, {"id": new_task(cid, title, due, owner, by=me)}
    if path == "task/done":
        tid = _s(body.get("id"), 12)
        t = jget(f"crm:task:{tid}")
        if not t:
            return 400, {"error": "That task has gone."}
        t["done"] = bool(body.get("done"))
        jset(f"crm:task:{tid}", t)
        if t["done"] and t["cid"]:
            add_act(t["cid"], "note", f"Done: {t['title']}", me)
        return 200, {"ok": True}
    del custs
    return 404, {"error": "not found"}


# ---------------------------------------------------------------- pages
def e(s):
    return html.escape(str(s), quote=True)


LOGO = ('<svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><rect x="2" y="2" width="28" height="28" rx="9" fill="#fff"/>'
        '<rect x="8" y="17" width="4" height="7" rx="1" fill="#2453ff"/><rect x="14" y="12" width="4" height="12" rx="1" fill="#2453ff"/>'
        '<rect x="20" y="8" width="4" height="16" rx="1" fill="#e8890c"/></svg>')

CSS = """
:root{--ink:#0b1b3a;--ink2:#41506f;--muted:#6a7896;--line:#dbe2f0;--bg:#eef2fb;--card:#fff;--blue:#2453ff;--blue-soft:#e6ecff;
--green:#0e9f6e;--green-soft:#dcf5ea;--amber:#e8890c;--amber-soft:#fdf0dc;--red:#d03050;--red-soft:#fde4ea;--navy:#0b1b3a}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);font:15px/1.45 Inter,system-ui,sans-serif}
h1,h2,h3{font-family:'Space Grotesk',Inter,sans-serif;margin:0;letter-spacing:-.01em}h1{font-size:28px}h2{font-size:19px}h3{font-size:16px}
a{color:var(--blue)}button,input,select,textarea{font:inherit;color:inherit}.mono{font-family:'JetBrains Mono',monospace}.muted{color:var(--muted)}
.demo{background:#fdecd0;color:#5c3a00;font-size:13px}.demo div{max-width:1280px;margin:0 auto;padding:7px 20px;display:flex;gap:10px;align-items:center}
.demo b{background:#8a4b00;color:#fff;border-radius:6px;padding:1px 8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase}
header{background:var(--navy);color:#fff}header .wrap{max-width:1280px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.brand{display:flex;gap:10px;align-items:center;color:#fff;text-decoration:none}.brand b{font-family:'Space Grotesk';font-size:19px;letter-spacing:.02em}
.brand small{display:block;color:#aab6d3;font-size:12px}nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:#c9d3ea;text-decoration:none;padding:7px 12px;border-radius:9px;font-weight:600;font-size:14px}nav a:hover{background:#ffffff14;color:#fff}
nav a.on{background:#fff;color:var(--navy)}.who{margin-left:auto;display:flex;gap:10px;align-items:center;font-size:13px;color:#c9d3ea}
.who b{color:#fff}.who button{background:#ffffff1c;border:0;color:#fff;border-radius:9px;padding:6px 12px;cursor:pointer;font-weight:600}
main{max-width:1280px;margin:0 auto;padding:22px 20px 60px;display:flex;flex-direction:column;gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.grid{display:grid;gap:18px}.g4{grid-template-columns:repeat(4,1fr)}.g2{grid-template-columns:1.4fr 1fr}.g3{grid-template-columns:1fr 1fr 1fr}
@media(max-width:900px){.g4{grid-template-columns:1fr 1fr}.g2,.g3{grid-template-columns:1fr}}
.kpi .k{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}.kpi .v{font-family:'Space Grotesk';font-size:28px;font-weight:700;margin-top:4px}
.kpi .s{font-size:13px;color:var(--muted)}.hd{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.btn{border:0;border-radius:10px;padding:9px 16px;font-weight:600;cursor:pointer;background:var(--blue);color:#fff}.btn:hover{filter:brightness(1.08)}
.btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}.btn.sm{padding:5px 10px;font-size:13px;border-radius:8px}
.btn.green{background:var(--green)}.btn.red{background:var(--red)}.btn:disabled{opacity:.5;cursor:default}
input,select,textarea{border:1px solid var(--line);border-radius:10px;padding:9px 12px;background:#fff;min-width:0}textarea{width:100%;min-height:70px;resize:vertical}
input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:-1px}
table{width:100%;border-collapse:collapse}th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid #edf1f8;vertical-align:top}tr.click{cursor:pointer}tr.click:hover td{background:#f6f8fd}.num{text-align:right;font-variant-numeric:tabular-nums}
.tw{overflow-x:auto}.badge{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;background:var(--blue-soft);color:var(--blue);white-space:nowrap}
.b-Premier{background:#efe5ff;color:#6b2fd6}.b-Business{background:var(--amber-soft);color:#9a5200}.b-Prospect{background:var(--green-soft);color:#0a6b4b}.b-Everyday{background:#e9eef7;color:#41506f}
.b-kiosk{background:var(--amber-soft);color:#9a5200}.b-bank{background:var(--blue-soft);color:var(--blue)}.b-shop{background:#e3f6ec;color:#11724a}.b-overdue{background:var(--red-soft);color:var(--red)}.b-today{background:var(--amber-soft);color:#9a5200}
.feed{display:flex;flex-direction:column;max-height:430px;overflow:auto}.feed .it{display:flex;gap:10px;padding:10px 4px;border-bottom:1px solid #edf1f8;align-items:flex-start}
.feed .it.new{animation:flash 2.4s ease}@keyframes flash{0%{background:#fff4d6}100%{background:transparent}}
.feed .t{font-size:12px;color:var(--muted);white-space:nowrap;min-width:46px}.empty{color:var(--muted);padding:18px 4px;text-align:center}
.tasks .it{display:flex;gap:10px;align-items:flex-start;padding:9px 2px;border-bottom:1px solid #edf1f8}.tasks .it.done .ti{text-decoration:line-through;color:var(--muted)}
.tasks input[type=checkbox]{width:18px;height:18px;margin-top:2px;accent-color:var(--green)}
.board{display:grid;grid-template-columns:repeat(6,minmax(190px,1fr));gap:12px;overflow-x:auto;padding-bottom:6px}
.col{background:#e4eaf6;border-radius:14px;padding:10px;min-height:200px;display:flex;flex-direction:column;gap:8px}.col.drop{outline:2px dashed var(--blue)}
.col h3{font-size:14px;display:flex;justify-content:space-between}.col .sum{font-size:12px;color:var(--muted);margin-top:-4px}
.opp{background:#fff;border-radius:11px;padding:10px;border:1px solid var(--line);cursor:grab;display:flex;flex-direction:column;gap:3px}
.opp .v{font-family:'Space Grotesk';font-weight:700;font-size:17px}.opp .mv{display:flex;gap:4px;margin-top:4px}.opp .mv button{flex:1;padding:3px 0;border:1px solid var(--line);background:#f6f8fd;border-radius:7px;cursor:pointer;font-size:12px}
.opp .mv button:hover{background:var(--blue-soft)}.opp.live{border-color:var(--amber);box-shadow:0 0 0 2px var(--amber-soft)}
.prof{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.av{width:58px;height:58px;border-radius:16px;background:var(--navy);color:#fff;display:grid;place-items:center;font-family:'Space Grotesk';font-size:22px;font-weight:700}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px 18px;margin-top:14px}.facts div span{display:block;font-size:12px;color:var(--muted)}
.prods{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}.prod{border:1px solid var(--line);border-radius:12px;padding:12px}
.prod .v{font-family:'Space Grotesk';font-size:20px;font-weight:700}.prod.dark{background:var(--navy);color:#fff;border-color:var(--navy)}.prod.dark .muted{color:#aab6d3}
.tl{display:flex;flex-direction:column}.tl .it{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:10px 0;border-bottom:1px solid #edf1f8}
.ic{width:28px;height:28px;border-radius:9px;display:grid;place-items:center;font-size:13px;font-weight:700;background:var(--blue-soft);color:var(--blue)}
.ic.call{background:var(--green-soft);color:var(--green)}.ic.visit{background:#efe5ff;color:#6b2fd6}.ic.email{background:#e9eef7;color:#41506f}.ic.event{background:var(--amber-soft);color:#9a5200}
.seg{display:flex;gap:6px;flex-wrap:wrap}.seg button{border:1px solid var(--line);background:#fff;border-radius:999px;padding:5px 12px;cursor:pointer;font-weight:600;font-size:13px}.seg button.on{background:var(--navy);color:#fff;border-color:var(--navy)}
.bar{height:8px;border-radius:9px;background:#e4eaf6;overflow:hidden}.bar i{display:block;height:100%;background:var(--blue)}
.stg{display:grid;grid-template-columns:110px 1fr 90px;gap:10px;align-items:center;font-size:14px;padding:5px 0}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--navy);color:#fff;padding:10px 18px;border-radius:12px;font-weight:600;z-index:9;box-shadow:0 8px 30px #0b1b3a40}
.modal{position:fixed;inset:0;background:#0b1b3a80;display:grid;place-items:center;padding:16px;z-index:8}.modal .card{width:min(460px,100%);display:flex;flex-direction:column;gap:12px}
.modal label{display:flex;flex-direction:column;gap:5px;font-size:13px;font-weight:600;color:var(--ink2)}
code.blk{display:block;background:var(--navy);color:#dfe6f7;border-radius:12px;padding:14px;white-space:pre-wrap;word-break:break-all;font-family:'JetBrains Mono',monospace;font-size:13px}
.credit{text-align:center;font-size:12px;color:var(--muted);padding:0 16px 22px}
.login{min-height:calc(100vh - 80px);display:grid;place-items:center;padding:16px}.login form{width:min(400px,100%);display:flex;flex-direction:column;gap:14px}
.login label{display:flex;flex-direction:column;gap:6px;font-weight:600;font-size:14px}.err{color:var(--red);font-weight:600;font-size:14px}
"""


def doc(title, body, js=""):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{e(title)}</title>
<link rel="icon" href="data:image/svg+xml,{urllib.parse.quote(LOGO)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap">
<style>{CSS}</style></head><body>
<div class="demo" role="note"><div><b>{e(T['demo_label'])}</b><span>{e(T['demo_banner'])}</span></div></div>
{body}<footer class="credit">Created By Steve McBride</footer>{f'<script>{js}</script>' if js else ''}</body></html>"""


def login_page(err=""):
    msg = {"wrong": "That password isn't right.", "locked": "Too many tries. Wait five minutes and try again.",
           "staff": "Choose who you are."}.get(err, "")
    if not PASSWORD:
        inner = '<p class="muted">The CRM is still starting. Try again in a moment.</p>'
    else:
        opts = "".join(f'<option value="{s["id"]}">{e(s["name"])} · {e(s["role"])}</option>' for s in STAFF.values())
        inner = f"""<label>You are<select name="staff">{opts}</select></label>
<label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label>
{f'<p class="err" role="alert">{e(msg)}</p>' if msg else ''}
<button class="btn" type="submit">Sign in</button>
<p class="muted" style="font-size:13px;margin:0">It's in this cluster's <span class="mono">dmobnk-crm</span> Secret.</p>"""
    return doc("Sign in · DMOBNK CRM", f"""<main class="login"><form class="card" method="post" action="/login">
<div class="row">{LOGO.replace('fill="#fff"', 'fill="#0b1b3a"', 1)}<div><h1 style="font-size:24px">DMOBNK CRM</h1><span class="muted">Customers and sales, for staff</span></div></div>
{inner}</form></main>""")


APP_JS = r"""
const $ = id => document.getElementById(id);
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = text; return n; }
function num(x, d) { const m = CFG.money; const neg = x < 0; x = Math.abs(x); let [i, f] = x.toFixed(d).split('.'); i = i.replace(/\B(?=(\d{3})+(?!\d))/g, m.group); return (neg ? '-' : '') + (f ? i + m.dec + f : i); }
function money(x, d) { const m = CFG.money; const s = num(x, d === undefined ? 2 : d); return m.pos === 'pre' ? (x < 0 ? '-' : '') + m.symbol + (m.space ? ' ' : '') + s.replace('-', '') : s + (m.space ? ' ' : '') + m.symbol; }
function short(x) { return x >= 1e6 ? money(x / 1e6, 1).replace(/(\d)$/, '$1') + 'm' : x >= 1e4 ? money(x / 1e3, 0) + 'k' : money(x, 0); }
function when(t) { const d = new Date(t * 1000), n = new Date(); return d.toDateString() === n.toDateString() ? d.toLocaleTimeString(CFG.lang, {hour: '2-digit', minute: '2-digit'}) : d.toLocaleDateString(CFG.lang, {day: 'numeric', month: 'short'}); }
function fullWhen(t) { return new Date(t * 1000).toLocaleString(CFG.lang, {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'}); }
function day(iso) { return new Date(iso + 'T12:00:00').toLocaleDateString(CFG.lang, {weekday: 'short', day: 'numeric', month: 'short'}); }
function ago(t) { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? 'just now' : s < 3600 ? Math.floor(s / 60) + ' min ago' : s < 86400 ? Math.floor(s / 3600) + ' h ago' : Math.floor(s / 86400) + ' days ago'; }
function initials(n) { return n.split(/\s+/).map(w => w[0] || '').join('').slice(0, 2).toUpperCase(); }
function toast(msg) { const t = el('div', 'toast', msg); document.body.append(t); setTimeout(() => t.remove(), 2600); }
async function api(path, body) {
  const opts = body === undefined ? {cache: 'no-store'} : {method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json', 'X-CRM': '1'}, body: JSON.stringify(body)};
  const resp = await fetch('/api/' + path, opts);
  if (resp.status === 401) { location.href = '/'; throw new Error('signed out'); }
  let data = {}; try { data = await resp.json(); } catch (err) {}
  if (!resp.ok) throw new Error(data.error || 'Something went wrong. Try again.');
  return data;
}
let META = null, view = null;
const staffName = id => (META.staff.find(s => s.id === id) || {name: id}).name;
const prodName = p => META.products[p] || p;
function seg(s) { return el('span', 'badge b-' + s, s); }
const SRC = {kiosk: ['kiosk', 'Kiosk'], bank: ['bank', 'Online bank'], shop: ['shop', 'DMOSHP online'], sok: ['shop', 'DMOSHP store']};
function srcBadge(app) { const s = SRC[app] || ['bank', app || 'Live']; return el('span', 'badge b-' + s[0], s[1]); }
function dueBadge(t, today) { if (t.done) return el('span', 'badge', 'Done'); if (t.due < today) return el('span', 'badge b-overdue', 'Overdue · ' + day(t.due)); if (t.due === today) return el('span', 'badge b-today', 'Today'); return el('span', 'badge', day(t.due)); }
function custLink(cid, name) { const a = el('a', '', name || '—'); if (cid) a.href = '#/c/' + encodeURIComponent(cid); return a; }

function modal(title, fields, submit, onDone) {
  const wrap = el('div', 'modal'), card = el('form', 'card'); card.append(el('h2', '', title));
  const inputs = {};
  for (const f of fields) {
    const lab = el('label', '', f.label); let inp;
    if (f.options) { inp = el('select'); for (const [v, t] of f.options) { const o = el('option', '', t); o.value = v; inp.append(o); } }
    else { inp = el('input'); inp.type = f.type || 'text'; if (f.step) inp.step = f.step; if (f.min !== undefined) inp.min = f.min; }
    if (f.value !== undefined) inp.value = f.value; inp.required = true; inputs[f.name] = inp; lab.append(inp); card.append(lab);
  }
  const err = el('p', 'err'); err.setAttribute('role', 'alert'); const row = el('div', 'row');
  const ok = el('button', 'btn', submit); ok.type = 'submit'; const cancel = el('button', 'btn ghost', 'Cancel'); cancel.type = 'button';
  cancel.onclick = () => wrap.remove(); row.append(ok, cancel); card.append(err, row); wrap.append(card);
  wrap.addEventListener('click', ev => { if (ev.target === wrap) wrap.remove(); });
  card.onsubmit = async ev => { ev.preventDefault(); ok.disabled = true; const vals = {}; for (const k in inputs) vals[k] = inputs[k].value;
    try { await onDone(vals); wrap.remove(); } catch (e) { err.textContent = e.message; ok.disabled = false; } };
  document.body.append(wrap); (card.querySelector('input,select') || ok).focus();
}
function addTask(cid, name) {
  const today = new Date(); const iso = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  modal('New task' + (name ? ' for ' + name : ''), [
    {name: 'title', label: 'What needs doing'},
    {name: 'due', label: 'Due', type: 'date', value: iso},
    {name: 'owner', label: 'For', options: META.staff.map(s => [s.id, s.name + ' · ' + s.role]), value: META.me.id}],
    'Add task', async v => { await api('task', {cid: cid || '', title: v.title, due: v.due, owner: v.owner}); toast('Task added'); route(); });
}
async function addOpp(cid, name) {
  let options = null;
  if (!cid) { const d = await api('customers'); options = d.customers.map(c => [c.id, c.name + ' · ' + c.segment]); }
  modal('New opportunity' + (name ? ' for ' + name : ''), [
    ...(cid ? [] : [{name: 'cid', label: 'Customer', options}]),
    {name: 'product', label: 'Product', options: Object.entries(META.products)},
    {name: 'value', label: 'Value (' + CFG.money.symbol + ')', type: 'number', min: 1, step: 'any'},
    {name: 'owner', label: 'Owner', options: META.staff.map(s => [s.id, s.name]), value: META.me.id}],
    'Add opportunity', async v => { await api('opp', {cid: cid || v.cid, product: v.product, value: parseFloat(v.value), owner: v.owner}); toast('Opportunity added'); route(); });
}

// ------------------------------------------------------------ dashboard
let feedSince = 0, feedTimer = null;
function feedItem(i, fresh) {
  const it = el('div', 'it' + (fresh ? ' new' : '')); it.append(el('span', 't', when(i.t)));
  const body = el('div'); const top = el('div', 'row'); top.style.gap = '6px'; top.append(srcBadge(i.app));
  if (i.where) top.append(el('span', 'muted', i.where)); body.append(top);
  const line = el('div'); if (i.cid) { line.append(custLink(i.cid, i.name), document.createTextNode(': ')); } line.append(document.createTextNode(i.text));
  body.append(line); it.append(body); return it;
}
async function pollFeed(box, first) {
  try {
    const d = await api('feed?since=' + feedSince);
    if (d.items.length) {
      box.querySelector('.empty') && box.querySelector('.empty').remove();
      const items = d.items.slice().reverse();
      for (const i of items) box.prepend(feedItem(i, !first));
      feedSince = Math.max(feedSince, ...d.items.map(i => i.id));
      while (box.children.length > 60) box.lastChild.remove();
      if (!first && view === 'dash') refreshKpis();
    }
  } catch (e) {}
}
let kpiBox = null;
async function refreshKpis() {
  const d = await api('dashboard'); if (!kpiBox) return; kpiBox.replaceChildren();
  const k = (lbl, v, s) => { const c = el('div', 'card kpi'); c.append(el('div', 'k', lbl), el('div', 'v', v), el('div', 's', s)); kpiBox.append(c); };
  k('Customers', d.customers, d.prospects === 1 ? '1 prospect' : d.prospects + ' prospects');
  k('Open pipeline', short(d.open_value), d.open_count === 1 ? '1 opportunity' : d.open_count + ' opportunities');
  k('Won this month', short(d.won_value), d.closed_count ? Math.round(100 * d.won_count / d.closed_count) + '% win rate' : 'nothing closed yet');
  k('Tasks', d.due_today + ' due today', d.overdue ? d.overdue + ' overdue' : 'none overdue');
  return d;
}
async function dash(root) {
  view = 'dash'; feedSince = 0;
  kpiBox = el('div', 'grid g4'); root.append(kpiBox);
  const g = el('div', 'grid g2'); root.append(g);
  const live = el('div', 'card'); const lh = el('div', 'hd'); lh.append(el('h2', '', 'Live from banks and kiosks'), el('a', '', 'Connect more'));
  lh.lastChild.href = '#/connect'; live.append(lh);
  const box = el('div', 'feed'); box.append(el('div', 'empty', 'Nothing yet. Sign in at a linked kiosk, book an appointment or apply for a mortgage online, and it shows up here.'));
  live.append(box); g.append(live);
  const side = el('div', 'grid'); g.append(side);
  const d = await refreshKpis();
  const mt = el('div', 'card'); const mh = el('div', 'hd'); mh.append(el('h2', '', 'My tasks'), el('a', '', 'All tasks')); mh.lastChild.href = '#/tasks'; mt.append(mh);
  const tl = el('div', 'tasks'); mt.append(tl);
  if (!d.my_tasks.length) tl.append(el('div', 'empty', 'Nothing open. Nice.'));
  for (const t of d.my_tasks) tl.append(taskRow(t, d.today));
  side.append(mt);
  const pc = el('div', 'card'); const ph = el('div', 'hd'); ph.append(el('h2', '', 'Pipeline'), el('a', '', 'Open board')); ph.lastChild.href = '#/pipeline'; pc.append(ph);
  const maxv = Math.max(1, ...d.stages.map(s => s.value));
  for (const s of d.stages) { const r = el('div', 'stg'); r.append(el('span', '', META.stage_names[s.stage] + ' (' + s.count + ')'));
    const b = el('div', 'bar'); const i = el('i'); i.style.width = (100 * s.value / maxv) + '%'; if (s.stage === 'won') i.style.background = 'var(--green)'; if (s.stage === 'lost') i.style.background = '#a7b2c8'; b.append(i); r.append(b, el('span', 'num', short(s.value))); pc.append(r); }
  side.append(pc);
  await pollFeed(box, true);
  feedTimer = setInterval(() => pollFeed(box, false), 3000);
}

// ------------------------------------------------------------ customers
let custQ = '', custSeg = '';
async function custs(root) {
  view = 'custs';
  const card = el('div', 'card'); const hd = el('div', 'hd'); hd.append(el('h1', '', 'Customers')); card.append(hd);
  const bar = el('div', 'row'); const q = el('input'); q.type = 'search'; q.placeholder = 'Search by name, email, phone or city'; q.value = custQ; q.style.flex = '1 1 260px';
  const segs = el('div', 'seg');
  for (const [v, t] of [['', 'All'], ['Everyday', 'Everyday'], ['Premier', 'Premier'], ['Business', 'Business'], ['Prospect', 'Prospects']]) {
    const b = el('button', v === custSeg ? 'on' : '', t); b.type = 'button'; b.onclick = () => { custSeg = v; load(); [...segs.children].forEach(x => x.classList.toggle('on', x === b)); }; segs.append(b); }
  bar.append(q, segs); card.append(bar);
  const tw = el('div', 'tw'); tw.style.marginTop = '12px'; const table = el('table'); tw.append(table); card.append(tw); root.append(card);
  let timer = null; q.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { custQ = q.value; load(); }, 180); };
  async function load() {
    const d = await api('customers?q=' + encodeURIComponent(custQ) + '&seg=' + encodeURIComponent(custSeg));
    table.replaceChildren(); const h = el('tr'); for (const t of ['Customer', 'Segment', 'Contact', 'City', 'Owner', 'Open deals', 'Balances']) h.append(el('th', t === 'Balances' || t === 'Open deals' ? 'num' : '', t)); table.append(h);
    if (!d.customers.length) { const r = el('tr'); const c = el('td', 'empty', 'No customers match.'); c.colSpan = 7; r.append(c); table.append(r); }
    for (const c of d.customers) { const r = el('tr', 'click'); r.onclick = () => location.hash = '#/c/' + encodeURIComponent(c.id);
      const n = el('td'); n.append(el('b', '', c.name)); const sg = el('td'); sg.append(seg(c.segment));
      const ct = el('td', 'muted'); ct.append(el('div', '', c.email || '—'), el('div', '', c.phone));
      r.append(n, sg, ct, el('td', '', c.city), el('td', '', staffName(c.owner)), el('td', 'num', c.open || ''), el('td', 'num', c.segment === 'Prospect' ? '—' : money(c.total))); table.append(r); }
  }
  await load(); q.focus();
}

// ------------------------------------------------------------ customer 360
async function cust(root, cid) {
  view = 'cust';
  const d = await api('customer?id=' + encodeURIComponent(cid)); const c = d.customer;
  const head = el('div', 'card'); const prof = el('div', 'prof'); prof.append(el('div', 'av', initials(c.name)));
  const nm = el('div'); nm.append(el('h1', '', c.name)); const sub = el('div', 'row'); sub.style.gap = '8px'; sub.append(seg(c.segment));
  if (c.segment !== 'Prospect') sub.append(el('span', 'muted', 'Customer since ' + c.since)); for (const t of c.tags || []) sub.append(el('span', 'badge', t));
  nm.append(sub); prof.append(nm);
  const acts = el('div', 'row'); acts.style.marginLeft = 'auto';
  const b1 = el('button', 'btn ghost', 'New task'); b1.onclick = () => addTask(c.id, c.name);
  const b2 = el('button', 'btn', 'New opportunity'); b2.onclick = () => addOpp(c.id, c.name); acts.append(b1, b2); prof.append(acts); head.append(prof);
  const facts = el('div', 'facts'); const fact = (k, v) => { const f = el('div'); f.append(el('span', '', k), el('b', '', v || '—')); facts.append(f); };
  fact('Email', c.email); fact('Phone', c.phone); fact(c.segment === 'Prospect' ? 'Where they booked' : 'City', c.city); fact('Relationship owner', staffName(c.owner)); fact('Customer ID', c.id);
  head.append(facts); root.append(head);

  if (c.segment !== 'Prospect') {
    const pc = el('div', 'card'); const ph = el('div', 'hd'); ph.append(el('h2', '', 'Products'), el('span', 'muted', 'Total balances ' + money(d.total))); pc.append(ph);
    const prods = el('div', 'prods');
    for (const a of c.accounts) { const p = el('div', 'prod dark'); p.append(el('div', 'muted', a.name), el('div', 'v', money(a.balance)), el('div', 'muted mono', a.id)); prods.append(p); }
    if (c.card) { const p = el('div', 'prod'); p.append(el('div', 'muted', 'Credit card •••• ' + c.card.last4), el('div', 'v', money(c.card.balance || 0)), el('div', 'muted', 'of ' + money(c.card.limit || 0, 0) + ' limit')); prods.append(p); }
    for (const l of c.loans || []) { const p = el('div', 'prod'); p.append(el('div', 'muted', l.name), el('div', 'v', money(l.balance, 0)), el('div', 'muted', l.rate + '% · ' + l.years + ' years left')); prods.append(p); }
    pc.append(prods); root.append(pc);
  }
  const g = el('div', 'grid g2'); root.append(g);
  const left = el('div', 'grid'); const right = el('div', 'grid'); g.append(left, right);

  const tlc = el('div', 'card'); tlc.append(el('h2', '', 'Timeline'));
  const form = el('form'); form.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin:12px 0';
  const kind = el('select'); for (const k of ['note', 'call', 'visit', 'email']) { const o = el('option', '', META.kinds[k]); o.value = k; kind.append(o); }
  const ta = el('textarea'); ta.placeholder = 'Log a note, call, visit or email with ' + c.name.split(' ')[0] + '…'; ta.maxLength = 1000;
  const fr = el('div', 'row'); const save = el('button', 'btn', 'Add to timeline'); save.type = 'submit'; fr.append(kind, save); form.append(ta, fr);
  form.onsubmit = async ev => { ev.preventDefault(); try { await api('note', {cid: c.id, kind: kind.value, text: ta.value}); toast('Added'); route(); } catch (e) { toast(e.message); } };
  tlc.append(form);
  const tl = el('div', 'tl');
  if (!d.activity.length) tl.append(el('div', 'empty', 'No history yet.'));
  for (const a of d.activity) { const it = el('div', 'it'); const ic = el('div', 'ic ' + a.kind, {note: '✎', call: '☎', visit: '⌂', email: '@', event: '⚡'}[a.kind] || '•'); ic.title = META.kinds[a.kind] || a.kind;
    const b = el('div'); const top = el('div', 'muted'); top.style.fontSize = '12px'; top.textContent = (META.kinds[a.kind] || a.kind) + ' · ' + (META.staff.find(s => s.id === a.by) ? staffName(a.by) : a.by) + ' · ' + fullWhen(a.t);
    b.append(top, el('div', '', a.text)); it.append(ic, b); tl.append(it); }
  tlc.append(tl); left.append(tlc);

  const oc = el('div', 'card'); oc.append(el('h2', '', 'Opportunities'));
  if (!d.opps.length) oc.append(el('div', 'empty', 'None yet.'));
  for (const o of d.opps) { const r = el('div', 'row'); r.style.cssText = 'justify-content:space-between;padding:9px 0;border-bottom:1px solid #edf1f8';
    const l = el('div'); l.append(el('b', '', prodName(o.product)), el('div', 'muted', staffName(o.owner) + ' · opened ' + when(o.t)));
    const rt = el('div', 'row'); rt.append(el('span', 'badge' + (o.stage === 'won' ? ' b-Prospect' : o.stage === 'lost' ? ' b-Everyday' : ''), META.stage_names[o.stage]), el('b', '', money(o.value, 0)));
    r.append(l, rt); oc.append(r); }
  right.append(oc);
  const tc = el('div', 'card'); tc.append(el('h2', '', 'Tasks')); const tlist = el('div', 'tasks'); tc.append(tlist);
  const today = new Date().toISOString().slice(0, 10);
  if (!d.tasks.length) tlist.append(el('div', 'empty', 'None.'));
  for (const t of d.tasks) tlist.append(taskRow(t, today, true));
  right.append(tc);
  if (d.tx.length) {
    const xc = el('div', 'card'); xc.append(el('h2', '', 'Recent transactions')); const t = el('table');
    for (const x of d.tx) { const r = el('tr'); const a = el('td'); a.append(el('div', '', x.desc), el('div', 'muted', when(x.t) + (x.acct_name ? ' · ' + x.acct_name : ''))); r.append(a, el('td', 'num', money(x.amount))); t.append(r); }
    xc.append(t); right.append(xc);
  }
}

// ------------------------------------------------------------ tasks
function taskRow(t, today, noName) {
  const it = el('div', 'it' + (t.done ? ' done' : '')); const cb = el('input'); cb.type = 'checkbox'; cb.checked = t.done; cb.setAttribute('aria-label', 'Done');
  cb.onchange = async () => { try { await api('task/done', {id: t.id, done: cb.checked}); it.classList.toggle('done', cb.checked); toast(cb.checked ? 'Done' : 'Reopened'); } catch (e) { cb.checked = !cb.checked; toast(e.message); } };
  const b = el('div'); b.style.flex = '1'; b.append(el('div', 'ti', t.title));
  const meta = el('div', 'row'); meta.style.cssText = 'gap:6px;font-size:13px;margin-top:2px'; meta.append(dueBadge(t, today), el('span', 'muted', staffName(t.owner)));
  if (!noName && t.cid) meta.append(custLink(t.cid, t.name)); b.append(meta); it.append(cb, b); return it;
}
let taskFilter = 'mine';
async function tasks(root) {
  view = 'tasks';
  const card = el('div', 'card'); const hd = el('div', 'hd'); hd.append(el('h1', '', 'Tasks')); const nb = el('button', 'btn', 'New task'); nb.onclick = () => addTask('', ''); hd.append(nb); card.append(hd);
  const segs = el('div', 'seg'); const list = el('div', 'tasks'); list.style.marginTop = '12px';
  const d = await api('tasks');
  const filters = {mine: ['Mine', t => !t.done && t.owner === META.me.id], all: ['All open', t => !t.done], overdue: ['Overdue', t => !t.done && t.due < d.today], done: ['Done', t => t.done]};
  function draw() { list.replaceChildren(); const shown = d.tasks.filter(filters[taskFilter][1]); if (!shown.length) list.append(el('div', 'empty', 'Nothing here.')); for (const t of shown) list.append(taskRow(t, d.today)); }
  for (const [k, [lbl, f]] of Object.entries(filters)) { const b = el('button', k === taskFilter ? 'on' : '', lbl + ' (' + d.tasks.filter(f).length + ')'); b.type = 'button';
    b.onclick = () => { taskFilter = k; [...segs.children].forEach(x => x.classList.toggle('on', x === b)); draw(); }; segs.append(b); }
  card.append(segs, list); root.append(card); draw();
}

// ------------------------------------------------------------ pipeline
async function pipeline(root) {
  view = 'pipe';
  const d = await api('pipeline');
  const top = el('div', 'grid g4'); root.append(top);
  const open = d.opps.filter(o => !['won', 'lost'].includes(o.stage));
  const weighted = open.reduce((s, o) => s + o.value * (d.odds[o.stage] || 0), 0);
  const won = d.opps.filter(o => o.stage === 'won'), lost = d.opps.filter(o => o.stage === 'lost');
  const k = (lbl, v, s) => { const c = el('div', 'card kpi'); c.append(el('div', 'k', lbl), el('div', 'v', v), el('div', 's', s)); top.append(c); };
  k('Open pipeline', short(open.reduce((s, o) => s + o.value, 0)), open.length + ' opportunities');
  k('Weighted forecast', short(weighted), 'value × chance at each stage');
  k('Won', short(won.reduce((s, o) => s + o.value, 0)), won.length + ' deals');
  k('Win rate', won.length + lost.length ? Math.round(100 * won.length / (won.length + lost.length)) + '%' : '—', won.length + ' won, ' + lost.length + ' lost');
  const card = el('div', 'card'); const hd = el('div', 'hd'); hd.append(el('h1', '', 'Pipeline'), el('span', 'muted', 'Drag a card, or use the arrows'));
  const nb = el('button', 'btn', 'New opportunity'); nb.onclick = () => addOpp('', ''); hd.append(nb); card.append(hd);
  const board = el('div', 'board'); card.append(board); root.append(card);
  const move = async (o, stage) => { try { await api('opp/move', {id: o.id, stage}); toast(prodName(o.product) + ' → ' + META.stage_names[stage]); route(); } catch (e) { toast(e.message); } };
  for (const s of META.stages) {
    const col = el('div', 'col'); const items = d.opps.filter(o => o.stage === s).sort((a, b) => b.moved - a.moved);
    const h = el('h3'); h.append(el('span', '', META.stage_names[s]), el('span', 'muted', items.length)); col.append(h, el('div', 'sum', short(items.reduce((x, o) => x + o.value, 0))));
    col.ondragover = ev => { ev.preventDefault(); col.classList.add('drop'); }; col.ondragleave = () => col.classList.remove('drop');
    col.ondrop = ev => { ev.preventDefault(); col.classList.remove('drop'); const o = d.opps.find(x => x.id === ev.dataTransfer.getData('text/plain')); if (o && o.stage !== s) move(o, s); };
    for (const o of items) {
      const c = el('div', 'opp' + (o.source && Date.now() / 1000 - o.t < 3600 ? ' live' : '')); c.draggable = true; c.ondragstart = ev => ev.dataTransfer.setData('text/plain', o.id);
      const nm = custLink(o.cid, o.name); nm.style.fontWeight = '600'; c.append(nm, el('span', 'muted', prodName(o.product)), el('span', 'v', money(o.value, 0)));
      const meta = el('span', 'muted'); meta.style.fontSize = '12px'; meta.textContent = staffName(o.owner) + ' · ' + ago(o.moved) + (o.source ? ' · from ' + (o.source === 'kiosk' ? 'a kiosk' : 'online') : ''); c.append(meta);
      const i = META.stages.indexOf(s); const mv = el('div', 'mv');
      const btn = (lbl, st, title) => { const b = el('button', '', lbl); b.type = 'button'; b.title = title; b.onclick = () => move(o, st); mv.append(b); };
      if (i > 0 && i < 4) btn('‹', META.stages[i - 1], 'Back to ' + META.stage_names[META.stages[i - 1]]);
      if (i < 3) btn('›', META.stages[i + 1], 'On to ' + META.stage_names[META.stages[i + 1]]);
      if (!['won', 'lost'].includes(s)) { btn('Won', 'won', 'Mark as won'); btn('Lost', 'lost', 'Mark as lost'); }
      if (['won', 'lost'].includes(s)) btn('Reopen', 'approved', 'Back to Approved');
      c.append(mv); col.append(c);
    }
    board.append(col);
  }
}

// ------------------------------------------------------------ connect
async function connect(root) {
  view = 'connect';
  const d = await api('connect'); const url = location.origin;
  const card = el('div', 'card'); card.append(el('h1', '', 'Connect banks and kiosks'));
  card.append(el('p', '', 'A bank or kiosk on this cluster sends its events here by itself. For one on another cluster, create this Secret in that cluster\'s dmobnk namespace. It takes effect within a minute or so, with no restart.'));
  const cmd = 'kubectl -n dmobnk create secret generic dmobnk-crm-link \\\n  --from-literal=url=' + url + ' \\\n  --from-literal=token=' + d.token;
  const code = el('code', 'blk', cmd); card.append(code);
  const cp = el('button', 'btn ghost sm', 'Copy'); cp.style.marginTop = '8px'; cp.onclick = async () => { try { await navigator.clipboard.writeText(cmd); toast('Copied'); } catch (e) { toast('Select the text to copy it'); } }; card.append(cp);
  card.append(el('p', 'muted', 'Keep the token to yourself: anyone with it can post events here. To unlink a cluster, delete its dmobnk-crm-link Secret.'));
  root.append(card);
  const sc = el('div', 'card'); sc.append(el('h2', '', 'Sending events'));
  if (!d.sources.length) sc.append(el('div', 'empty', 'Nothing has sent an event yet.'));
  else { const t = el('table'); const h = el('tr'); for (const x of ['From', 'Where', 'Last event']) h.append(el('th', '', x)); t.append(h);
    for (const s of d.sources) { const r = el('tr'); const a = el('td'); a.append(srcBadge(s.app)); r.append(a, el('td', '', s.where), el('td', 'muted', ago(s.t))); t.append(r); } sc.append(t); }
  root.append(sc);
}

// ------------------------------------------------------------ router
async function route() {
  clearInterval(feedTimer); kpiBox = null; document.querySelectorAll('.modal').forEach(m => m.remove());
  const h = location.hash.replace(/^#\/?/, ''); const root = $('main'); root.replaceChildren();
  const [page, arg] = h.split('/');
  for (const a of document.querySelectorAll('nav a')) a.classList.toggle('on', a.dataset.p === (page === 'c' ? 'customers' : page || 'dash'));
  try {
    if (page === 'customers') await custs(root);
    else if (page === 'c' && arg) await cust(root, decodeURIComponent(arg));
    else if (page === 'pipeline') await pipeline(root);
    else if (page === 'tasks') await tasks(root);
    else if (page === 'connect') await connect(root);
    else await dash(root);
  } catch (e) { root.replaceChildren(el('div', 'card empty', e.message)); }
  window.scrollTo(0, 0);
}
(async () => {
  META = await api('me'); $('me').textContent = META.me.name; $('role').textContent = META.me.role;
  window.addEventListener('hashchange', route); route();
})();
"""


def app_page():
    nav = "".join(f'<a href="#/{p if p != "dash" else ""}" data-p="{p}">{lbl}</a>' for p, lbl in
                  [("dash", "Dashboard"), ("customers", "Customers"), ("pipeline", "Pipeline"), ("tasks", "Tasks"), ("connect", "Connect")])
    cfg = json.dumps({"money": L["money"], "lang": L["lang"]})
    body = f"""<header><div class="wrap">
<a class="brand" href="#/">{LOGO}<span><b>DMOBNK CRM</b><small>Customers and sales</small></span></a>
<nav>{nav}</nav>
<div class="who"><span><b id="me"></b><br><span id="role"></span></span>
<form method="post" action="/logout"><button type="submit">Sign out</button></form></div>
</div></header>
<main id="main"></main>"""
    return doc("DMOBNK CRM", body, f"const CFG = {cfg};\n" + APP_JS)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def _body(self, limit=65536):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, limit)) if n > 0 else b""

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/healthz":
            return self._json(200 if PASSWORD else 503, {"ok": bool(PASSWORD)})
        me = session_staff(self)
        if u.path == "/":
            return self._send(200, "text/html; charset=utf-8", app_page() if me else login_page(q.get("e", "")))
        if u.path.startswith("/api/"):
            if not me:
                return self._json(401, {"error": "Signed out."})
            try:
                return self._json(*api_get(u.path[5:], q, me))
            except Exception as err:
                print(f"GET {u.path}: {err!r}", flush=True)
                return self._json(503, {"error": "Can't reach the CRM's database. Try again."})
        self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        raw = self._body()
        if path == "/api/ingest":
            auth = self.headers.get("Authorization", "")
            if not TOKEN or not hmac.compare_digest(auth.encode(), f"Bearer {TOKEN}".encode()):
                return self._json(401, {"error": "bad token"})
            try:
                ev = json.loads(raw or b"{}")
                if not isinstance(ev, dict):
                    raise ValueError
                ingest(ev)
            except ValueError:
                return self._json(400, {"error": "bad event"})
            except Exception as err:
                print(f"ingest: {err!r}", flush=True)
                return self._json(503, {"error": "try again"})
            return self._json(200, {"ok": True})
        if path == "/login":
            ip = client_ip(self)
            if not PASSWORD:
                return self._send(303, "text/plain", "", {"Location": "/"})
            if locked_out(ip):
                return self._send(303, "text/plain", "", {"Location": "/?e=locked"})
            form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            given, staff = form.get("password", [""])[0], form.get("staff", [""])[0]
            if staff not in STAFF:
                return self._send(303, "text/plain", "", {"Location": "/?e=staff"})
            if hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest()):
                _fails.pop(ip, None)
                return self._send(303, "text/plain", "", {"Location": "/", "Set-Cookie": set_cookie(self, new_session(staff), SESSION_S)})
            _fails.setdefault(ip, []).append(time.time())
            time.sleep(1)
            return self._send(303, "text/plain", "", {"Location": "/?e=wrong"})
        if path == "/logout":
            return self._send(303, "text/plain", "", {"Location": "/", "Set-Cookie": set_cookie(self, "", 0)})
        if path.startswith("/api/"):
            me = session_staff(self)
            if not me:
                return self._json(401, {"error": "Signed out."})
            if self.headers.get("X-CRM") != "1":
                return self._json(403, {"error": "forbidden"})
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad request"})
            try:
                return self._json(*api_post(path[5:], body, me))
            except Exception as err:
                print(f"POST {path}: {err!r}", flush=True)
                return self._json(503, {"error": "Can't reach the CRM's database. Try again."})
        self._send(404, "text/plain; charset=utf-8", "not found")

    def log_message(self, *args):
        pass


def seed_loop():
    while True:
        try:
            seed()
            return
        except Exception as err:
            print(f"seeding waits for redis: {err}", flush=True)
            time.sleep(3)


threading.Thread(target=init, daemon=True).start()
threading.Thread(target=seed_loop, daemon=True).start()
ingress_host.start("crm-web", "crm", os.environ.get("CRM_HOST", ""), os.environ.get("INGRESS_CLASS", ""),
                   path="/", service="crm-web", owner="crm-web")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
