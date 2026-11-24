"""Sends DMOBNK events (a kiosk withdrawal, a mortgage application...) to the
CRM, when there is one:
  - on the same cluster (crm=true there too): automatically, using the
    token from the CRM's own dmobnk-crm Secret (mounted, token only);
  - on another cluster: through a `dmobnk-crm-link` Secret here, with keys
    `url` and `token`, which the CRM's Connect page shows how to create.
Both are optional Secret volumes, so they take effect within a minute or so
of appearing, without a restart. With neither, events are dropped.

send() never blocks or raises: events go through a small queue, and a
background thread posts them to <url>/api/ingest."""
import json
import queue
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


def target():
    """(url, token) of the CRM to send to, or ("", "")."""
    url, token = _file(f"{LINK}/url"), _file(f"{LINK}/token")
    if url and token:
        return url.rstrip("/"), token
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
