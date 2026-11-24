"""DMOBNK branch counter: the web app for one retail branch, deployed by
Fleet to clusters labelled dmobnk.smclab.net/role=branch.

Three screens: the customer kiosk (take a queue ticket), the queue board
(the "now serving" display) and the teller screen (call the next customer,
record cash paid in or taken out, check in appointments). Shared state lives
in branch-redis, so any replica can serve any screen.

The text, currency and number formats come from the mounted locale ConfigMap
(the "b" section). The app also points its own Ingress at
branch.<node address>.sslip.io, or at the host set in the optional
dmobnk-branch ConfigMap (see branch.yaml)."""
import html
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

L = json.load(open("/locale/locale.json", encoding="utf-8"))
B = L["b"]
T = L["t"]
BRANCH = os.environ.get("BRANCH_NAME", "").strip() or B["branch_name"]
COUNTERS = 3
SERVICES = ["A", "B", "C"]
MINUTES_PER_CUSTOMER = 4
CASH_LOW = 0.2  # warn below 20% of the opening cash
CASH_START = int(L["branch_cash_start"]) * 100  # held in minor units


# ---------------------------------------------------------------- redis
class Redis:
    """Just enough of the Redis protocol for this app, so the image needs
    no pip install (branch clusters may have no internet beyond images)."""

    def __init__(self, host, port=6379):
        self.addr = (host, port)
        self.lock = threading.Lock()
        self.sock = None
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _exact(self, n):
        while len(self.buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n + 2:]
        return data

    def _reply(self):
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RuntimeError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self._exact(n).decode()
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._reply() for _ in range(n)]
        raise ConnectionError("bad redis reply")

    def __call__(self, *args):
        out = b"*%d\r\n" % len(args)
        for a in args:
            a = str(a).encode()
            out += b"$%d\r\n%s\r\n" % (len(a), a)
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self.sock = socket.create_connection(self.addr, timeout=3)
                        self.buf = b""
                    self.sock.sendall(out)
                    return self._reply()
                except (OSError, ConnectionError):
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                    if attempt:
                        raise


r = Redis(os.environ.get("REDIS_HOST", "branch-redis"))

WITHDRAW_LUA = """
local c = tonumber(redis.call('GET', KEYS[1]) or ARGV[2])
local amt = tonumber(ARGV[1])
if c < amt then return -1 end
redis.call('SET', KEYS[1], c - amt)
return c - amt
"""


def day():
    return time.strftime("%Y%m%d")


def k(name):
    return f"branch:{day()}:{name}"


def cash():
    r("SET", "branch:cash", CASH_START, "NX")
    return int(r("GET", "branch:cash"))


# ---------------------------------------------------------------- branch logic
def take_ticket(svc):
    n = r("INCR", k(f"seq:{svc}"))
    r("EXPIRE", k(f"seq:{svc}"), 172800)
    ticket = {"no": f"{svc}{n:03d}", "svc": svc, "t": int(time.time())}
    ahead = r("RPUSH", k("queue"), json.dumps(ticket)) - 1
    r("EXPIRE", k("queue"), 172800)
    return {"no": ticket["no"], "svc": svc, "ahead": ahead,
            "minutes": round(ahead * MINUTES_PER_CUSTOMER / COUNTERS)}


def call_next(counter):
    raw = r("LPOP", k("queue"))
    if raw is None:
        r("DEL", k(f"counter:{counter}"))
        return None
    ticket = json.loads(raw)
    now = int(time.time())
    ticket.update(counter=counter, called=now)
    r("SET", k(f"counter:{counter}"), json.dumps(ticket), "EX", 172800)
    r("LPUSH", k("called"), json.dumps(ticket))
    r("LTRIM", k("called"), 0, 5)
    r("EXPIRE", k("called"), 172800)
    r("INCR", k("served"))
    r("INCRBY", k("waitsum"), now - ticket["t"])
    for key in ("served", "waitsum"):
        r("EXPIRE", k(key), 172800)
    return ticket


def record_tx(kind, amount, acct):
    minor = int(round(amount * 100))
    if kind == "out":
        left = r("EVAL", WITHDRAW_LUA, 1, "branch:cash", minor, CASH_START)
        if left < 0:
            return None
    else:
        cash()
        r("INCRBY", "branch:cash", minor)
    tx = {"t": int(time.time()), "kind": kind, "amount": minor / 100, "acct": acct}
    r("LPUSH", k("ledger"), json.dumps(tx))
    r("LTRIM", k("ledger"), 0, 19)
    r("EXPIRE", k("ledger"), 172800)
    return tx


def state():
    queue = [json.loads(x) for x in r("LRANGE", k("queue"), 0, -1)]
    counters = {}
    for n in range(1, COUNTERS + 1):
        raw = r("GET", k(f"counter:{n}"))
        counters[n] = json.loads(raw) if raw else None
    served = int(r("GET", k("served")) or 0)
    waitsum = int(r("GET", k("waitsum")) or 0)
    checked = set(r("SMEMBERS", k("checkin")) or [])
    c = cash()
    return {"branch": BRANCH, "queue": queue, "counters": counters,
            "called": [json.loads(x) for x in r("LRANGE", k("called"), 0, -1)],
            "served": served, "avg_wait": round(waitsum / served / 60) if served else None,
            "cash": c / 100, "cash_low": c < CASH_START * CASH_LOW,
            "ledger": [json.loads(x) for x in r("LRANGE", k("ledger"), 0, -1)],
            "appts": [{"i": i, "time": a[0], "name": a[1], "topic": a[2], "in": str(i) in checked}
                      for i, a in enumerate(L["branch_appts"])],
            "pod": os.environ.get("POD_NAME", "")}


# ---------------------------------------------------------------- own Ingress host
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = "dmobnk"
INGRESS = f"/apis/networking.k8s.io/v1/namespaces/{NS}/ingresses/branch-web"


def k8s(method, path, body=None, ctype="application/json"):
    with open(f"{SA}/token") as f:
        tok = f.read().strip()
    req = urllib.request.Request("https://kubernetes.default.svc" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=5, context=ssl.create_default_context(cafile=f"{SA}/ca.crt")) as resp:
        return json.load(resp)


IMDS = "http://169.254.169.254/latest"


def ec2_public_ip():
    """The EC2 instance's public IPv4 from instance metadata. Self-managed
    k3s/rke2 on EC2 don't record it on the Node, so without this the host
    would be the private VPC address. Tries IMDSv2, then IMDSv1: with the
    instance's default hop limit of 1, IMDSv2's token reply never reaches a
    pod, but a v1 reply does when the instance allows v1. Fails fast off EC2."""
    headers = {}
    try:
        req = urllib.request.Request(f"{IMDS}/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with urllib.request.urlopen(req, timeout=1) as resp:
            headers = {"X-aws-ec2-metadata-token": resp.read().decode()}
    except Exception:
        pass
    try:
        req = urllib.request.Request(f"{IMDS}/meta-data/public-ipv4", headers=headers)
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.read().decode().strip() or None
    except Exception:
        return None


def on_ec2(node):
    if (node.get("spec", {}).get("providerID") or "").startswith("aws://"):
        return True
    for f in ("/sys/class/dmi/id/sys_vendor", "/sys/class/dmi/id/board_vendor", "/sys/class/dmi/id/product_uuid"):
        try:
            with open(f) as fh:
                v = fh.read().strip().lower()
            if "amazon" in v or v.startswith("ec2"):
                return True
        except OSError:
            pass
    return False


def egress_ip():
    # AWS's own "what's my IP" service: on an EC2 instance with a public IP
    # (and no NAT gateway) that's the instance's public IP.
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com/", timeout=3) as resp:
            ip = resp.read().decode().strip()
            return ip if ip.count(".") == 3 else None
    except Exception:
        return None


def node_address():
    """(address, where it came from): the node's ExternalIP (EKS and most
    managed clouds), else on EC2 the instance's public IP, else the node's
    InternalIP (right on-prem)."""
    name = os.environ.get("NODE_NAME", "")
    node = {}
    try:
        node = k8s("GET", f"/api/v1/nodes/{name}")
        for a in node["status"].get("addresses", []):
            if a.get("type") == "ExternalIP" and a.get("address"):
                return a["address"], "node ExternalIP"
    except Exception as err:
        print(f"couldn't read node {name}: {err}", flush=True)
    ip = ec2_public_ip()
    if ip:
        return ip, "EC2 instance metadata"
    if on_ec2(node):
        ip = egress_ip()
        if ip:
            return ip, "EC2 egress IP (checkip.amazonaws.com)"
        print("on EC2 but found no public IP: set `host` in the dmobnk-branch ConfigMap", flush=True)
    ip = os.environ.get("NODE_IP", "").strip()
    return (ip, "node InternalIP") if ip else (None, None)


def ingress_class(current):
    """The class to use: the one set in dmobnk-branch, else none when the
    cluster has a default class, else its only class, else traefik or nginx."""
    fixed = os.environ.get("INGRESS_CLASS", "").strip()
    if fixed:
        return fixed
    try:
        items = k8s("GET", "/apis/networking.k8s.io/v1/ingressclasses")["items"]
    except Exception:
        return current
    names = [i["metadata"]["name"] for i in items]
    if any(i["metadata"].get("annotations", {}).get("ingressclass.kubernetes.io/is-default-class") == "true"
           for i in items):
        return current
    if current in names:
        return current
    if len(names) == 1:
        return names[0]
    for pick in ("traefik", "nginx"):
        if pick in names:
            return pick
    return current


def ingress_loop():
    if not os.path.exists(f"{SA}/token"):
        return
    address, checked = None, 0.0
    while True:
        try:
            host = os.environ.get("BRANCH_HOST", "").strip()
            if not host:
                # Re-resolved every 5 minutes, so a wrong first answer (or a
                # new public IP after a stop/start) doesn't stick.
                if not address or time.time() - checked > 300:
                    found, source = node_address()
                    if found and found != address:
                        print(f"using {source} {found}", flush=True)
                    address, checked = found or address, time.time()
                host = f"branch.{address}.sslip.io" if address else None
            spec = k8s("GET", INGRESS)["spec"]
            ops = []
            if host and spec["rules"][0].get("host") != host:
                ops.append({"op": "replace", "path": "/spec/rules/0/host", "value": host})
            cls = ingress_class(spec.get("ingressClassName"))
            if cls and cls != spec.get("ingressClassName"):
                ops.append({"op": "add", "path": "/spec/ingressClassName", "value": cls})
            if ops:
                k8s("PATCH", INGRESS, ops, "application/json-patch+json")
                print(f"ingress set to host {host}, class {cls or '(cluster default)'}", flush=True)
        except Exception as err:
            print(f"ingress update failed: {err}", flush=True)
        time.sleep(10)


# ---------------------------------------------------------------- pages
def e(s):
    return html.escape(str(s), quote=True)


CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f8fc;color:#0b1b3a;font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh;display:flex;flex-direction:column}
a{color:#1f4fe0}
button,input,select{font:inherit}
button{cursor:pointer}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #7fa2ff;outline-offset:2px}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace}
.disp{font-family:"Space Grotesk",Inter,sans-serif}
.wrap{max-width:1280px;margin:0 auto;width:100%;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.demo{background:#fdecd3;border-bottom:1px solid #f0c58a;color:#5c3300;font-size:13px}
.demo .wrap{display:flex;align-items:flex-start;gap:10px;padding-block:9px}
.demo b{flex-shrink:0;background:#8a4b00;color:#fff;border-radius:6px;padding:1px 8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-top:1px}
header{background:#fff;border-bottom:1px solid #dfe5f1}
header .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:12px 28px;padding-block:14px}
.logo{display:flex;align-items:center;gap:10px;color:#0b1b3a;text-decoration:none}
.logo b{font:700 22px/1.1 "Space Grotesk",sans-serif;letter-spacing:.04em;display:block}
.logo small{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#4a5a7d}
nav{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:14px;font-weight:600;flex:1}
nav a{color:#0b1b3a;text-decoration:none;padding:6px 0;border-bottom:2px solid transparent}
nav a[aria-current]{border-color:#2453ff;color:#1f4fe0}
main{flex:1;padding-block:28px 44px;display:flex;flex-direction:column;gap:24px}
h1{margin:0;font:700 clamp(30px,4.5vw,46px)/1.08 "Space Grotesk",sans-serif;letter-spacing:-1.2px;text-wrap:balance}
h2{margin:0;font:700 20px/1.2 "Space Grotesk",sans-serif;letter-spacing:-.3px}
h3{margin:0;font-size:17px}
p{margin:0}
.muted{color:#4a5a7d}
.card{background:#fff;border:1px solid #dfe5f1;border-radius:18px;padding:20px;display:flex;flex-direction:column;gap:12px;min-width:0}
.row{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.btn{min-height:44px;padding:0 20px;border:0;border-radius:12px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;text-decoration:none;background:#2453ff;color:#fff}
.btn.navy{background:#0b1b3a}.btn.ghost{background:#fff;color:#1f4fe0;border:2px solid #2453ff}.btn.amber{background:#e8890c;color:#1d1200}
.btn:disabled{opacity:.55;cursor:progress}
.svc{all:unset;cursor:pointer;background:#fff;border:2px solid #dfe5f1;border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:10px;min-height:170px;transition:border-color .2s,transform .2s}
.svc:hover{border-color:#2453ff}
.svc:focus-visible{outline:3px solid #7fa2ff;outline-offset:2px}
.svc .letter{width:56px;height:56px;border-radius:16px;display:grid;place-items:center;font:700 26px "Space Grotesk",sans-serif;color:#fff}
.ticket{background:#0b1b3a;color:#fff;border-radius:24px;padding:32px;display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;max-width:520px;width:100%;margin:0 auto}
.ticket .no{font:700 clamp(72px,16vw,120px)/1 "Space Grotesk",sans-serif;letter-spacing:-3px}
.board{background:#0b1b3a;color:#fff;border-radius:24px;padding:24px;display:flex;flex-direction:column;gap:18px}
.board .counters{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.board .ctr{background:#13274f;border-radius:18px;padding:18px;display:flex;flex-direction:column;gap:4px}
.board .ctr span{color:#b9c6e4;font-size:14px;font-weight:600}
.board .ctr b{font:700 clamp(48px,8vw,76px)/1 "Space Grotesk",sans-serif;letter-spacing:-2px}
.board .ctr.flash{animation:flash 1.2s ease 2}
@keyframes flash{50%{background:#e8890c;color:#1d1200}}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{border-radius:10px;padding:6px 12px;font:700 18px "Space Grotesk",sans-serif;background:#eef2f8;color:#0b1b3a}
.board .chip{background:#18305e;color:#fff}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.stat{background:#f4f7fd;border-radius:12px;padding:12px}
.stat span{font-size:12px;color:#4a5a7d}
.stat b{display:block;font:700 24px "Space Grotesk",sans-serif}
label{font-size:13px;font-weight:600}
input,select{height:44px;border:1px solid #c9d3e8;border-radius:12px;padding:0 12px;color:#0b1b3a;width:100%;background:#fff}
.field{display:flex;flex-direction:column;gap:6px;min-width:0}
.seg{display:flex;border:1px solid #c9d3e8;border-radius:12px;overflow:hidden}
.seg button{flex:1;min-height:42px;border:0;background:#fff;color:#0b1b3a;font-weight:700}
.seg button[aria-pressed=true]{background:#0b1b3a;color:#fff}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#4a5a7d;padding:8px 10px;border-bottom:1px solid #dfe5f1;text-align:left}
td{padding:9px 10px;border-bottom:1px solid #eef2f8;font-variant-numeric:tabular-nums}
td.num,th.num{text-align:right}
.scroll{overflow-x:auto}
.msg{min-height:1.4em;font-size:13px;font-weight:600}
.msg.err{color:#b42335}.msg.ok{color:#12804a}
.warn{background:#fdecd3;color:#8a4b00;border-radius:12px;padding:10px 12px;font-size:14px;font-weight:600}
.badge{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px;background:#e3f4ea;color:#0d6b3c;white-space:nowrap}
footer{border-top:1px solid #dfe5f1;background:#fff;font-size:13px;color:#4a5a7d}
footer .wrap{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;padding-block:18px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

LOGO = ('<svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><rect x="2" y="2" width="28" height="28" rx="9" fill="#0b1b3a"/>'
        '<rect x="8" y="17" width="4" height="7" rx="1" fill="#7fa2ff"/><rect x="14" y="12" width="4" height="12" rx="1" fill="#7fa2ff"/>'
        '<rect x="20" y="8" width="4" height="16" rx="1" fill="#e8890c"/></svg>')
SVC_COLORS = {"A": "#2453ff", "B": "#0e9f6e", "C": "#e8890c"}


def page(name, title, body, js):
    pages = [("", B["nav_kiosk"]), ("board", B["nav_board"]), ("teller", B["nav_teller"])]
    nav = "".join(f'<a href="/{p}"{" aria-current=page" if p == name else ""}>{e(lbl)}</a>' for p, lbl in pages)
    cfg = {"lang": L["lang"] if L["country"] != "sim" else "en-GB", "money": L["money"], "b": B,
           "colors": SVC_COLORS, "services": {s: B["svc_" + s] for s in SERVICES}}
    cfg_json = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{e(L['lang'])}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · {e(BRANCH)} · DMOBNK</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">
<style>{CSS}</style>
</head>
<body>
<div class="demo" role="note"><div class="wrap"><b>{e(T['demo_label'])}</b><span>{e(T['demo_banner'])}</span></div></div>
<header><div class="wrap">
  <a class="logo" href="/">{LOGO}<span><b>DMOBNK</b><small>{e(BRANCH)}</small></span></a>
  <nav aria-label="{e(B['title'])}">{nav}</nav>
</div></header>
<main class="wrap">{body}</main>
<footer><div class="wrap"><span>{e(T['footer'])}</span><span class="mono" id="pod"></span></div></footer>
<script id="cfg" type="application/json">{cfg_json}</script>
<script>{COMMON_JS}</script>
<script>{js}</script>
</body>
</html>"""


COMMON_JS = r"""
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
  return m.pos === 'pre' ? m.symbol + (m.space ? ' ' : '') + s : s + (m.space ? ' ' : '') + m.symbol;
}
function hhmm(epoch) { return new Date(epoch * 1000).toLocaleTimeString(CFG.lang, {hour: '2-digit', minute: '2-digit'}); }
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
async function api(path, body) {
  const opts = body === undefined ? {cache: 'no-store'} : {method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const resp = await fetch('/api/' + path, opts);
  let data = {}; try { data = await resp.json(); } catch (err) {}
  if (!resp.ok) throw new Error(data.error || B.error);
  return data;
}
function poll(fn, ms) { const run = () => fn().catch(() => {}); run(); setInterval(run, ms); }
function chip(no) { const c = el('span', 'chip', no); c.style.borderLeft = '5px solid ' + (CFG.colors[no[0]] || '#94a3b8'); return c; }
"""


def kiosk_page():
    tiles = "".join(f"""<button class="svc" type="button" data-svc="{s}">
  <span class="letter" style="background:{SVC_COLORS[s]}" aria-hidden="true">{s}</span>
  <h2>{e(B['svc_' + s])}</h2><span class="muted">{e(B['svc_' + s + '_d'])}</span>
</button>""" for s in SERVICES)
    body = f"""
<section id="pick" style="display:flex;flex-direction:column;gap:22px">
  <div style="display:flex;flex-direction:column;gap:8px"><h1>{e(B['kiosk_h'])}</h1><p class="muted" style="font-size:17px">{e(B['kiosk_lede'])}</p></div>
  <div class="grid">{tiles}</div>
  <p class="muted" id="waiting-now"></p>
  <p class="msg err" id="k-msg" role="alert"></p>
</section>
<section id="got" hidden aria-live="polite">
  <div class="ticket">
    <span style="color:#b9c6e4;font-weight:600">{e(B['your_ticket'])}</span>
    <span class="no" id="t-no"></span>
    <span id="t-svc" style="font-weight:700"></span>
    <span id="t-ahead" style="color:#b9c6e4"></span>
    <button class="btn amber" type="button" id="t-done" style="margin-top:8px;min-width:160px">{e(B['done'])}</button>
  </div>
</section>"""
    js = r"""
let resetTimer = null;
function reset() { $('got').hidden = true; $('pick').hidden = false; clearTimeout(resetTimer); }
document.querySelectorAll('.svc').forEach(b => b.addEventListener('click', async () => {
  $('k-msg').textContent = '';
  try {
    const t = await api('ticket', {svc: b.dataset.svc});
    $('t-no').textContent = t.no; $('t-svc').textContent = CFG.services[t.svc];
    $('t-ahead').textContent = t.ahead ? tr('ahead', {n: t.ahead, min: Math.max(1, t.minutes)}) : B.ahead_none;
    $('pick').hidden = true; $('got').hidden = false; $('t-done').focus();
    resetTimer = setTimeout(reset, 20000);
  } catch (err) { $('k-msg').textContent = err.message; }
}));
$('t-done').addEventListener('click', reset);
poll(async () => {
  const s = await api('state');
  $('waiting-now').textContent = s.queue.length ? B.waiting + ': ' + s.queue.length : B.none_waiting;
  $('pod').textContent = s.pod;
}, 5000);
"""
    return page("", B["nav_kiosk"], body, js)


def board_page():
    ctrs = "".join(f'<div class="ctr" id="ctr-{n}"><span>{e(B["counter"].replace("{n}", str(n)))}</span><b>…</b></div>'
                   for n in range(1, COUNTERS + 1))
    body = f"""
<section class="board" aria-labelledby="bh">
  <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px">
    <h1 id="bh" style="color:#fff">{e(B['board_h'])}</h1><span class="disp" style="font-size:28px;font-weight:700" id="clock"></span>
  </div>
  <div class="counters" aria-live="polite">{ctrs}</div>
  <div style="display:flex;flex-direction:column;gap:10px"><h2 style="color:#b9c6e4;font-size:16px">{e(B['waiting'])}</h2><div class="chips" id="b-wait"></div></div>
</section>"""
    js = r"""
const shown = {};
function tickClock() { $('clock').textContent = new Date().toLocaleTimeString(CFG.lang, {hour: '2-digit', minute: '2-digit'}); }
tickClock(); setInterval(tickClock, 10000);
poll(async () => {
  const s = await api('state');
  for (const [n, t] of Object.entries(s.counters)) {
    const box = $('ctr-' + n); const no = t ? t.no : B.free;
    if (shown[n] !== undefined && shown[n] !== no && t) { box.classList.remove('flash'); void box.offsetWidth; box.classList.add('flash'); }
    shown[n] = no; box.querySelector('b').textContent = no;
    box.querySelector('b').style.color = t ? '#fff' : '#6b7fa8';
  }
  $('b-wait').replaceChildren(...(s.queue.length ? s.queue.slice(0, 18).map(t => chip(t.no)) : [el('span', '', B.none_waiting)]));
  $('pod').textContent = s.pod;
}, 2000);
"""
    return page("board", B["nav_board"], body, js)


def teller_page():
    opts = "".join(f'<option value="{n}">{e(B["counter"].replace("{n}", str(n)))}</option>' for n in range(1, COUNTERS + 1))
    sym = e(L["money"]["symbol"])
    body = f"""
<section style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px">
  <div style="display:flex;flex-direction:column;gap:6px"><h1>{e(B['teller_h'])}</h1><p class="muted">{e(B['staff_note'])}</p></div>
  <div class="field" style="width:220px"><label for="ctr">{e(B['your_counter'])}</label><select id="ctr">{opts}</select></div>
</section>
<div class="stats">
  <div class="stat"><span>{e(B['waiting'])}</span><b id="s-wait">…</b></div>
  <div class="stat"><span>{e(B['served'])}</span><b id="s-served">…</b></div>
  <div class="stat"><span>{e(B['avg_wait'])}</span><b id="s-avg">…</b></div>
  <div class="stat"><span>{e(B['cash_h'])}</span><b id="s-cash">…</b></div>
</div>
<p class="warn" id="cash-low" hidden>{e(B['cash_low'])}</p>
<div class="row">
  <section class="card" style="flex:1 1 340px" aria-labelledby="sv-h">
    <h2 id="sv-h">{e(B['serving'])}</h2>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <span class="disp" style="font-size:56px;font-weight:700;letter-spacing:-2px;line-height:1" id="cur-no">—</span>
      <span class="muted" id="cur-svc"></span>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:10px">
      <button class="btn" type="button" id="call">{e(B['call_next'])}</button>
      <button class="btn ghost" type="button" id="finish">{e(B['finish'])}</button>
    </div>
    <p class="msg" id="q-msg" aria-live="polite"></p>
    <div class="chips" id="t-queue"></div>
  </section>
  <section class="card" style="flex:1 1 340px" aria-labelledby="tx-h">
    <h2 id="tx-h">{e(B['tx_h'])}</h2>
    <div class="seg" role="group" aria-label="{e(B['tx_h'])}">
      <button type="button" id="k-in" aria-pressed="true">{e(B['tx_in'])}</button>
      <button type="button" id="k-out" aria-pressed="false">{e(B['tx_out'])}</button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px">
      <div class="field"><label for="amt">{e(B['tx_amount'])} ({sym})</label><input id="amt" type="number" min="1" max="10000" step="0.01" inputmode="decimal"></div>
      <div class="field"><label for="acct">{e(B['tx_account'])}</label><input id="acct" inputmode="numeric" maxlength="4" pattern="[0-9]{{4}}" placeholder="0000"></div>
    </div>
    <button class="btn navy" type="button" id="tx-go">{e(B['tx_go'])}</button>
    <p class="msg" id="tx-msg" aria-live="polite"></p>
  </section>
</div>
<div class="row">
  <section class="card" style="flex:1 1 340px" aria-labelledby="ap-h">
    <h2 id="ap-h">{e(B['appts_h'])}</h2>
    <div class="scroll"><table><tbody id="appts"></tbody></table></div>
  </section>
  <section class="card" style="flex:1 1 340px" aria-labelledby="lg-h">
    <h2 id="lg-h">{e(B['ledger_h'])}</h2>
    <div class="scroll"><table><tbody id="ledger"></tbody></table></div>
  </section>
</div>"""
    js = r"""
try { const saved = localStorage.getItem('dmobnk-counter'); if (saved) $('ctr').value = saved; } catch (err) {}
$('ctr').addEventListener('change', () => { try { localStorage.setItem('dmobnk-counter', $('ctr').value); } catch (err) {} refresh(); });
let kind = 'in';
for (const [id, k] of [['k-in', 'in'], ['k-out', 'out']]) $(id).addEventListener('click', () => {
  kind = k; $('k-in').setAttribute('aria-pressed', k === 'in'); $('k-out').setAttribute('aria-pressed', k === 'out');
});
function draw(s) {
  const mine = s.counters[$('ctr').value];
  $('cur-no').textContent = mine ? mine.no : '—';
  $('cur-svc').textContent = mine ? CFG.services[mine.svc] : B.nobody;
  $('s-wait').textContent = s.queue.length; $('s-served').textContent = s.served;
  $('s-avg').textContent = s.avg_wait === null ? '—' : s.avg_wait + ' ' + B.min;
  $('s-cash').textContent = money(s.cash, 0); $('cash-low').hidden = !s.cash_low;
  $('t-queue').replaceChildren(...s.queue.slice(0, 12).map(t => chip(t.no)));
  $('appts').replaceChildren(...s.appts.map(a => {
    const row = el('tr'); const act = el('td', 'num');
    if (a.in) act.append(el('span', 'badge', B.checked_in));
    else { const b = el('button', 'btn ghost', B.check_in); b.type = 'button'; b.style.minHeight = '36px'; b.onclick = () => post('checkin', {i: a.i}); act.append(b); }
    const who = el('td'); who.append(el('strong', '', a.name), el('br'), el('span', 'muted', a.topic));
    row.append(el('td', 'mono', a.time), who, act); return row;
  }));
  const empty = el('tr'); empty.append(el('td', 'muted', B.tx_none));
  $('ledger').replaceChildren(...(s.ledger.length ? s.ledger.map(t => {
    const row = el('tr');
    row.append(el('td', 'mono', hhmm(t.t)), el('td', '', t.kind === 'in' ? B.tx_in : B.tx_out), el('td', 'mono', '••' + t.acct),
               el('td', 'num', (t.kind === 'in' ? '+' : '−') + money(t.amount)));
    return row;
  }) : [empty]));
  $('pod').textContent = s.pod;
}
async function refresh() { draw(await api('state')); }
async function post(path, body, msgId) {
  try { const res = await api(path, body); await refresh(); return res; }
  catch (err) { if (msgId) { $(msgId).className = 'msg err'; $(msgId).textContent = err.message; } }
}
$('call').addEventListener('click', async ev => {
  ev.target.disabled = true; $('q-msg').textContent = '';
  const res = await post('call', {counter: +$('ctr').value}, 'q-msg');
  if (res && !res.ticket) { $('q-msg').className = 'msg'; $('q-msg').textContent = B.queue_empty; }
  ev.target.disabled = false;
});
$('finish').addEventListener('click', () => post('finish', {counter: +$('ctr').value}, 'q-msg'));
$('tx-go').addEventListener('click', async ev => {
  const amount = parseFloat($('amt').value), acct = $('acct').value.trim();
  if (!(amount > 0) || !/^\d{4}$/.test(acct)) { $('tx-msg').className = 'msg err'; $('tx-msg').textContent = B.tx_bad; return; }
  ev.target.disabled = true;
  const res = await post('tx', {kind, amount, acct}, 'tx-msg');
  if (res) { $('tx-msg').className = 'msg ok'; $('tx-msg').textContent = B.tx_done; $('amt').value = ''; $('acct').value = ''; }
  ev.target.disabled = false;
});
poll(refresh, 3000);
"""
    return page("teller", B["nav_teller"], body, js)


PAGES = {"": kiosk_page, "board": board_page, "teller": teller_page}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = self.path.split("?", 1)[0].strip("/")
        if path == "healthz":
            return self._json(200, {"ok": True})
        try:
            if path in PAGES:
                return self._send(200, "text/html; charset=utf-8", PAGES[path]())
            if path == "api/state":
                return self._json(200, state())
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
            counter = body.get("counter")
            if path in ("api/call", "api/finish"):
                if counter not in range(1, COUNTERS + 1):
                    return self._json(400, {"error": B["error"]})
                if path == "api/call":
                    return self._json(200, {"ticket": call_next(counter)})
                r("DEL", k(f"counter:{counter}"))
                return self._json(200, {"ok": True})
            if path == "api/tx":
                amount, acct = body.get("amount"), str(body.get("acct", ""))
                if (body.get("kind") not in ("in", "out") or not isinstance(amount, (int, float))
                        or not 0 < amount <= 10000 or not (len(acct) == 4 and acct.isdigit())):
                    return self._json(400, {"error": B["tx_bad"]})
                tx = record_tx(body["kind"], amount, acct)
                return self._json(200, tx) if tx else self._json(409, {"error": B["tx_nocash"]})
            if path == "api/checkin":
                i = body.get("i")
                if i not in range(len(L["branch_appts"])):
                    return self._json(400, {"error": B["error"]})
                r("SADD", k("checkin"), i)
                r("EXPIRE", k("checkin"), 172800)
                return self._json(200, {"ok": True})
        except Exception as err:
            print(f"POST {path}: {err}", flush=True)
            return self._json(503, {"error": B["error"]})
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


threading.Thread(target=ingress_loop, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
