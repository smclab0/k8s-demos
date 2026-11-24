"""Sends DMOBNK events (a kiosk withdrawal, a mortgage application...) to the
CRM, when this cluster is linked to one. The CRM runs on its own cluster
(crm=true); linking is a `dmobnk-crm-link` Secret with keys `url` and
`token`, which the CRM's Connect page shows how to create. The Secret is
mounted as an optional volume, so creating or changing it takes effect
within a minute or so, without a restart. Unlinked, events are dropped.

send() never blocks or raises: events go through a small queue, and a
background thread posts them to <url>/api/ingest."""
import json
import queue
import threading
import time
import urllib.request

LINK = "/etc/dmobnk-crm"
SOURCE = {"app": "", "where": ""}   # set by the app: e.g. {"app": "kiosk", "where": "London Bridge branch"}
_q = queue.Queue(maxsize=500)
_started = threading.Lock()
_thread = [None]
_last_err = [0.0]


def _read(key):
    try:
        with open(f"{LINK}/{key}") as f:
            return f.read().strip()
    except OSError:
        return ""


def linked():
    return bool(_read("url") and _read("token"))


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
        url, token = _read("url").rstrip("/"), _read("token")
        if not (url and token):
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
