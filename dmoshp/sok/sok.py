"""DMOSOK: the self-checkout in a DMOSHP store. Customers scan their items
(tap a product, or type its barcode), pay by card and get a receipt. It
sells from the same live stock as the online shop (../shop), and a DMOBNK
card is authorised by the bank (../common/store.py).

It points its own Ingress at the cluster it runs on (sok.<address>.sslip.io,
../common/ingress_host.py). Settings, all optional, in the dmoshp-sok
ConfigMap: host, ingressClass, name (the store, e.g. "Borough Market")."""
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/dmobnk")
import crm_feed  # noqa: E402  (mounted from the dmoshp-lib ConfigMap)
import ingress_host  # noqa: E402
import store  # noqa: E402
from store import e  # noqa: E402

STORE = (os.environ.get("STORE_NAME") or "London Bridge").strip()[:40]
crm_feed.SOURCE.update(app="sok", where=f"DMOSHP {STORE}")

CSS = store.CHROME_CSS + """
:root{--g:#0f7a4f;--g2:#e3f6ec;--ink:#13221b;--muted:#5d6b63;--line:#dfe7e2;--bg:#eef3ef;--red:#b42335}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column;user-select:none;-webkit-tap-highlight-color:transparent}
h1,h2,h3{font-family:"Space Grotesk",Inter,sans-serif;margin:0;letter-spacing:-.01em}
button,input{font:inherit;color:inherit}
button{cursor:pointer}
header{background:#13221b;color:#fff}
header .in{max-width:1240px;margin:0 auto;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.brand{display:flex;gap:10px;align-items:center}
.brand b{font-family:"Space Grotesk",sans-serif;font-size:21px;display:block;line-height:1.1}
.brand small{color:#b7cfc2;font-size:13px}
.clock{font-family:"Space Grotesk",sans-serif;font-size:24px;font-weight:700}
main{flex:1;max-width:1240px;width:100%;margin:0 auto;padding:22px 16px;display:flex;flex-direction:column;min-height:0}
.screen{flex:1;display:flex;flex-direction:column;gap:18px;min-height:0}
.screen[hidden]{display:none}
.welcome{align-items:center;justify-content:center;text-align:center}
.welcome h1{font-size:44px;line-height:1.1}
.lede{color:var(--muted);margin:0}
.big{border:0;background:var(--g);color:#fff;font-weight:700;border-radius:18px;padding:20px 40px;font-size:22px}
.big:disabled{opacity:.45;cursor:not-allowed}
.big.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.scan{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(300px,1fr);gap:18px;flex:1;min-height:0}
@media (max-width:820px){.scan{grid-template-columns:1fr}.welcome h1{font-size:32px}.brand small{display:none}}
.shelf{display:flex;flex-direction:column;gap:12px;min-height:0}
.bar{display:flex;gap:10px;flex-wrap:wrap}
.bar input{flex:1;min-width:180px;border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fff;font-family:"JetBrains Mono",ui-monospace,monospace;user-select:text}
.bar button{border:0;background:var(--ink);color:#fff;border-radius:12px;padding:12px 18px;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));grid-auto-rows:max-content;gap:12px;overflow:auto;padding-bottom:6px;flex:1;min-height:0}
.tile{border:1px solid var(--line);background:#fff;border-radius:14px;padding:0;overflow:hidden;text-align:left;display:flex;flex-direction:column}
.tile .img{height:74px;width:100%;display:grid;place-items:center}
.tile .img svg{width:38px;height:38px;stroke:#fff;fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.tile .t{width:100%;padding:8px 10px 10px;font-size:14px;line-height:1.3;display:flex;flex-direction:column;gap:2px}
.tile .t b{font-size:15px}
.tile:active{transform:scale(.98)}
.tile.flash{outline:3px solid var(--g)}
.tile:disabled{opacity:.4}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 14px;font-weight:500;font-size:15px}
.chip[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.till{background:#fff;border:1px solid var(--line);border-radius:18px;display:flex;flex-direction:column;min-height:300px}
.till .hd{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
.till .items{flex:1;overflow:auto;padding:4px 18px}
.till .ft{padding:16px 18px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:12px}
.it{display:grid;grid-template-columns:1fr auto;gap:4px 10px;padding:10px 0;border-bottom:1px solid var(--line)}
.it .ctl{display:flex;gap:6px;align-items:center;grid-column:1/-1}
.it .ctl button{border:1px solid var(--line);background:#fff;border-radius:8px;width:36px;height:32px;font-size:18px}
.it .ctl .rm{width:auto;padding:0 10px;font-size:13px;color:var(--red);margin-left:auto}
.tot{display:flex;justify-content:space-between;font-weight:700;font-size:24px;font-family:"Space Grotesk",sans-serif}
.empty{color:var(--muted);text-align:center;padding:30px 0}
.pay{align-items:center;justify-content:center;text-align:center}
.cards{display:flex;gap:18px;flex-wrap:wrap;justify-content:center}
.cc{width:300px;max-width:100%;aspect-ratio:1.586;border-radius:18px;border:0;color:#fff;padding:18px;text-align:left;display:flex;flex-direction:column;justify-content:space-between;box-shadow:0 10px 30px rgba(10,30,20,.18)}
.cc b{font-family:"Space Grotesk",sans-serif;font-size:20px}
.cc .num{font-family:"JetBrains Mono",ui-monospace,monospace;letter-spacing:.12em;font-size:16px}
.cc small{opacity:.85;font-size:13px}
.cc:disabled{filter:grayscale(1);opacity:.5;cursor:not-allowed}
.spin{width:64px;height:64px;border-radius:50%;border:6px solid var(--g2);border-top-color:var(--g);animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
.receipt{background:#fff;border:1px solid var(--line);border-radius:14px;width:min(420px,100%);padding:20px;text-align:left;font-family:"JetBrains Mono",ui-monospace,monospace;font-size:14px;display:flex;flex-direction:column;gap:8px}
.receipt .r{display:flex;justify-content:space-between;gap:10px}
.receipt hr{border:0;border-top:1px dashed #b9c6bf;width:100%;margin:4px 0}
.bad{width:72px;height:72px;border-radius:50%;background:#fde3e6;color:var(--red);display:grid;place-items:center;font-size:36px;font-weight:700}
.good{width:72px;height:72px;border-radius:50%;background:var(--g);color:#fff;display:grid;place-items:center;font-size:36px}
.row{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:#fff;padding:10px 18px;border-radius:12px;font-size:15px}
.toast[hidden]{display:none}
"""

JS = r"""
const $ = id => document.getElementById(id);
const el = (t, c, txt) => { const n = document.createElement(t); if (c) n.className = c; if (txt != null) n.textContent = txt; return n; };
const gbp = x => '£' + x.toFixed(2);
let P = [], CATS = [], PAY = {}, cat = '', basket = [], idle = null, busy = false;
const byId = id => P.find(p => p.id === id);
async function api(path, body) {
  const res = await fetch('api/' + path, body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Sok': '1'}, body: JSON.stringify(body)} : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || 'Something went wrong.'), data);
  return data;
}
async function load() { const d = await api('catalogue'); P = d.products; CATS = d.categories; PAY = d.pay; }
function show(s) {
  for (const n of document.querySelectorAll('.screen')) n.hidden = n.id !== 's-' + s;
  clearTimeout(idle);
  if (s === 'done' || s === 'declined') idle = setTimeout(reset, 30000);
  if (s === 'scan') idle = setTimeout(reset, 180000);
}
function reset() { basket = []; show('welcome'); }
function toast(msg) { const t = $('toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 2200); }
function beep() { try { const a = new (window.AudioContext || window.webkitAudioContext)(); const o = a.createOscillator(), g = a.createGain(); o.frequency.value = 1500; g.gain.value = .05; o.connect(g); g.connect(a.destination); o.start(); o.stop(a.currentTime + .08); } catch (err) {} }

function scan(id) {
  const p = byId(id);
  if (!p) { toast("That barcode isn't one of ours."); return; }
  const inBasket = basket.find(l => l.id === id);
  if ((inBasket ? inBasket.qty : 0) >= Math.min(20, p.stock)) { toast('Sorry, no more ' + p.name + ' in stock.'); return; }
  if (inBasket) inBasket.qty++; else basket.push({id, qty: 1});
  beep(); show('scan'); renderTill();
  const t = document.querySelector('[data-id="' + id + '"]'); if (t) { t.classList.add('flash'); setTimeout(() => t.classList.remove('flash'), 300); }
}
function renderShelf() {
  const chips = $('chips'); chips.replaceChildren();
  for (const [id, name] of [['', 'All'], ...CATS]) { const b = el('button', 'chip', name); b.type = 'button'; b.setAttribute('aria-pressed', String(cat === id)); b.onclick = () => { cat = id; renderShelf(); }; chips.append(b); }
  const tiles = $('tiles'); tiles.replaceChildren();
  for (const p of P.filter(p => !cat || p.cat === cat)) {
    const b = el('button', 'tile'); b.type = 'button'; b.dataset.id = p.id; b.disabled = p.stock <= 0;
    const img = el('div', 'img'); img.style.background = p.color; img.innerHTML = '<svg viewBox="0 0 24 24">' + (ICONS[p.cat] || '') + '</svg>';
    const t = el('div', 't'); t.append(el('span', '', p.name), el('b', '', gbp(p.price)));
    b.append(img, t); b.onclick = () => scan(p.id); tiles.append(b);
  }
}
const total = () => basket.reduce((s, l) => s + byId(l.id).price * l.qty, 0);
function renderTill() {
  const box = $('items'); box.replaceChildren();
  const n = basket.reduce((s, l) => s + l.qty, 0);
  $('count').textContent = n + (n === 1 ? ' item' : ' items');
  if (!n) box.append(el('p', 'empty', 'Scan your first item'));
  for (const l of [...basket].reverse()) {
    const p = byId(l.id), r = el('div', 'it');
    r.append(el('span', '', p.name), el('b', '', gbp(p.price * l.qty)));
    const c = el('div', 'ctl'), m = el('button', '', '−'), q = el('span', '', '× ' + l.qty), pl = el('button', '', '+'), rm = el('button', 'rm', 'Remove');
    m.type = pl.type = rm.type = 'button'; m.ariaLabel = 'One fewer ' + p.name; pl.ariaLabel = 'One more ' + p.name;
    m.onclick = () => { l.qty--; if (!l.qty) basket = basket.filter(x => x !== l); renderTill(); };
    pl.onclick = () => scan(l.id);
    rm.onclick = () => { basket = basket.filter(x => x !== l); renderTill(); };
    c.append(m, q, pl, rm); r.append(c); box.append(r);
  }
  $('total').textContent = gbp(total()); $('to-pay').disabled = !n;
}
function renderPay() {
  $('pay-total').textContent = gbp(total());
  const c = PAY.dmobnk, b = $('card-dmobnk');
  b.disabled = !c;
  $('dmobnk-sub').textContent = !c ? "DMOBNK isn't on this cluster" : c.frozen ? 'Frozen in online banking' : 'Credit card · authorised by DMOBNK';
}
async function pay(method) {
  if (busy) return; busy = true; show('paying');
  try {
    const o = await api('pay', {items: basket.map(l => ({id: l.id, qty: l.qty})), method});
    $('r-lines').replaceChildren(...o.lines.map(l => { const d = el('div', 'r'); d.append(el('span', '', l.qty + ' × ' + l.name), el('span', '', gbp(l.amount))); return d; }));
    $('r-total').textContent = gbp(o.total); $('r-ref').textContent = o.ref; $('r-time').textContent = new Date(o.t * 1000).toLocaleString();
    $('r-card').textContent = (o.method === 'dmobnk' ? 'DMOBNK •••• ' + o.card : 'Card •••• ' + o.card) + '  auth ' + o.auth;
    $('r-by').textContent = o.by + (o.pod ? ' · till ' + o.pod : '');
    basket = []; show('done');
  } catch (e) {
    $('why').textContent = e.message; show('declined');
  } finally { busy = false; load().then(renderShelf).catch(() => {}); }
}
$('start').onclick = () => { show('scan'); renderTill(); };
$('code').addEventListener('keydown', ev => { if (ev.key === 'Enter') { scan($('code').value.trim()); $('code').value = ''; } });
$('code-go').onclick = () => { scan($('code').value.trim()); $('code').value = ''; };
$('to-pay').onclick = async () => { await load().catch(() => {}); renderPay(); show('pay'); };
$('pay-back').onclick = () => { show('scan'); renderTill(); };
$('card-dmobnk').onclick = () => pay('dmobnk');
$('card-other').onclick = () => pay('other');
$('cancel').onclick = reset;
$('done-new').onclick = reset;
$('dec-back').onclick = async () => { await load().catch(() => {}); renderPay(); show('pay'); };
$('dec-cancel').onclick = reset;
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); }, 1000);
load().then(() => { renderShelf(); renderTill(); }).catch(() => toast("Can't reach the store system. Retrying…"));
setInterval(() => { if (!busy) load().then(renderShelf).catch(() => {}); }, 15000);
"""


def page():
    hint = "Tap an item to scan it, or type a barcode (try " + e(next(iter(store.PRODUCTS))) + ")."
    body = f"""{store.banner()}
<header><div class="in"><div class="brand">{store.LOGO}<span><b>DMOSHP</b><small>{e(STORE)} · Self-checkout</small></span></div><span class="clock" id="clock"></span></div></header>
<main>
<section class="screen welcome" id="s-welcome">
  <h1>Welcome to DMOSHP {e(STORE)}</h1>
  <p class="lede">Scan your shopping and pay by card. It only takes a minute.</p>
  <button class="big" id="start" type="button">Start</button>
</section>
<section class="screen" id="s-scan" hidden>
  <div class="scan">
    <div class="shelf">
      <div class="bar"><input id="code" inputmode="numeric" autocomplete="off" placeholder="Barcode" aria-label="Type a barcode"><button id="code-go" type="button">Scan</button></div>
      <p class="lede" style="font-size:15px">{hint}</p>
      <div class="chips" id="chips"></div>
      <div class="tiles" id="tiles"></div>
    </div>
    <div class="till">
      <div class="hd"><h2>Your shopping</h2><span class="lede" id="count">0 items</span></div>
      <div class="items" id="items" aria-live="polite"></div>
      <div class="ft"><div class="tot"><span>Total</span><span id="total">£0.00</span></div>
        <button class="big" id="to-pay" type="button" disabled>Pay</button>
        <button class="big ghost" id="cancel" type="button" style="padding:12px;font-size:17px">Cancel</button></div>
    </div>
  </div>
</section>
<section class="screen pay" id="s-pay" hidden>
  <h1>Pay <span id="pay-total"></span></h1>
  <p class="lede">Tap your card.</p>
  <div class="cards">
    <button class="cc" id="card-dmobnk" type="button" style="background:linear-gradient(135deg,#0b1b3a,#2a4fd6)"><b>DMOBNK</b><span class="num">•••• •••• •••• 4821</span><small id="dmobnk-sub"></small></button>
    <button class="cc" id="card-other" type="button" style="background:linear-gradient(135deg,#4d5a53,#93a39a)"><b>Another card</b><span class="num">•••• •••• •••• 0000</span><small>Simulated: always approved</small></button>
  </div>
  <button class="big ghost" id="pay-back" type="button">Back</button>
</section>
<section class="screen pay" id="s-paying" hidden><div class="spin" role="status" aria-label="Paying"></div><h2>Contacting your bank…</h2></section>
<section class="screen pay" id="s-done" hidden>
  <div class="good">✓</div><h1>Thank you</h1><p class="lede">Please take your receipt and your shopping.</p>
  <div class="receipt"><b>DMOSHP {e(STORE)}</b><span id="r-time"></span><hr><div id="r-lines"></div><hr>
    <div class="r"><b>TOTAL</b><b id="r-total"></b></div><span id="r-card"></span><span>Ref <span id="r-ref"></span></span><small class="lede" id="r-by"></small></div>
  <button class="big" id="done-new" type="button">Done</button>
</section>
<section class="screen pay" id="s-declined" hidden>
  <div class="bad">!</div><h1>Payment declined</h1><p class="lede" id="why"></p>
  <div class="row"><button class="big" id="dec-back" type="button">Try another card</button><button class="big ghost" id="dec-cancel" type="button">Cancel</button></div>
</section>
</main>
<div class="toast" id="toast" role="status" hidden></div>
{store.footer()}"""
    js = "const ICONS = " + json.dumps(store.ICONS) + ";\n" + JS
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>DMOSHP self-checkout</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap">
<style>{CSS}</style></head><body>{body}<script>{js.replace("</", "<\\/")}</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        try:
            if u.path == "/healthz":
                store.r("PING")
                return self._json(200, {"ok": True})
            if u.path == "/":
                return self._send(200, "text/html; charset=utf-8", page())
            if u.path == "/api/catalogue":
                return self._json(200, {"products": store.catalogue(), "categories": store.CATEGORIES,
                                        "pay": store.pay_options()})
        except Exception as err:
            print(f"error on {u.path}: {err}", flush=True)
            return self._json(503, {"error": "The till is busy. Try again in a moment."})
        self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if self.headers.get("X-Sok") != "1":
            return self._json(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad request"})
        try:
            if u.path == "/api/pay":
                return self._json(*store.place(body.get("items"), body.get("method"), "sok", STORE))
        except Exception as err:
            print(f"error on {u.path}: {err}", flush=True)
            return self._json(503, {"error": "The till is busy. Try again in a moment."})
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def _seed():
    while True:
        try:
            store.seed()
            return
        except Exception:
            time.sleep(2)


threading.Thread(target=_seed, daemon=True).start()
ingress_host.start("sok-web", "sok", os.environ.get("SOK_HOST", ""), os.environ.get("INGRESS_CLASS", ""),
                   path="/", service="sok-web", owner="sok-web")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
