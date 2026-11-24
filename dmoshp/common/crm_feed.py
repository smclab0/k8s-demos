"""Sends DMOBNK events (a kiosk withdrawal, a mortgage application...) to the
CRM, when there is one:
  - on the same cluster (crm=true there too): automatically, using the
    token from the CRM's own dmobnk-crm Secret (mounted, token only);
  - on another cluster: through a `dmobnk-crm-link` Secret here, with keys
    `url` and `token`, which the CRM's Connect page shows how to create.
Both are optional Secret volumes, so they take effect within a minute or so
of appearing, without a restart. With neither, events are dropped.
An app in another namespace (DMOSHP's shop and self-checkout) sets
CRM_NAMESPACE=dmobnk instead: it reads the token from that namespace's
dmobnk-crm Secret through the API (its ServiceAccount may get that one
Secret) and posts to the crm-web Service there.

send() never blocks or raises: events go through a small queue, and a
background thread posts them to <url>/api/ingest."""
import base64
import json
import os
import queue
import ssl
import threading
import time
import urllib.request

LINK = "/etc/dmobnk-crm"
LOCAL = "/etc/dmobnk-crm-local"
LOCAL_URL = "http://crm-web"   # the CRM's Service, when it's on this cluster
SOURCE = {"app": "", "where": ""}   # set by the app: e.g. {"app": "kiosk", "where": "London Bridge branch"}
_q = queue.Queue(maxsize=500)
_started = threading.Lock()
_thread = [None]
_last_err = [0.0]


def _file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


CRM_NS = os.environ.get("CRM_NAMESPACE", "")
_remote = {"t": 0.0, "token": ""}


def _remote_token():
    """The CRM's ingest token from CRM_NS's dmobnk-crm Secret, checked once a minute."""
    if time.time() - _remote["t"] < 60:
        return _remote["token"]
    _remote["t"], token = time.time(), ""
    try:
        sa = "/var/run/secrets/kubernetes.io/serviceaccount"
        req = urllib.request.Request(f"https://kubernetes.default.svc/api/v1/namespaces/{CRM_NS}/secrets/dmobnk-crm",
                                     headers={"Authorization": f"Bearer {_file(sa + '/token')}"})
        ctx = ssl.create_default_context(cafile=f"{sa}/ca.crt")
        data = json.loads(urllib.request.urlopen(req, timeout=4, context=ctx).read()).get("data") or {}
        token = base64.b64decode(data.get("token", "")).decode()
    except Exception:
        pass       # no CRM on this cluster (404) or not allowed: events are dropped
    _remote["token"] = token
    return token


def target():
    """(url, token) of the CRM to send to, or ("", "")."""
    url, token = _file(f"{LINK}/url"), _file(f"{LINK}/token")
    if url and token:
        return url.rstrip("/"), token
    if CRM_NS:
        token = _remote_token()
        return (f"http://crm-web.{CRM_NS}.svc", token) if token else ("", "")
    token = _file(f"{LOCAL}/token")
    return (LOCAL_URL, token) if token else ("", "")


def linked():
    return bool(target()[0])


def send(kind, text, customer=None, **data):
    """customer: {"id": ..., "name": ...} or None."""
    if not linked():
        return
    ev = {"type": kind, "text": text, "customer": customer, "data": data,
          "t": int(time.time()), "source": dict(SOURCE)}
    try:
        _q.put_nowait(ev)
    except queue.Full:
        return
    with _started:
        if _thread[0] is None:
            _thread[0] = threading.Thread(target=_worker, daemon=True)
            _thread[0].start()


def _worker():
    while True:
        ev = _q.get()
        url, token = target()
        if not url:
            continue
        try:
            req = urllib.request.Request(url + "/api/ingest", data=json.dumps(ev).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {token}"})
            urllib.request.urlopen(req, timeout=4).read()
        except Exception as err:
            if time.time() - _last_err[0] > 60:
                _last_err[0] = time.time()
                print(f"crm feed: couldn't reach {url}: {err}", flush=True)
