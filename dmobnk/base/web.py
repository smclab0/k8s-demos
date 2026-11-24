"""DMOBNK web front end. All the page text, the currency and the number
formats come from the mounted locale ConfigMap: locale-uk by default, or
another country's via the overlays in ../countries/."""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import control  # Kube Power, the admin-only control room at /dmobnk/admin

sys.path.insert(0, "/opt/dmobnk")
import ingress_host  # noqa: E402  (mounted from the dmobnk-ingress-host ConfigMap)

L = json.load(open("/locale/locale.json", encoding="utf-8"))
T = L["t"]
BASE = "/dmobnk/"
# Old per-country links (/dmobnk/de/cards etc.) redirect to the same page.
OLD_PREFIXES = ("/dmobnk/uk", "/dmobnk/us", "/dmobnk/de", "/dmobnk/sim")
SERVICES = [("accounts", None), ("cards", "cards"), ("loans", None),
            ("mortgage", "mortgage"), ("rates", "rates"), ("cross", None)]

# ---------------------------------------------------------------- backend calls
_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def svc(name, path, method="GET", timeout=4):
    req = urllib.request.Request(f"http://{name}:8080{path}", method=method,
                                 data=b"" if method == "POST" else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.load(err)
        except ValueError:
            return err.code, {"error": "bad response"}
    except Exception:
        return 503, {"error": "unavailable"}


def rates_local():
    code, data = cached("rates", 2, lambda: svc("rates-svc", "/rates"))
    if code != 200:
        return None
    per, prev = data["per_gbp"], data["prev_per_gbp"]
    base = per[L["currency"]]
    pbase = prev.get(L["currency"], base)
    rows = []
    for c in L["fx_codes"]:
        mid = per[c] / base
        pmid = prev.get(c, per[c]) / pbase
        rows.append({"code": c, "name": L["currency_names"].get(c, c), "mid": mid,
                     "buy": mid * 1.025, "sell": mid * 0.975, "up": mid >= pmid})
    unit = L["gold_unit_g"]
    return {"rows": rows, "gold": data["gold_gbp_per_g"] * base * unit,
            "gold_hist": [g * base * unit for g in data["gold_hist"]],
            "updated": data["updated"], "per_gbp_base": base}


# ---------------------------------------------------------------- HTML
def e(s):
    return html.escape(str(s), quote=True)


def fmt(key, **kw):
    s = T[key]
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


ICONS = {
    "accounts": '<path d="M3 10 12 4l9 6"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8"/><path d="M3 20h18"/>',
    "cards": '<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 9.5h19"/><path d="M6 15h4"/>',
    "loans": '<rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/><path d="M6.5 9.5v5M17.5 9.5v5"/>',
    "mortgage": '<path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-5h4v5"/>',
    "rates": '<path d="M3 20h18"/><path d="M5 20l2-6h10l2 6"/><path d="M8 14l1.5-4h5L16 14"/><path d="M14 4l2 2 4-4"/>',
    "cross": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f8fc;color:#0b1b3a;font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:#1f4fe0}a:hover{color:#163aa8}
button{font:inherit;cursor:pointer}
button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #7fa2ff;outline-offset:2px}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace}
.disp{font-family:"Space Grotesk",Inter,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
header.main{background:#fff;border-bottom:1px solid #dfe5f1}
header.main .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:12px 28px;padding-block:14px}
.logo{display:flex;align-items:center;gap:10px;color:#0b1b3a;text-decoration:none}
.logo b{font:700 22px/1.1 "Space Grotesk",sans-serif;letter-spacing:.04em;display:block}
.logo small{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#4a5a7d}
nav.links{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:14px;font-weight:600;flex:1}
nav.links a{color:#0b1b3a;text-decoration:none;padding:6px 0;border-bottom:2px solid transparent}
nav.links a[aria-current]{border-color:#2453ff;color:#1f4fe0}
main{padding-block:32px 48px;display:flex;flex-direction:column;gap:36px}
h1{margin:0;font:700 clamp(34px,5vw,52px)/1.05 "Space Grotesk",sans-serif;letter-spacing:-1.4px;text-wrap:balance;hyphens:auto}
h2{margin:0;font:700 22px/1.2 "Space Grotesk",sans-serif;letter-spacing:-.4px}
h3{margin:0;font-size:18px}
p{margin:0}
.muted{color:#4a5a7d}
.lede{font-size:17px;color:#3d4c6d;max-width:58ch}
.eyebrow{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#1f4fe0}
.row{display:flex;flex-wrap:wrap;gap:24px;align-items:flex-start}
.grow{flex:999 1 600px;min-width:0;display:flex;flex-direction:column;gap:24px}
.card{background:#fff;border:1px solid #dfe5f1;border-radius:18px;padding:20px;display:flex;flex-direction:column;gap:12px;min-width:0}
.btn{min-height:44px;padding:0 20px;border:0;border-radius:12px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
.btn.blue{background:#2453ff;color:#fff}.btn.navy{background:#0b1b3a;color:#fff}.btn.red{background:#c42b3c;color:#fff}
.btn.amber{background:#e8890c;color:#1d1200}.btn.ghost{background:#fff;color:#1f4fe0;border:2px solid #2453ff}
.btn:disabled{opacity:.55;cursor:progress}
.msg{min-height:1.4em;font-size:13px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.tile .ic{width:48px;height:48px;border-radius:14px;background:#e6ecff;display:grid;place-items:center}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#4a5a7d;padding:8px 10px;border-bottom:1px solid #dfe5f1;text-align:right}
td{padding:9px 10px;border-bottom:1px solid #eef2f8;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
.up{color:#12804a;font-weight:700}.down{color:#b42335;font-weight:700}
.scroll{overflow-x:auto}
.gold{background:#fffaf0;border-color:#f3dcae}
.bars{display:flex;align-items:flex-end;gap:3px;height:60px}
.bars span{flex:1;border-radius:3px 3px 0 0;background:#e8b04a;min-height:2px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.stat{background:#f4f7fd;border-radius:12px;padding:12px}
.stat b{display:block;font:700 22px "Space Grotesk",sans-serif}
.cc{aspect-ratio:1.586;max-width:100%;border-radius:14px;color:#fff;padding:14px;display:flex;flex-direction:column;justify-content:space-between}
label{font-size:13px;font-weight:600}
input{height:44px;border:1px solid #c9d3e8;border-radius:12px;padding:0 12px;font:inherit;color:#0b1b3a;width:100%;background:#fff}
.field{display:flex;flex-direction:column;gap:6px}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.steps li{display:flex;align-items:center;gap:12px}
.steps .num{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:700;background:#eef2f8;color:#4a5a7d;flex-shrink:0}
.steps .done .num{background:#e3f4ea;color:#0d6b3c}.steps .now .num{background:#fdecd3;color:#8a4b00}
.steps .s{margin-left:auto;font-size:13px;font-weight:700;color:#4a5a7d}
.steps .done .s{color:#12804a}.steps .now .s{color:#8a4b00}
footer{border-top:1px solid #dfe5f1;background:#fff;font-size:13px;color:#4a5a7d}
footer .wrap{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;padding-block:20px}
.msg.err{color:#b42335}
.glance{flex:1 1 340px;min-width:0;background:#0b1b3a;color:#fff;border-radius:24px;padding:22px;display:flex;flex-direction:column;gap:14px}
.glance .cc{max-width:280px}
.demo{background:#fdecd3;border-bottom:1px solid #f0c58a;color:#5c3300;font-size:13px}
.demo .wrap{display:flex;align-items:flex-start;gap:10px;padding-block:9px}
.demo b{flex-shrink:0;background:#8a4b00;color:#fff;border-radius:6px;padding:1px 8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-top:1px}
.card .cc{width:100%;max-width:300px}
body{min-height:100vh;display:flex;flex-direction:column}
main.wrap{flex:1;width:100%}
.glance .msg.err{color:#fda4af}
.stats.dark .stat{background:#13274f}
.stats.dark .stat span{font-size:12px;color:#b9c6e4}
"""

LOGO = ('<svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><rect x="2" y="2" width="28" height="28" rx="9" fill="#0b1b3a"/>'
        '<rect x="8" y="17" width="4" height="7" rx="1" fill="#7fa2ff"/><rect x="14" y="12" width="4" height="12" rx="1" fill="#7fa2ff"/>'
        '<rect x="20" y="8" width="4" height="16" rx="1" fill="#e8890c"/></svg>')


def page(name, title, body, extra_js=""):
    pages = [("", T["nav_home"]), ("cards", T["nav_cards"]), ("mortgage", T["nav_mortgage"]), ("rates", T["nav_rates"])]
    cur_page = ' aria-current="page"'
    nav = "".join(f'<a href="{BASE}{p}"{cur_page if p == name else ""}>{e(lbl)}</a>' for p, lbl in pages)
    # Simlish has no real locale; dates use en-GB.
    cfg = {"base": BASE, "lang": L["lang"] if L["country"] != "sim" else "en-GB", "money": L["money"], "t": T}
    cfg_json = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{e(L['lang'])}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · DMOBNK</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">
<style>{CSS}</style>
</head>
<body>
<div class="demo" role="note"><div class="wrap"><b>{e(T['demo_label'])}</b><span>{e(T['demo_banner'])}</span></div></div>
<header class="main"><div class="wrap">
  <a class="logo" href="{BASE}">{LOGO}<span><b>DMOBNK</b><small>Demo Bank Ldn</small></span></a>
  <nav class="links" aria-label="DMOBNK">{nav}</nav>
</div></header>
<main class="wrap">{body}</main>
<footer><div class="wrap"><span>{e(T['footer'])}</span><a href="{BASE}admin" style="color:#4a5a7d">Admin</a></div></footer>
<script id="cfg" type="application/json">{cfg_json}</script>
<script>{COMMON_JS}</script>
<script>{extra_js}</script>
</body>
</html>"""


COMMON_JS = r"""
const CFG = JSON.parse(document.getElementById('cfg').textContent);
const T = CFG.t;
const $ = id => document.getElementById(id);
function tr(key, vals) { let s = T[key] || key; for (const k in (vals || {})) s = s.split('{' + k + '}').join(vals[k]); return s; }
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
async function api(path, opts) {
  const resp = await fetch(CFG.base + 'api/' + path, Object.assign({cache: 'no-store'}, opts || {}));
  let body = {}; try { body = await resp.json(); } catch (err) {}
  if (!resp.ok) throw new Error(body.error || T.error);
  return body;
}
function post(path) { return api(path, {method: 'POST'}); }
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function poll(fn, ms) { const run = () => fn().catch(() => {}); run(); setInterval(run, ms); }
"""


def home():
    tiles = []
    for key, link in SERVICES:
        more = (f'<a href="{BASE}{link}" style="font-weight:700;text-decoration:none">{e(T["open_page"])}</a>'
                if link else f'<span class="muted" style="font-size:14px">{e(T["coming_soon"])}</span>')
        tiles.append(f"""<article class="card tile">
  <div class="ic"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#1f4fe0" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[key]}</svg></div>
  <h3>{e(T['svc_' + key])}</h3>
  <p class="muted" style="font-size:14px">{e(T['svc_' + key + '_d'])}</p>
  {more}
</article>""")
    body = f"""
<section class="row" style="align-items:center;gap:32px">
  <div style="flex:1 1 520px;min-width:0;display:flex;flex-direction:column;gap:16px">
    <span class="eyebrow">{e(T['eyebrow'])}</span>
    <h1>{e(T['h1'])}</h1>
    <p class="lede">{e(T['lede'])}</p>
    <div style="display:flex;flex-wrap:wrap;gap:12px">
      <a class="btn navy" href="{BASE}cards">{e(T['cta_cards'])}</a>
      <a class="btn ghost" href="{BASE}rates">{e(T['cta_rates'])}</a>
    </div>
  </div>
  <aside class="glance" aria-labelledby="gl-h">
    <div class="cc" style="background:linear-gradient(135deg,#2453ff,#13274f)">
      <b class="disp" style="letter-spacing:.06em">DMOBNK</b>
      <span class="mono" style="font-size:13px;letter-spacing:.1em">•••• •••• •••• 4821</span>
    </div>
    <h2 id="gl-h" style="font-size:18px">{e(T['glance_h'])}</h2>
    <div class="stats dark">
      <div class="stat"><span>{e(T['balance'])}</span><b id="g-bal">…</b></div>
      <div class="stat"><span>{e(T['available'])}</span><b id="g-avail">…</b></div>
    </div>
    <p class="msg err" id="g-frozen" hidden>{e(T['frozen_note'])}</p>
    <a href="{BASE}cards" style="color:#9db4ff;font-weight:700;text-decoration:none">{e(T['view_card'])}</a>
  </aside>
</section>
<section aria-labelledby="svc-h" style="display:flex;flex-direction:column;gap:18px">
  <h2 id="svc-h" style="font-size:28px">{e(T['our_services'])}</h2>
  <div class="grid">{''.join(tiles)}</div>
</section>
{rates_blocks()}
"""
    js = r"""
poll(async () => {
  const c = await api('card');
  $('g-bal').textContent = money(c.balance);
  $('g-avail').textContent = money(c.limit - c.balance);
  $('g-frozen').hidden = !c.frozen;
}, 10000);
""" + RATES_JS
    return page("", "Demo Bank Ldn", body, js)


def rates_blocks(full=False):
    return f"""
<section class="row" style="gap:16px">
  <div class="card" style="flex:2 1 520px">
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px">
      <h2>{e(T['fx_h'])}</h2><span class="muted" style="font-size:12px" id="fx-note">…</span>
    </div>
    <div class="scroll"><table style="min-width:420px">
      <thead><tr><th>{e(T['col_cur'])}</th><th>{e(T['col_buy'])}</th><th>{e(T['col_sell'])}</th><th>{e(T['col_today'])}</th></tr></thead>
      <tbody id="fx-body"></tbody>
    </table></div>
  </div>
  <div class="card gold" style="flex:1 1 300px">
    <h2>{e(T['gold_h'])}</h2>
    <span style="font-size:12px;color:#6b4a12">{e(T['gold_note'])}</span>
    <span class="disp" style="font-size:40px;font-weight:700;letter-spacing:-1px" id="gold-price">…</span>
    <div class="bars" aria-hidden="true" id="gold-bars"{' style="height:110px"' if full else ''}></div>
  </div>
</section>"""


RATES_JS = r"""
poll(async () => {
  const d = await api('rates');
  $('fx-note').textContent = tr('fx_note', {unit: money(1, 0), time: hhmm(d.updated)});
  $('fx-body').replaceChildren(...d.rows.map(row => {
    const dp = row.mid >= 20 ? 2 : 4; const r = el('tr');
    const c = el('td'); c.append(el('strong', 'mono', row.code), document.createTextNode(' '), el('span', 'muted', row.name));
    r.append(c, el('td', '', num(row.buy, dp)), el('td', '', num(row.sell, dp)), el('td', row.up ? 'up' : 'down', row.up ? '▲' : '▼'));
    return r;
  }));
  $('gold-price').textContent = money(d.gold);
  const lo = Math.min(...d.gold_hist), hi = Math.max(...d.gold_hist), span = (hi - lo) || 1;
  $('gold-bars').replaceChildren(...d.gold_hist.map(g => {
    const b = el('span'); b.style.height = (20 + 80 * (g - lo) / span) + '%'; return b;
  }));
}, 5000);
"""


def cards_page():
    products = [("everyday", "4821", "#0b1b3a", 24.9), ("rewards", "1907", "#2453ff", 29.9), ("travel", "3366", "#b0650a", 27.9)]
    prod_html = "".join(f"""<article class="card">
  <div class="cc" style="background:{bg}"><b class="disp" style="letter-spacing:.06em">DMOBNK</b><span class="mono" style="font-size:13px;letter-spacing:.1em">•••• •••• •••• {last4}</span></div>
  <h3>{e(T['card_' + k])}</h3><p class="muted" style="font-size:13px">{e(T['card_' + k + '_d'])}</p>
  <div style="display:flex;justify-content:space-between;gap:8px;font-size:13px"><span class="muted">{e(T['apr'])}</span><strong class="apr" data-apr="{apr}">{apr}%</strong></div>
</article>""" for k, last4, bg, apr in products)
    body = f"""
<section style="display:flex;flex-direction:column;gap:8px"><h1 style="font-size:40px">{e(T['svc_cards'])}</h1><p class="lede">{e(T['cards_lede'])}</p></section>
<section style="display:flex;flex-direction:column;gap:14px" aria-labelledby="ch"><h2 id="ch" style="font-size:18px">{e(T['choose'])}</h2>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">{prod_html}</div></section>
<section class="card" aria-labelledby="mine-h">
  <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px">
    <h2 id="mine-h" style="font-size:18px">{e(fmt('your_card', last4='4821'))}</h2>
    <button class="btn navy" type="button" id="freeze-btn">{e(T['freeze'])}</button>
  </div>
  <p class="msg" id="frozen-note" style="color:#b42335" hidden>{e(T['frozen_note'])}</p>
  <div class="stats">
    <div class="stat"><span class="muted" style="font-size:12px">{e(T['balance'])}</span><b id="c-bal">…</b></div>
    <div class="stat"><span class="muted" style="font-size:12px">{e(T['available'])}</span><b id="c-avail">…</b></div>
    <div class="stat"><span class="muted" style="font-size:12px">{e(T['limit'])}</span><b id="c-limit">…</b></div>
  </div>
  <div class="scroll"><table><tbody id="c-spend"></tbody></table></div>
  <p class="muted" style="font-size:12px">{e(T['example_data'])}</p>
</section>"""
    js = r"""
document.querySelectorAll('.apr').forEach(a => a.textContent = num(+a.dataset.apr, 1) + '%');
let frozen = false;
function drawFrozen() {
  $('freeze-btn').textContent = frozen ? T.unfreeze : T.freeze;
  $('frozen-note').hidden = !frozen;
}
poll(async () => {
  const c = await api('card');
  $('c-bal').textContent = money(c.balance);
  $('c-avail').textContent = money(c.limit - c.balance);
  $('c-limit').textContent = money(c.limit);
  $('c-spend').replaceChildren(...c.spend.map(([d, who, amt]) => {
    const row = el('tr'); const name = el('td', '', who); name.style.textAlign = 'left';
    row.append(el('td', 'muted', new Date(d).toLocaleDateString(CFG.lang, {day: 'numeric', month: 'short'})), name, el('td', '', money(amt)));
    return row;
  }));
  frozen = c.frozen; drawFrozen();
}, 10000);
$('freeze-btn').addEventListener('click', async ev => {
  ev.target.disabled = true;
  try { frozen = (await post('card/freeze')).frozen; drawFrozen(); } catch (err) {}
  ev.target.disabled = false;
});
"""
    return page("cards", T["svc_cards"], body, js)


def mortgage_page():
    d = L["mortgage_defaults"]
    steps = [("details", True), ("credit", False), ("affordability", False), ("valuation", False), ("aip", False)]
    steps_html = "".join(f'<li id="step-{k}" class="{"done" if done else ""}"><span class="num">{i + 1}</span>'
                         f'<span>{e(T["step_" + k])}</span><span class="s">{e(T["s_done"] if done else T["s_next"])}</span></li>'
                         for i, (k, done) in enumerate(steps))
    sym = e(L['money']['symbol'])
    body = f"""
<section style="display:flex;flex-direction:column;gap:8px"><h1 style="font-size:40px">{e(T['svc_mortgage'])}</h1><p class="lede">{e(T['mort_lede'])}</p></section>
<div class="row">
  <section class="card" style="flex:2 1 560px;flex-direction:row;flex-wrap:wrap;gap:24px" aria-labelledby="calc-h">
    <form id="calc" style="flex:1 1 300px;min-width:0;display:flex;flex-direction:column;gap:14px" onsubmit="return false">
      <h2 id="calc-h" style="font-size:18px">{e(T['calc_h'])}</h2>
      <div class="field"><label for="m-price">{e(T['price'])} ({sym})</label><input id="m-price" type="number" min="0" step="1000" value="{d['price']}"></div>
      <div class="field"><label for="m-dep">{e(T['deposit'])} ({sym})</label><input id="m-dep" type="number" min="0" step="1000" value="{d['deposit']}"></div>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
        <div class="field"><label for="m-years">{e(T['term'])}</label><input id="m-years" type="number" min="1" max="40" value="{d['years']}"></div>
        <div class="field"><label for="m-rate">{e(T['rate'])}</label><input id="m-rate" type="number" min="0" max="20" step="0.05" value="{d['rate']}"></div>
      </div>
    </form>
    <div style="flex:1 1 260px;min-width:0;background:#0b1b3a;color:#fff;border-radius:16px;padding:20px;display:flex;flex-direction:column;gap:10px;justify-content:center">
      <span style="font-size:13px;color:#b9c6e4" id="m-borrow">…</span>
      <span class="disp" style="font-size:42px;font-weight:700;letter-spacing:-1px"><span id="m-monthly">…</span><span style="font-size:18px;font-weight:500;color:#b9c6e4"> {e(T['per_month'])}</span></span>
      <span style="font-size:13px;color:#b9c6e4" id="m-over">…</span>
      <button class="btn amber" type="button" id="aip-btn">{e(T['aip'])}</button>
    </div>
  </section>
  <section class="card" style="flex:1 1 320px" aria-labelledby="app-h"><h2 id="app-h" style="font-size:18px">{e(T['app_h'])}</h2>
    <ol class="steps">{steps_html}</ol>
    <p class="msg err" id="aip-msg" aria-live="polite"></p>
  </section>
</div>"""
    js = r"""
function calc() {
  const price = +$('m-price').value || 0, dep = Math.min(+$('m-dep').value || 0, price);
  const years = Math.max(1, +$('m-years').value || 1), rate = Math.max(0, +$('m-rate').value || 0);
  const P = price - dep, rm = rate / 100 / 12, n = years * 12;
  const m = rm === 0 ? P / n : P * rm / (1 - Math.pow(1 + rm, -n));
  $('m-borrow').textContent = tr('borrow', {amount: money(P, 0), ltv: price ? Math.round(P / price * 100) : 0});
  $('m-monthly').textContent = money(m, 0);
  $('m-over').textContent = tr('over', {years, rate: num(rate, 2)});
}
['m-price', 'm-dep', 'm-years', 'm-rate'].forEach(id => $(id).addEventListener('input', calc)); calc();
const ORDER = ['credit', 'affordability', 'valuation'];
function drawSteps(done, finished) {
  ORDER.concat(['aip']).forEach(k => {
    const li = $('step-' + k); const isDone = finished || done.includes(k);
    const isNow = !isDone && (k === 'aip' ? done.length === ORDER.length : ORDER[done.length] === k);
    li.className = isDone ? 'done' : isNow ? 'now' : '';
    li.querySelector('.s').textContent = isDone ? T.s_done : isNow ? T.s_checking : T.s_next;
  });
}
let timer = null;
$('aip-btn').addEventListener('click', async ev => {
  const btn = ev.target; btn.disabled = true; $('aip-msg').textContent = '';
  try {
    const a = await post('mortgage/apply');
    drawSteps([], false);
    clearInterval(timer);
    timer = setInterval(async () => {
      try {
        const s = await api('mortgage/app?id=' + encodeURIComponent(a.id));
        drawSteps(s.steps, s.status === 'done');
        if (s.status === 'done') { clearInterval(timer); btn.disabled = false; }
      } catch (err) {}
    }, 1000);
  } catch (err) { $('aip-msg').textContent = err.message; btn.disabled = false; }
});
"""
    return page("mortgage", T["svc_mortgage"], body, js)


def rates_page():
    sav = "".join(f'<div class="card" style="padding:16px;gap:4px"><span class="muted" style="font-size:13px">{e(n)}</span>'
                  f'<b class="disp" style="font-size:26px">{e(v)} {e(L["aer"])}</b></div>' for n, v in L["savings"])
    body = f"""
<section style="display:flex;flex-direction:column;gap:8px"><h1 style="font-size:40px">{e(T['svc_rates'])}</h1><p class="lede">{e(T['rates_lede'])}</p></section>
{rates_blocks(full=True)}
<section style="display:flex;flex-direction:column;gap:12px" aria-labelledby="sav-h"><h2 id="sav-h" style="font-size:18px">{e(T['savings_h'])}</h2>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">{sav}</div></section>"""
    return page("rates", T["svc_rates"], body, RATES_JS)


PAGES = {"": home, "cards": cards_page, "mortgage": mortgage_page, "rates": rates_page}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def _route(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/healthz":
            return "/healthz", {}
        for old in OLD_PREFIXES:  # the old per-country URLs, e.g. /dmobnk/de/cards
            if u.path == old or u.path.startswith(old + "/"):
                return ("redirect", BASE + u.path[len(old):].lstrip("/")), {}
        if u.path == "/dmobnk" or u.path.startswith(BASE):
            return u.path[len("/dmobnk"):].lstrip("/"), dict(urllib.parse.parse_qsl(u.query))
        return None, {}

    def do_GET(self):
        rest, q = self._route()
        if rest == "/healthz":
            return self._json(200, {"ok": True})
        if rest is None:
            return self._send(302, "text/plain", "", {"Location": BASE})
        if isinstance(rest, tuple):
            return self._send(301, "text/plain", "", {"Location": rest[1]})
        if rest == "admin" or rest.startswith("admin/"):
            return control.handle(self, "GET", rest, q)
        try:
            if rest in PAGES:
                if rest == "" and not urllib.parse.urlsplit(self.path).path.startswith(BASE):
                    return self._send(302, "text/plain", "", {"Location": BASE})
                return self._send(200, "text/html; charset=utf-8", PAGES[rest]())
            if rest == "api/rates":
                d = rates_local()
                return self._json(200, d) if d else self._json(503, {"error": T["error"]})
            if rest == "api/card":
                code, c = svc("cards-svc", "/card")
                d = rates_local()
                if code != 200 or not d:
                    return self._json(503, {"error": T["error"]})
                k = d["per_gbp_base"]
                return self._json(200, {"balance": c["balance_gbp"] * k, "limit": c["limit_gbp"] * k,
                                        "spend": [[s[0], s[1], s[2] * k] for s in c["spend"]], "frozen": c["frozen"]})
            if rest == "api/mortgage/app":
                app_id = q.get("id", "")
                if not app_id.isalnum() or len(app_id) > 12:
                    return self._json(400, {"error": "bad id"})
                code, a = svc("mortgage-svc", f"/app?id={app_id}")
                return self._json(code, {"id": a.get("id"), "status": a.get("status"), "steps": a.get("steps", [])})
        except Exception:
            return self._json(503, {"error": T["error"]})
        self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        rest, q = self._route()
        if isinstance(rest, str) and (rest == "admin" or rest.startswith("admin/")):
            return control.handle(self, "POST", rest, q)
        try:
            if rest == "api/card/freeze":
                code, res = svc("cards-svc", "/freeze", "POST")
                return self._json(code, res)
            if rest == "api/mortgage/apply":
                code, res = svc("mortgage-svc", "/apply", "POST")
                return self._json(code, {"id": res["id"]} if code == 200 else {"error": T["error"]})
        except Exception:
            return self._json(503, {"error": T["error"]})
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


ingress_host.start("dmobnk", "dmobnk", os.environ.get("SITE_HOST", ""), os.environ.get("INGRESS_CLASS", ""))
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
