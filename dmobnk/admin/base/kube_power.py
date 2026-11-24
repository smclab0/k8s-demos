"""Serves Kube Power (control.py) as its own small Deployment, so only
clusters labelled admin=true run it, and the public web
pods never hold its permissions. Its Ingress shares the app's host and takes
the ADMIN_BASE path (/dmobnk/admin on the bank, /admin on the kiosk); it
points that Ingress at the cluster the same way the apps do
(../../common/ingress_host.py)."""
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/dmobnk")
import control  # noqa: E402
import ingress_host  # noqa: E402  (mounted from the dmobnk-ingress-host ConfigMap)

BASE = control.BASE


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def _route(self, method):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/healthz":
            # Ready only once the admin password is loaded, so sign-in works.
            return self._json(200 if control.PASSWORD else 503, {"ok": bool(control.PASSWORD)})
        if u.path == BASE or u.path.startswith(BASE + "/"):
            rest = "admin" + u.path[len(BASE):]
            return control.handle(self, method, rest, dict(urllib.parse.parse_qsl(u.query)))
        self._send(404, "text/plain; charset=utf-8", "not found")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def log_message(self, *args):
        pass


threading.Thread(target=control.init, daemon=True).start()
ingress_host.start("kube-power", os.environ.get("HOST_PREFIX", "dmobnk"),
                   os.environ.get("SITE_HOST", ""), os.environ.get("INGRESS_CLASS", ""),
                   follow=os.environ.get("FOLLOW_INGRESS", ""))
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
