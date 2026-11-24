"""DMOCRP: the group homepage for the DemoCorp demos. It introduces the two
companies, DMOBNK (bank) and DMOSHP (retail), plays the promo video, and
links to whichever of their apps run on this cluster (it reads their
Ingresses' hosts, read only).

The video isn't in the bundle (a ConfigMap can't hold it): the init
container downloads it from the repo's media/ folder into /media when the
pod starts. Without internet access the page shows the poster and a link.

It creates and points its own Ingress at the cluster it runs on, like the
other apps: dmocrp.<address>.sslip.io (../common/ingress_host.py)."""
import html
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/dmobnk")
import ingress_host  # noqa: E402  (mounted from the dmocrp-lib ConfigMap)

MEDIA = "/media"
VIDEO = "democorp-promo.mp4"
POSTER = "democorp-promo-poster.jpg"
VIDEO_PAGE = os.environ.get("VIDEO_PAGE", "https://github.com/smclab0/k8s-demos/blob/main/media/democorp-promo.mp4")
CREDIT = "Created By Steve McBride"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"

# The group's apps: (namespace, Ingress, name, what it is, path, admin?)
APPS = [
    ("dmobnk", "dmobnk", "Online bank", "Accounts, cards, mortgages and rates on six microservices.", "/dmobnk/", False),
    ("dmobnk", "branch-web", "Branch kiosk", "Self-service balances, cash, transfers and appointments.", "/", False),
    ("dmobnk", "crm-web", "CRM", "Customer 360, sales pipeline and tasks, live from every channel.", "/", False),
    ("dmobnk", "kube-power", "Kube Power", "The bank's control room: break things, watch them heal.", "", True),
    ("dmoshp", "shop-web", "Online shop", "Groceries, home and tech with live stock.", "/", False),
    ("dmoshp", "sok-web", "DMOSOK self-checkout", "Scan, tap a card, take the receipt.", "/", False),
    ("dmoshp", "kube-power", "Kube Power", "The shop's control room.", "", True),
]


def e(s):
    return html.escape(str(s), quote=True)


_cache = {"t": 0.0, "hosts": {}}


def live_hosts():
    """{(ns, ingress): "http://host/path"} for the group's Ingresses on this
    cluster, refreshed every 20 s."""
    if time.time() - _cache["t"] < 20:
        return _cache["hosts"]
    hosts = {}
    try:
        token = open(f"{SA}/token").read().strip()
        ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
        for ns in ("dmobnk", "dmoshp"):
            req = urllib.request.Request(f"https://kubernetes.default.svc/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
                                         headers={"Authorization": f"Bearer {token}"})
            try:
                items = json.loads(urllib.request.urlopen(req, timeout=3, context=ctx).read()).get("items", [])
            except Exception:
                continue
            for ing in items:
                rule = (ing.get("spec", {}).get("rules") or [{}])[0]
                host = rule.get("host", "")
                path = ((rule.get("http") or {}).get("paths") or [{}])[0].get("path", "/")
                if host and "node-ip" not in host:
                    scheme = "https" if ing["spec"].get("tls") else "http"
                    hosts[(ns, ing["metadata"]["name"])] = f"{scheme}://{host}{path}"
    except Exception as err:
        print(f"couldn't read the apps' Ingresses: {err}", flush=True)
    _cache.update(t=time.time(), hosts=hosts)
    return hosts


LOGO = ('<svg viewBox="0 0 32 32" width="36" height="36" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#14213d"/>'
        '<rect x="7" y="7" width="8" height="8" rx="2" fill="#7fa2ff"/><rect x="17" y="7" width="8" height="8" rx="2" fill="#5fd39b"/>'
        '<rect x="7" y="17" width="8" height="8" rx="2" fill="#5fd39b"/><rect x="17" y="17" width="8" height="8" rx="2" fill="#7fa2ff"/></svg>')

CSS = """
:root{--ink:#14213d;--muted:#4a5568;--paper:#f6f7fb;--line:#dfe5f1;--blue:#2453ff;--green:#0f7a4f}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
h1,h2,h3{font-family:"Space Grotesk",Inter,sans-serif;margin:0;letter-spacing:-.01em}
a{color:var(--blue)}
.wrap{max-width:1180px;margin:0 auto;padding-left:16px;padding-right:16px}
.demo{background:#fdecd0;color:#5c3a00;font-size:13px}
.demo .wrap{display:flex;gap:10px;align-items:center;padding-block:7px}
.demo b{background:#8a4b00;color:#fff;border-radius:6px;padding:1px 8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;flex:none}
header{background:#fff;border-bottom:1px solid var(--line)}
header .wrap{display:flex;align-items:center;gap:12px;padding-block:14px}
.brand b{font-family:"Space Grotesk",sans-serif;font-size:22px;letter-spacing:.04em;display:block;line-height:1.1}
.brand small{color:var(--muted);font-size:12px}
.hero{background:linear-gradient(135deg,#0b1b3a 0%,#14213d 55%,#0b3d2c 100%);color:#f6f7fb}
.hero .wrap{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.35fr);gap:40px;align-items:center;padding-block:56px}
.hero h1{font-size:48px;line-height:1.08}
.hero p{color:#c9d4ee;font-size:18px;margin:14px 0 0}
.eb{font-size:13px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#9db4ff;margin-bottom:10px}
.player{border-radius:18px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.15);background:#000;aspect-ratio:16/9}
.player video,.player img{width:100%;height:100%;display:block;object-fit:cover}
.novideo{font-size:14px;color:#c9d4ee;margin-top:10px}
.novideo a{color:#9db4ff}
@media (max-width:860px){.hero .wrap{grid-template-columns:1fr;padding-block:36px}.hero h1{font-size:36px}}
main .wrap{padding-block:40px;display:flex;flex-direction:column;gap:28px}
.cos{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}
@media (max-width:860px){.cos{grid-template-columns:1fr}}
.co{background:#fff;border:1px solid var(--line);border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:14px}
.co .k{font-size:13px;font-weight:700;letter-spacing:.1em;text-transform:uppercase}
.co.bnk .k{color:var(--blue)}.co.shp .k{color:var(--green)}
.co p{margin:0;color:var(--muted)}
.app{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;border-top:1px solid var(--line);padding-top:12px}
.app b{display:block}
.app span{color:var(--muted);font-size:14px}
.go{flex:none;font-weight:700;text-decoration:none;border-radius:10px;padding:8px 14px;font-size:14px;white-space:nowrap}
.bnk .go{background:#e7edff;color:#1a3bc2}.shp .go{background:#e3f6ec;color:#0b6640}
.off{flex:none;font-size:13px;color:#6b7690;padding:8px 0;white-space:nowrap}
.note{font-size:14px;color:var(--muted)}
footer{text-align:center;font-size:13px;color:var(--muted);padding:18px 16px 26px}
"""


def page():
    hosts = live_hosts()
    have_video = os.path.exists(f"{MEDIA}/{VIDEO}")
    have_poster = os.path.exists(f"{MEDIA}/{POSTER}")
    poster = f' poster="/media/{POSTER}"' if have_poster else ""
    if have_video:
        player = (f'<div class="player"><video controls playsinline preload="metadata"{poster} aria-label="DemoCorp promo video">'
                  f'<source src="/media/{VIDEO}" type="video/mp4"></video></div>')
    else:
        img = f'<img src="/media/{POSTER}" alt="DemoCorp promo video poster">' if have_poster else ""
        player = (f'<div class="player">{img}</div><p class="novideo">This cluster couldn\'t download the video. '
                  f'<a href="{e(VIDEO_PAGE)}">Watch it on GitHub</a>.</p>')

    def company(cls, key, title, blurb, ns):
        rows = ""
        for a_ns, ing, name, desc, _path, _admin in APPS:
            if a_ns != ns:
                continue
            url = hosts.get((a_ns, ing))
            act = (f'<a class="go" href="{e(url)}">Open</a>' if url else '<span class="off">Not on this cluster</span>')
            rows += f'<div class="app"><div><b>{e(name)}</b><span>{e(desc)}</span></div>{act}</div>'
        return f'<section class="co {cls}"><span class="k">{e(key)}</span><h2>{e(title)}</h2><p>{e(blurb)}</p>{rows}</section>'

    body = f"""<div class="demo" role="note"><div class="wrap"><b>Demo</b><span>DMOCRP and its companies are fictional. This is a demonstration of apps placed and run by Rancher and Kubernetes.</span></div></div>
<header><div class="wrap"><span>{LOGO}</span><span class="brand"><b>DMOCRP</b><small>The DemoCorp group</small></span></div></header>
<section class="hero"><div class="wrap">
  <div><div class="eb">Rancher · Fleet · Kubernetes</div><h1>One group. Banking and retail, on any cluster.</h1>
  <p>Label a cluster in Rancher and Fleet places the apps. Freeze a card at the bank and the shop's till declines it. Kill a pod and watch Kubernetes heal it.</p></div>
  <div>{player}</div>
</div></section>
<main><div class="wrap">
  <div class="cos">
    {company("bnk", "DMOBNK · Demo Bank Ldn", "Banking, in branch and online", "The group's bank: an online bank, a branch kiosk and a CRM that sees every channel.", "dmobnk")}
    {company("shp", "DMOSHP · DMOCRP retail", "Shopping, online and in store", "The group's retail arm: one live stock count, and every card payment authorised by DMOBNK.", "dmoshp")}
  </div>
  <p class="note">Links appear for the apps running on this cluster. Demo sign-ins use the password <b>dmopwd1</b>.</p>
</div></main>
<footer>{e(CREDIT)}</footer>"""
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>DMOCRP · DemoCorp</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;600;700&display=swap">
<style>{CSS}</style></head><body>{body}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _media(self, name):
        """Serves /media files with Range support (browsers seek video with it)."""
        path = f"{MEDIA}/{name}"
        if name not in (VIDEO, POSTER) or not os.path.exists(path):
            return self._send(404, "text/plain", "not found")
        ctype = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
        size = os.path.getsize(path)
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        code = 200
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
                code = 206
            except ValueError:
                return self._send(416, "text/plain", "", {"Content-Range": f"bytes */{size}"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/healthz":
            return self._send(200, "application/json", '{"ok": true}')
        if u.path == "/":
            return self._send(200, "text/html; charset=utf-8", page(), {"Cache-Control": "no-store"})
        if u.path.startswith("/media/"):
            return self._media(u.path[len("/media/"):])
        self._send(404, "text/plain", "not found")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


ingress_host.start("dmocrp-web", "dmocrp", os.environ.get("SITE_HOST", ""), os.environ.get("INGRESS_CLASS", ""),
                   path="/", service="dmocrp-web", owner="dmocrp-web")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
