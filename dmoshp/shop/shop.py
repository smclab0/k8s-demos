"""DMOSHP online: the web shop of DMOCRP's retail arm. A catalogue with live
stock (shared with the in-store self-checkout, ../sok), a basket, checkout
with a DMOBNK card or a simulated one, and order tracking.

It points its own Ingress at the cluster it runs on (shop.<address>.sslip.io,
../common/ingress_host.py). Settings, all optional, in the dmoshp-shop
ConfigMap: host, ingressClass."""
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

WHERE = "online"
crm_feed.SOURCE.update(app="shop", where="DMOSHP online")

CSS = store.CHROME_CSS + """
:root{--g:#0f7a4f;--g2:#e3f6ec;--ink:#13221b;--muted:#5d6b63;--line:#dfe7e2;--bg:#f6f8f5;--card:#fff;--red:#b42335}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
h1,h2,h3{font-family:"Space Grotesk",Inter,sans-serif;margin:0;letter-spacing:-.01em}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
.wrap{max-width:1240px;margin:0 auto;padding-left:16px;padding-right:16px}
header{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header .wrap{display:flex;align-items:center;gap:14px;padding-block:12px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:inherit;flex:none}
.brand b{font-family:"Space Grotesk",sans-serif;font-size:21px;letter-spacing:.02em;display:block;line-height:1.1}
.brand small{color:var(--muted);font-size:12px}
.search{flex:1;min-width:0;display:flex}
.search input{width:100%;border:1px solid var(--line);border-radius:999px;padding:9px 16px;background:var(--bg)}
.search input:focus{outline:2px solid var(--g);outline-offset:1px;background:#fff}
.bag{position:relative;border:0;background:var(--g);color:#fff;border-radius:999px;padding:9px 16px;font-weight:600;display:flex;gap:8px;align-items:center;flex:none}
.bag .n{background:#fff;color:var(--g);border-radius:999px;min-width:22px;padding:0 6px;font-size:12px;font-weight:700;text-align:center}
.linkbtn{border:0;background:none;color:var(--g);font-weight:600;padding:6px 4px;flex:none}
.hero{background:linear-gradient(120deg,#0f7a4f,#0b5a3a 60%,#13221b);color:#fff;border-radius:18px;padding:28px;margin:22px 0;display:flex;flex-wrap:wrap;justify-content:space-between;gap:16px;align-items:flex-end}
.hero h1{font-size:34px;line-height:1.1}
.hero p{margin:8px 0 0;color:#cfeedd;max-width:560px}
.hero .tag{font-size:13px;color:#cfeedd}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.chip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 14px;font-weight:500}
.chip[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:14px;margin-bottom:28px}
.p{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;display:flex;flex-direction:column}
.p .img{aspect-ratio:4/3;display:grid;place-items:center}
.p .img svg{width:46%;height:46%;stroke:#fff;fill:none;stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round;opacity:.92}
.p .body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:4px;flex:1}
.p .name{font-weight:600;line-height:1.3}
.p .size{color:var(--muted);font-size:13px}
.p .row{display:flex;justify-content:space-between;align-items:center;margin-top:auto;padding-top:8px;gap:8px}
.p .price{font-weight:700;font-size:17px}
.stock{font-size:12px;color:var(--muted)}
.stock.low{color:#9a5200;font-weight:600}.stock.out{color:var(--red);font-weight:600}
.add{border:0;background:var(--g2);color:var(--g);font-weight:700;border-radius:10px;padding:7px 12px}
.add:disabled{opacity:.45;cursor:not-allowed}
.qty{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.qty button{border:0;background:#fff;width:32px;height:32px;font-size:17px}
.qty span{min-width:26px;text-align:center;font-weight:600}
.drawer{position:fixed;inset:0;background:rgba(10,20,15,.35);display:flex;justify-content:flex-end;z-index:20}
.drawer[hidden]{display:none}
.panel{background:#fff;width:min(420px,100%);height:100%;display:flex;flex-direction:column}
.panel .hd{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid var(--line)}
.panel .bd{flex:1;overflow:auto;padding:8px 18px}
.panel .ft{border-top:1px solid var(--line);padding:16px 18px;display:flex;flex-direction:column;gap:10px}
.line{display:grid;grid-template-columns:44px 1fr auto;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.sw{width:44px;height:44px;border-radius:10px}
.tot{display:flex;justify-content:space-between;font-weight:700;font-size:18px}
.btn{border:0;background:var(--g);color:#fff;font-weight:700;border-radius:12px;padding:13px 18px;font-size:16px;text-align:center}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.x{border:0;background:none;font-size:24px;line-height:1;padding:4px 8px;color:var(--muted)}
.view{padding:22px 0 30px}
.cols{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:22px;align-items:start}
@media (max-width:820px){.cols{grid-template-columns:1fr}.hero h1{font-size:27px}.brand small{display:none}}
@media (max-width:520px){header .wrap{flex-wrap:wrap}.search{order:3;flex-basis:100%}.bag .lbl{display:none}}
.box{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;display:flex;flex-direction:column;gap:14px}
label{font-weight:600;font-size:14px;display:flex;flex-direction:column;gap:6px}
input[type=text],input[type=email]{border:1px solid var(--line);border-radius:10px;padding:11px 12px;width:100%}
.opt{display:flex;flex-direction:row;gap:12px;align-items:center;border:1px solid var(--line);border-radius:12px;padding:12px 14px;cursor:pointer;font-weight:500}
.opt:has(input:checked){border-color:var(--g);background:var(--g2)}
.opt.off{opacity:.55;cursor:not-allowed}
.opt small{display:block;color:var(--muted);font-weight:400}
.cardart{width:54px;height:34px;border-radius:6px;flex:none;background:linear-gradient(135deg,#0b1b3a,#2a4fd6);color:#fff;font-size:9px;font-weight:700;padding:4px 5px;letter-spacing:.04em}
.cardart.o{background:linear-gradient(135deg,#6b7a72,#a9b5ae)}
.err{color:var(--red);font-weight:600;margin:0}
.ok{display:flex;gap:12px;align-items:center}
.tick{width:44px;height:44px;border-radius:50%;background:var(--g);color:#fff;display:grid;place-items:center;font-size:24px;flex:none}
table{width:100%;border-collapse:collapse}
td{padding:7px 0;border-bottom:1px solid var(--line)}
td:last-child{text-align:right}
.muted{color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:40px 0}
"""

JS = r"""
const $ = id => document.getElementById(id);
const el = (t, c, txt) => { const n = document.createElement(t); if (c) n.className = c; if (txt != null) n.textContent = txt; return n; };
const gbp = x => '£' + x.toFixed(2);
let P = [], CATS = [], PAY = {}, cat = '', q = '';
let basket = {};
try { basket = JSON.parse(localStorage.getItem('dmoshp-basket') || '{}') || {}; } catch (err) { basket = {}; }
function save() { try { localStorage.setItem('dmoshp-basket', JSON.stringify(basket)); } catch (err) {} }
function myOrders() { try { return JSON.parse(localStorage.getItem('dmoshp-orders') || '[]'); } catch (err) { return []; } }
function remember(ref) { try { localStorage.setItem('dmoshp-orders', JSON.stringify([ref, ...myOrders().filter(r => r !== ref)].slice(0, 10))); } catch (err) {} }
const byId = id => P.find(p => p.id === id);
const icon = cat => '<svg viewBox="0 0 24 24">' + (ICONS[cat] || '') + '</svg>';

async function api(path, body) {
  const res = await fetch('api/' + path, body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Shop': '1'}, body: JSON.stringify(body)} : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || 'Something went wrong. Try again.'), data);
  return data;
}
async function load() {
  const d = await api('catalogue'); P = d.products; CATS = d.categories; PAY = d.pay;
  for (const id of Object.keys(basket)) if (!byId(id)) delete basket[id];
}

function stockLabel(p) {
  const s = el('span', 'stock');
  if (p.stock <= 0) { s.classList.add('out'); s.textContent = 'Out of stock'; }
  else if (p.stock <= 5) { s.classList.add('low'); s.textContent = 'Only ' + p.stock + ' left'; }
  else s.textContent = p.stock + ' in stock';
  return s;
}
function qtyCtl(p) {
  const w = el('span', 'qty'), minus = el('button', '', '−'), n = el('span', '', String(basket[p.id])), plus = el('button', '', '+');
  minus.type = plus.type = 'button'; minus.ariaLabel = 'One fewer ' + p.name; plus.ariaLabel = 'One more ' + p.name;
  minus.onclick = () => setQty(p.id, basket[p.id] - 1); plus.onclick = () => setQty(p.id, basket[p.id] + 1);
  plus.disabled = basket[p.id] >= Math.min(20, p.stock);
  w.append(minus, n, plus); return w;
}
function setQty(id, n) { if (n <= 0) delete basket[id]; else basket[id] = n; save(); render(); }

function renderShop() {
  const chips = $('chips'); chips.replaceChildren();
  for (const [id, name] of [['', 'All'], ...CATS]) {
    const b = el('button', 'chip', name); b.type = 'button'; b.setAttribute('aria-pressed', String(cat === id));
    b.onclick = () => { cat = id; renderShop(); }; chips.append(b);
  }
  const grid = $('grid'); grid.replaceChildren();
  const ql = q.trim().toLowerCase();
  const list = P.filter(p => (!cat || p.cat === cat) && (!ql || p.name.toLowerCase().includes(ql)));
  if (!list.length) grid.append(el('p', 'empty', 'Nothing matches "' + q + '".'));
  for (const p of list) {
    const c = el('article', 'p'), img = el('div', 'img'); img.style.background = 'linear-gradient(135deg,' + p.color + ',' + p.color + 'cc)'; img.innerHTML = icon(p.cat);
    const b = el('div', 'body'); b.append(el('span', 'name', p.name), el('span', 'size', p.size || ' '), stockLabel(p));
    const row = el('div', 'row'); row.append(el('span', 'price', gbp(p.price)));
    if (basket[p.id]) row.append(qtyCtl(p));
    else { const a = el('button', 'add', 'Add'); a.type = 'button'; a.disabled = p.stock <= 0; a.onclick = () => setQty(p.id, 1); a.ariaLabel = 'Add ' + p.name; row.append(a); }
    b.append(row); c.append(img, b); grid.append(c);
  }
}
function lines() { return Object.entries(basket).map(([id, qty]) => ({p: byId(id), qty})).filter(l => l.p); }
const total = () => lines().reduce((s, l) => s + l.p.price * l.qty, 0);
function renderBag() {
  const n = lines().reduce((s, l) => s + l.qty, 0); $('bag-n').textContent = n;
  const bd = $('bag-bd'); bd.replaceChildren();
  if (!n) bd.append(el('p', 'empty', 'Your basket is empty.'));
  for (const l of lines()) {
    const r = el('div', 'line'), sw = el('div', 'sw'); sw.style.background = l.p.color;
    const mid = el('div'); mid.append(el('div', '', l.p.name)); const pr = el('div', 'muted', gbp(l.p.price) + ' each'); pr.style.fontSize = '13px'; mid.append(pr);
    const right = el('div'); right.style.cssText = 'display:flex;flex-direction:column;align-items:flex-end;gap:6px'; right.append(el('b', '', gbp(l.p.price * l.qty)), qtyCtl(l.p));
    r.append(sw, mid, right); bd.append(r);
  }
  $('bag-tot').textContent = gbp(total()); $('to-checkout').disabled = !n;
}
function payOption(value, title, sub, art, off) {
  const o = el('label', 'opt' + (off ? ' off' : '')), i = el('input'); i.type = 'radio'; i.name = 'pay'; i.value = value; i.disabled = !!off;
  const a = el('span', 'cardart' + (value === 'other' ? ' o' : ''), art); const t = el('span', '', title); t.append(el('small', '', sub));
  o.append(i, a, t); return o;
}
function renderCheckout() {
  const t = $('co-lines'); t.replaceChildren();
  for (const l of lines()) { const r = el('tr'); r.append(el('td', '', l.qty + ' × ' + l.p.name), el('td', '', gbp(l.p.price * l.qty))); t.append(r); }
  const r = el('tr'); const a = el('td'), b = el('td'); a.append(el('b', '', 'Total')); b.append(el('b', '', gbp(total()))); r.append(a, b); t.append(r);
  const box = $('co-pay'); box.replaceChildren();
  const c = PAY.dmobnk;
  if (c) box.append(payOption('dmobnk', 'DMOBNK credit card •••• ' + c.last4, c.frozen ? 'Frozen in DMOBNK online banking: payments will be declined' : gbp(c.available) + ' available · authorised live by DMOBNK', 'DMOBNK', false));
  else box.append(payOption('dmobnk', 'DMOBNK credit card', "DMOBNK isn't running on this cluster", 'DMOBNK', true));
  box.append(payOption('other', 'Another card', 'A simulated card that is always approved', 'CARD', false));
  const first = box.querySelector('input:not(:disabled)'); if (first) first.checked = true;
  $('co-err').hidden = true;
}
function renderDone(o) {
  $('done-ref').textContent = o.ref; $('done-total').textContent = gbp(o.total);
  $('done-card').textContent = o.method === 'dmobnk' ? 'DMOBNK card •••• ' + o.card + ' (auth ' + o.auth + ')' : 'Simulated card (auth ' + o.auth + ')';
  $('done-by').textContent = o.by + (o.pod ? ' · order taken by ' + o.pod : '');
  const t = $('done-lines'); t.replaceChildren();
  for (const l of o.lines) { const r = el('tr'); r.append(el('td', '', l.qty + ' × ' + l.name), el('td', '', gbp(l.amount))); t.append(r); }
}
async function renderOrders() {
  const box = $('orders-list'); box.replaceChildren();
  const refs = myOrders();
  if (!refs.length) { box.append(el('p', 'muted', "You haven't ordered anything from this browser yet.")); return; }
  for (const ref of refs) {
    try {
      const o = await api('order?ref=' + encodeURIComponent(ref));
      const r = el('div', 'line'); const sw = el('div', 'sw'); sw.style.background = '#0f7a4f';
      const mid = el('div'); mid.append(el('b', '', o.ref)); const s = el('div', 'muted', new Date(o.t * 1000).toLocaleString() + ' · ' + o.lines.reduce((n, l) => n + l.qty, 0) + ' items'); s.style.fontSize = '13px'; mid.append(s);
      r.append(sw, mid, el('b', '', gbp(o.total))); box.append(r);
    } catch (err) {}
  }
}

let view = 'shop';
function go(v) { view = v; location.hash = v === 'shop' ? '' : v; render(); scrollTo(0, 0); }
function render() {
  for (const v of ['shop', 'checkout', 'done', 'orders']) $('v-' + v).hidden = view !== v;
  renderBag();
  if (view === 'shop') renderShop();
  if (view === 'checkout') { if (!lines().length) return go('shop'); renderCheckout(); }
  if (view === 'orders') renderOrders();
}

$('q').addEventListener('input', ev => { q = ev.target.value; if (view !== 'shop') go('shop'); else renderShop(); });
$('bag').onclick = () => { $('drawer').hidden = false; renderBag(); };
$('bag-x').onclick = () => { $('drawer').hidden = true; };
$('drawer').onclick = ev => { if (ev.target.id === 'drawer') $('drawer').hidden = true; };
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') $('drawer').hidden = true; });
$('to-checkout').onclick = async () => { $('drawer').hidden = true; await load().catch(() => {}); go('checkout'); };
$('home').onclick = ev => { ev.preventDefault(); go('shop'); };
$('my-orders').onclick = () => go('orders');
$('done-back').onclick = () => go('shop');
$('co-back').onclick = () => go('shop');
$('co-form').onsubmit = async ev => {
  ev.preventDefault();
  const btn = $('co-go'), err = $('co-err'); err.hidden = true; btn.disabled = true; btn.textContent = 'Paying…';
  const pay = (document.querySelector('input[name=pay]:checked') || {}).value;
  try {
    const o = await api('order', {items: lines().map(l => ({id: l.p.id, qty: l.qty})), method: pay, name: $('co-name').value});
    basket = {}; save(); remember(o.ref); renderDone(o); go('done'); load().catch(() => {});
  } catch (e) {
    err.textContent = e.declined ? 'Payment declined: ' + e.message : e.message; err.hidden = false;
    await load().catch(() => {}); renderCheckout(); err.hidden = false;
  } finally { btn.disabled = false; btn.textContent = 'Pay now'; }
};
load().then(() => { const h = location.hash.slice(1); go(h === 'orders' ? 'orders' : h === 'checkout' ? 'checkout' : 'shop'); })
  .catch(() => { $('grid').replaceChildren(el('p', 'empty', "The shop can't load right now. Refresh in a moment.")); });
setInterval(() => { if (view === 'shop') load().then(renderShop).catch(() => {}); }, 10000);
"""


def page():
    body = f"""{store.banner()}
<header><div class="wrap">
  <a class="brand" id="home" href="./">{store.LOGO}<span><b>DMOSHP</b><small>a DMOCRP company</small></span></a>
  <div class="search"><input id="q" type="search" placeholder="Search groceries, home and tech" aria-label="Search the shop"></div>
  <button class="linkbtn" id="my-orders" type="button">Orders</button>
  <button class="bag" id="bag" type="button" aria-label="Basket"><span class="lbl">Basket</span><span class="n" id="bag-n">0</span></button>
</div></header>
<main class="wrap">
  <section id="v-shop">
    <div class="hero"><div><h1>Everyday shopping, delivered</h1><p>Fresh food, bakery, home and tech. Stock is live: the same shelves as our self-checkout stores.</p></div>
      <span class="tag">Pay with your DMOBNK card</span></div>
    <div class="chips" id="chips" role="toolbar" aria-label="Categories"></div>
    <div class="grid" id="grid"><p class="empty">Loading the shop…</p></div>
  </section>
  <section id="v-checkout" class="view" hidden>
    <form id="co-form" class="cols">
      <div class="box">
        <h2>Checkout</h2>
        <label>Name for the order<input id="co-name" type="text" autocomplete="off" placeholder="e.g. Sam Taylor" maxlength="40"></label>
        <div style="display:flex;flex-direction:column;gap:8px"><b style="font-size:14px">Pay with</b><div id="co-pay" style="display:flex;flex-direction:column;gap:8px"></div></div>
        <p class="err" id="co-err" role="alert" hidden></p>
        <button class="btn" id="co-go" type="submit">Pay now</button>
        <button class="btn ghost" id="co-back" type="button">Keep shopping</button>
      </div>
      <div class="box"><h3>Your order</h3><table><tbody id="co-lines"></tbody></table>
        <p class="muted" style="font-size:13px;margin:0">Demo delivery: nothing is actually sent.</p></div>
    </form>
  </section>
  <section id="v-done" class="view" hidden>
    <div class="cols">
      <div class="box">
        <div class="ok"><span class="tick">✓</span><div><h2>Order placed</h2><span class="muted">Reference <b id="done-ref"></b></span></div></div>
        <table><tbody id="done-lines"></tbody></table>
        <div class="tot"><span>Paid</span><span id="done-total"></span></div>
        <div class="muted" style="font-size:14px"><div id="done-card"></div><div id="done-by" style="font-size:12px"></div></div>
        <button class="btn" id="done-back" type="button">Back to the shop</button>
      </div>
    </div>
  </section>
  <section id="v-orders" class="view" hidden>
    <div class="box" style="max-width:640px"><h2>Your orders</h2><div id="orders-list"></div></div>
  </section>
</main>
<div class="drawer" id="drawer" hidden><div class="panel" role="dialog" aria-modal="true" aria-label="Basket">
  <div class="hd"><h2>Basket</h2><button class="x" id="bag-x" type="button" aria-label="Close">×</button></div>
  <div class="bd" id="bag-bd"></div>
  <div class="ft"><div class="tot"><span>Total</span><span id="bag-tot">£0.00</span></div><button class="btn" id="to-checkout" type="button">Checkout</button></div>
</div></div>
{store.footer()}"""
    js = "const ICONS = " + json.dumps(store.ICONS) + ";\n" + JS
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>DMOSHP</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600;700&display=swap">
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
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/healthz":
                store.r("PING")
                return self._json(200, {"ok": True})
            if u.path == "/":
                return self._send(200, "text/html; charset=utf-8", page())
            if u.path == "/api/catalogue":
                return self._json(200, {"products": store.catalogue(), "categories": store.CATEGORIES,
                                        "pay": store.pay_options()})
            if u.path == "/api/order":
                o = store.order(q.get("ref", ""))
                return self._json(200, o) if o else self._json(404, {"error": "No order with that reference."})
        except Exception as err:
            print(f"error on {u.path}: {err}", flush=True)
            return self._json(503, {"error": "The shop is busy. Try again in a moment."})
        self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if self.headers.get("X-Shop") != "1":
            return self._json(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad request"})
        try:
            if u.path == "/api/order":
                return self._json(*store.place(body.get("items"), body.get("method"), "shop", WHERE,
                                               str(body.get("name") or "")))
        except Exception as err:
            print(f"error on {u.path}: {err}", flush=True)
            return self._json(503, {"error": "The shop is busy. Try again in a moment."})
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
ingress_host.start("shop-web", "shop", os.environ.get("SHOP_HOST", ""), os.environ.get("INGRESS_CLASS", ""),
                   path="/", service="shop-web", owner="shop-web")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
