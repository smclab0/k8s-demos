"""Kube Power: DMOBNK's admin-only control room. It runs as its own
kube-power Deployment (kube_power.py serves it), deployed only to clusters
labelled admin=true: at /dmobnk/admin on the online bank, at /admin on a
kiosk or CRM cluster; see ../../fleet.yaml.

It shows the dmobnk namespace live (pods by node, deployments, the
autoscaler, events) and lets a signed-in admin break things to show
Kubernetes fixing them: scale a deployment, kill a pod, do a rolling restart
and, on the bank, send a traffic surge at rates-svc.

Sign-in uses the `password` key of the `dmobnk-admin` Secret. If there's
no such Secret, the first start creates one with a random password (read it
with kubectl, see the README). Everything it touches is limited to the
dmobnk namespace by the kube-power Role and to ADMIN_APPS below."""
import base64
import hashlib
import hmac
import html
import json
import os
import random
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

NS = os.environ.get("POD_NAMESPACE", "dmobnk")
K8S = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
ENABLED = os.path.exists(f"{SA}/token")
PASSWORD = ""                    # set by init() from the dmobnk-admin Secret
_KEY = b""
SECRET = "dmobnk-admin"
POD = os.environ.get("POD_NAME", "")
NODE = os.environ.get("NODE_NAME", "").split(".")[0]
COOKIE = "dmobnk_admin"
SESSION_S = 8 * 3600


def _env_list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# What this control room drives, set per app by ../bank and ../branch.
BASE = os.environ.get("ADMIN_BASE", "/dmobnk/admin").rstrip("/")
APP_URL = os.environ.get("APP_URL", "/dmobnk/")
APP_NAME = os.environ.get("APP_NAME", "the bank")
# The deployments the controls may touch. Redis is never in the list: it
# holds the app's state and runs as a single pod.
APPS = _env_list("ADMIN_APPS", "web,accounts-svc,cards-svc,loans-svc,mortgage-svc,rates-svc,crossborder-svc")
HPA_APPS = set(_env_list("ADMIN_HPA", "rates-svc"))   # sized by an autoscaler, not by hand
SURGE = os.environ.get("ADMIN_SURGE", "rates-svc").strip()   # "" = no traffic surge
CRON = os.environ.get("ADMIN_CRON", "rates-refresh").strip()  # "" = no CronJob tile
SCALE_MIN, SCALE_MAX = 1, 6

_ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt") if ENABLED else None
_fails = {}      # client ip -> [timestamps of failed sign-ins]
_last_kill = [0.0]


# ---------------------------------------------------------------- k8s
class K8sError(Exception):
    def __init__(self, msg, code=0):
        super().__init__(msg)
        self.code = code


def k8s(method, path, body=None, ctype="application/json"):
    with open(f"{SA}/token") as f:
        tok = f.read().strip()
    req = urllib.request.Request(K8S + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": ctype,
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5, context=_ctx) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            msg = json.load(err).get("message", "")
        except ValueError:
            msg = ""
        raise K8sError(f"Kubernetes said {err.code}{': ' + msg if msg else ''}", err.code)


def init():
    """Load the sign-in password from the dmobnk-admin Secret, creating the
    Secret with a random password if there isn't one. Retries until the API
    answers, so the pod only turns ready once sign-in works."""
    global PASSWORD, _KEY
    while ENABLED:
        try:
            try:
                sec = k8s("GET", f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
            except K8sError as err:
                if err.code != 404:
                    raise
                pw = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(20))
                try:
                    k8s("POST", f"/api/v1/namespaces/{NS}/secrets", {
                        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": SECRET, "labels": {"app.kubernetes.io/part-of": "dmobnk"}},
                        "stringData": {"password": pw}})
                    print(f"created the {SECRET} Secret with a new admin password", flush=True)
                except K8sError as err2:
                    if err2.code != 409:  # 409: another replica made it first
                        raise
                sec = k8s("GET", f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
            PASSWORD = base64.b64decode(sec["data"]["password"]).decode().strip()
            # Every replica derives the same key from the shared password, so a
            # session works whichever pod serves the next request.
            _KEY = hashlib.sha256(b"dmobnk-admin-session:" + PASSWORD.encode()).digest()
            return
        except Exception as err:
            print(f"couldn't load the admin password: {err}", flush=True)
            time.sleep(5)


_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _ready(p):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))


def list_pods():
    out = []
    for p in k8s("GET", f"/api/v1/namespaces/{NS}/pods")["items"]:
        meta, status = p["metadata"], p["status"]
        if meta.get("deletionTimestamp"):
            state = "terminating"
        elif status.get("phase") in ("Succeeded", "Failed"):
            continue
        elif _ready(p):
            state = "running"
        else:
            state = "starting"
        labels = meta.get("labels", {})
        app = labels.get("app") or ("job" if labels.get("job-name") else "other")
        out.append({"name": meta["name"], "app": app, "node": (p["spec"].get("nodeName") or "").split(".")[0],
                    "state": state, "created": meta.get("creationTimestamp", ""),
                    "restarts": sum(c.get("restartCount", 0) for c in status.get("containerStatuses") or [])})
    return sorted(out, key=lambda x: (x["app"], x["created"]))


def list_deployments():
    out = []
    for d in k8s("GET", f"/apis/apps/v1/namespaces/{NS}/deployments")["items"]:
        spec, st, meta = d["spec"], d.get("status", {}), d["metadata"]
        env = {v["name"]: v.get("value") for v in spec["template"]["spec"]["containers"][0].get("env", [])}
        desired = spec.get("replicas", 0) or 0
        updated = st.get("updatedReplicas", 0) or 0
        rolling = (st.get("observedGeneration", 0) < meta.get("generation", 0) or updated < desired
                   or (st.get("replicas", 0) or 0) > desired)
        out.append({"name": meta["name"], "desired": desired, "ready": st.get("readyReplicas", 0) or 0,
                    "updated": updated, "version": env.get("VERSION", ""), "rolling": rolling,
                    "controllable": meta["name"] in APPS, "hpa": meta["name"] in HPA_APPS})
    return sorted(out, key=lambda x: (x["name"] not in APPS, APPS.index(x["name"]) if x["name"] in APPS else 0, x["name"]))


def hpa():
    if not HPA_APPS:
        return None
    try:
        h = k8s("GET", f"/apis/autoscaling/v2/namespaces/{NS}/horizontalpodautoscalers/{sorted(HPA_APPS)[0]}")
    except K8sError:
        return None
    cpu = None
    for m in h["status"].get("currentMetrics") or []:
        if m.get("type") == "Resource":
            cpu = m["resource"]["current"].get("averageUtilization")
    return {"min": h["spec"].get("minReplicas", 1), "max": h["spec"]["maxReplicas"],
            "current": h["status"].get("currentReplicas", 0), "desired": h["status"].get("desiredReplicas", 0),
            "cpu": cpu, "target": h["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"]}


def surge_jobs():
    if not SURGE:
        return []
    items = k8s("GET", f"/apis/batch/v1/namespaces/{NS}/jobs?labelSelector=app%3Drates-rush")["items"]
    return [j["metadata"]["name"] for j in items if (j.get("status", {}).get("active") or 0) > 0]


def _ts(ev):
    return (ev.get("eventTime") or ev.get("lastTimestamp") or ev.get("firstTimestamp")
            or ev["metadata"].get("creationTimestamp") or "")


def list_events():
    items = k8s("GET", f"/api/v1/namespaces/{NS}/events?limit=250")["items"]
    items.sort(key=_ts, reverse=True)
    return [{"t": _ts(ev), "type": ev.get("type", "Normal"), "reason": ev.get("reason", ""),
             "obj": f"{ev['involvedObject'].get('kind', '').lower()}/{ev['involvedObject'].get('name', '')}",
             "msg": ev.get("message", ""), "count": ev.get("count") or 1} for ev in items[:25]]


def cluster_info():
    try:
        return k8s("GET", "/version").get("gitVersion", "")
    except Exception:
        return ""


def state():
    pods = cached("pods", 1.5, list_pods)
    def cron_last():
        if not CRON:
            return None
        try:
            return k8s("GET", f"/apis/batch/v1/namespaces/{NS}/cronjobs/{CRON}").get("status", {}).get("lastSuccessfulTime")
        except K8sError:
            return None
    return {"version": cached("version", 3600, cluster_info), "served": {"pod": POD, "node": NODE},
            "pods": pods, "deployments": cached("deps", 1.5, list_deployments), "hpa": cached("hpa", 3, hpa),
            "cron_last": cached("cron", 10, cron_last),
            "surge": cached("surge", 3, surge_jobs), "events": cached("events", 3, list_events)}


def _forget(*keys):
    for k in keys:
        _cache.pop(k, None)


# ---------------------------------------------------------------- actions
def scale(app, replicas):
    if app not in APPS:
        return 400, {"error": "That deployment can't be scaled from here."}
    if app in HPA_APPS:
        return 400, {"error": f"{app} is sized by its autoscaler. Use the traffic surge instead."}
    if not isinstance(replicas, int) or not SCALE_MIN <= replicas <= SCALE_MAX:
        return 400, {"error": f"Pick between {SCALE_MIN} and {SCALE_MAX} replicas."}
    k8s("PATCH", f"/apis/apps/v1/namespaces/{NS}/deployments/{app}/scale",
        {"spec": {"replicas": replicas}}, "application/merge-patch+json")
    _forget("deps", "pods")
    return 200, {"message": f"Scaling {app} to {replicas} pods."}


def kill(app):
    if app and app not in APPS:
        return 400, {"error": "Pods of that deployment can't be killed from here."}
    if time.time() - _last_kill[0] < 2:
        return 429, {"error": "One at a time: give it a couple of seconds."}
    pods = list_pods()
    running = {}
    for p in pods:
        if p["app"] in APPS and p["state"] == "running":
            running.setdefault(p["app"], []).append(p)
    # Only kill where another ready pod keeps the service up.
    pool = [p for a, ps in running.items() if len(ps) >= 2 and (not app or a == app) for p in ps]
    if not pool:
        return 409, {"error": "Every candidate is down to its last ready pod. Wait for the replacement."}
    victim = random.choice(pool)
    _last_kill[0] = time.time()
    k8s("DELETE", f"/api/v1/namespaces/{NS}/pods/{victim['name']}")
    _forget("pods", "deps", "events")
    return 200, {"message": f"Killed {victim['name']} on {victim['node']}. Watch its replacement start.",
                 "pod": victim["name"]}


def restart(app):
    if app not in APPS:
        return 400, {"error": "That deployment can't be restarted from here."}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    k8s("PATCH", f"/apis/apps/v1/namespaces/{NS}/deployments/{app}",
        {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}},
        "application/merge-patch+json")
    _forget("deps", "pods")
    return 200, {"message": f"Rolling restart of {app}: new pods come up before old ones go."}


def surge():
    if not SURGE:
        return 404, {"error": "No traffic surge here."}
    if surge_jobs():
        return 409, {"error": "A surge is already running."}
    name = f"rates-rush-{secrets.token_hex(3)}"
    loop = ("end=$(( $(date +%s) + 90 )); while [ $(date +%s) -lt $end ]; do "
            f"curl -s -o /dev/null -m 5 'http://{SURGE}:8080/quote?burn=1'; done")
    k8s("POST", f"/apis/batch/v1/namespaces/{NS}/jobs", {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "labels": {"app": "rates-rush", "app.kubernetes.io/part-of": "dmobnk"}},
        "spec": {"parallelism": 4, "completions": 4, "backoffLimit": 0, "activeDeadlineSeconds": 150,
                 "ttlSecondsAfterFinished": 300,
                 "template": {"metadata": {"labels": {"app": "rates-rush"}},
                              "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                       "containers": [{"name": "rush", "image": "curlimages/curl:8.10.1",
                                                       "command": ["sh", "-c", loop],
                                                       "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                                     "limits": {"memory": "32Mi"}}}]}}},
    })
    _forget("surge", "pods")
    return 200, {"message": f"Surge started ({name}): 4 pods hit {SURGE} for 90 s. Watch the autoscaler."}


# ---------------------------------------------------------------- sessions
def _sign(payload):
    return hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()


def new_session():
    payload = f"{int(time.time()) + SESSION_S}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def valid_session(token):
    try:
        exp, nonce, sig = token.split(".")
        return hmac.compare_digest(sig, _sign(f"{exp}.{nonce}")) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


def _cookie(h):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def _client_ip(h):
    return h.headers.get("X-Real-IP") or h.client_address[0]


def _locked_out(ip):
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 300]
    _fails[ip] = recent
    return len(recent) >= 5


def _set_cookie(h, value, max_age):
    # Secure only over HTTPS: on a cluster served over plain HTTP (an sslip.io
    # host) a Secure cookie would never be sent back, so sign-in would loop.
    https = (h.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip() == "https"
    return f"{COOKIE}={value}; Path={BASE}; Max-Age={max_age}; HttpOnly;{' Secure;' if https else ''} SameSite=Strict"


# ---------------------------------------------------------------- HTTP
def handle(h, method, rest, q):
    """rest is the path after /dmobnk/, e.g. "admin" or "admin/api/state"."""
    sub = rest[len("admin"):].strip("/")
    signed_in = ENABLED and bool(PASSWORD) and valid_session(_cookie(h))
    if method == "GET":
        if sub == "":
            if not signed_in:
                return h._send(200, "text/html; charset=utf-8", login_page(error=q.get("e", "")))
            return h._send(200, "text/html; charset=utf-8", control_page())
        if sub == "api/state":
            if not signed_in:
                return h._json(401, {"error": "Signed out."})
            try:
                return h._json(200, state())
            except Exception as err:
                return h._json(503, {"error": str(err) or "Can't reach the cluster."})
        return h._send(404, "text/plain; charset=utf-8", "not found")

    length = int(h.headers.get("Content-Length") or 0)
    raw = h.rfile.read(min(length, 4096)) if length else b""
    if sub == "login":
        if not (ENABLED and PASSWORD):
            return h._send(303, "text/plain", "", {"Location": BASE})
        ip = _client_ip(h)
        if _locked_out(ip):
            return h._send(303, "text/plain", "", {"Location": BASE + "?e=locked"})
        given = urllib.parse.parse_qs(raw.decode("utf-8", "replace")).get("password", [""])[0]
        if hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest()):
            _fails.pop(ip, None)
            return h._send(303, "text/plain", "", {"Location": BASE, "Set-Cookie": _set_cookie(h, new_session(), SESSION_S)})
        _fails.setdefault(ip, []).append(time.time())
        time.sleep(1)
        return h._send(303, "text/plain", "", {"Location": BASE + "?e=wrong"})
    if sub == "logout":
        return h._send(303, "text/plain", "", {"Location": BASE, "Set-Cookie": _set_cookie(h, "", 0)})

    # The actions: signed in, and sent by our own page's fetch (custom header).
    if not signed_in:
        return h._json(401, {"error": "Signed out. Reload the page to sign in again."})
    if h.headers.get("X-Kube-Power") != "1":
        return h._json(403, {"error": "forbidden"})
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return h._json(400, {"error": "bad request"})
    app = str(body.get("app") or "")
    try:
        if sub == "api/scale":
            return h._json(*scale(app, body.get("replicas")))
        if sub == "api/kill":
            return h._json(*kill(app))
        if sub == "api/restart":
            return h._json(*restart(app))
        if sub == "api/surge":
            return h._json(*surge())
    except K8sError as err:
        return h._json(502, {"error": str(err)})
    except Exception:
        return h._json(503, {"error": "Can't reach the cluster."})
    return h._json(404, {"error": "not found"})


# ---------------------------------------------------------------- pages
def e(s):
    return html.escape(str(s), quote=True)


CSS = """
*{box-sizing:border-box}
body{margin:0;background:#eef2fa;color:#0b1b3a;font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}
a{color:#1f4fe0}a:hover{color:#163aa8}
button,select,input{font:inherit}
button{cursor:pointer}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #7fa2ff;outline-offset:2px}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace}
.disp{font-family:"Space Grotesk",Inter,sans-serif}
.wrap{max-width:1360px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
header{background:#0b1b3a;color:#fff}
header .wrap{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding-block:14px}
.brand{display:flex;align-items:center;gap:12px;color:#fff;text-decoration:none}
.brand b{font:700 21px/1.1 "Space Grotesk",sans-serif;letter-spacing:-.4px;display:block}
.brand small{font-size:12px;color:#b9c6e4}
.pills{display:flex;flex-wrap:wrap;gap:8px;font-size:13px;align-items:center}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:99px;background:#18305e;min-width:0;overflow-wrap:anywhere}
.dot{width:8px;height:8px;border-radius:50%;background:#4ade80;flex-shrink:0}
.pill button{background:none;border:0;color:#fff;font-weight:700;padding:0}
main{padding-block:22px 40px;display:flex;flex-direction:column;gap:20px}
h1,h2,h3{margin:0}
h2{font:700 22px/1.2 "Space Grotesk",sans-serif;letter-spacing:-.4px}
h3{font-size:16px}
p{margin:0}
.muted{color:#4a5a7d}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
.tile{background:#fff;border-radius:16px;padding:16px 20px;display:flex;flex-direction:column;gap:4px;min-width:0}
.tile .k{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#4a5a7d}
.tile .v{font:700 32px/1.15 "Space Grotesk",sans-serif;letter-spacing:-1px}
.tile .s{font-size:13px;color:#4a5a7d}
.bar{height:8px;border-radius:99px;background:#dfe6f5;overflow:hidden}
.bar span{display:block;height:100%;background:#2453ff;border-radius:99px;transition:width .4s}
.cols{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.panel{background:#fff;border-radius:20px;padding:20px;display:flex;flex-direction:column;gap:14px;min-width:0}
.map{flex:999 1 640px}
.headrow{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px 14px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:#4a5a7d}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:12px;height:12px;border-radius:4px;display:inline-block;box-sizing:border-box}
.nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.node{border:1px solid #d6deee;border-radius:14px;padding:12px;background:#f8faff;display:flex;flex-direction:column;gap:10px;min-width:0}
.node .nh{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.node .nh b{font-family:"JetBrains Mono",monospace;font-size:13px;overflow-wrap:anywhere}
.podgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(64px,1fr));gap:6px;align-content:start;min-height:46px}
.pod{height:42px;border-radius:8px;font:500 10px/1.2 "JetBrains Mono",monospace;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:2px;overflow:hidden;transition:opacity .3s}
.pod b{font-weight:700;font-size:10px}
.pod.starting{background:#fff!important;border:2px dashed currentColor;color:#1f4fe0}
.pod.terminating{background:#c8d0e0!important;color:#4a5a7d!important;text-decoration:line-through}
.pod.served{box-shadow:0 0 0 2px #fff,0 0 0 4px #4ade80}
.deps{width:100%;border-collapse:collapse;font-size:14px;min-width:520px}
.deps th{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#4a5a7d;padding:8px 10px;border-bottom:1px solid #d6deee;text-align:left}
.deps td{padding:9px 10px;border-bottom:1px solid #edf1f8;vertical-align:middle}
.deps td .bar{width:120px}
.scroll{overflow-x:auto}
.badge{font-size:11px;font-weight:700;letter-spacing:.04em;padding:3px 9px;border-radius:99px;white-space:nowrap}
.b-ok{background:#e3f4ea;color:#0d6b3c}.b-warn{background:#fdecd3;color:#8a4b00}.b-info{background:#e6ecff;color:#1a3fbf}.b-bad{background:#fde4e7;color:#9b1c2c}
aside{flex:1 1 340px;min-width:0;display:flex;flex-direction:column;gap:14px}
.act{background:#fff;border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:10px}
.act .ah{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.act .ah span{font-size:12px;color:#4a5a7d}
.act p{font-size:13px;color:#4a5a7d}
.ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.ctl select{flex:1 1 150px;min-width:0;height:44px;border:1px solid #c9d3e8;border-radius:12px;padding:0 10px;background:#fff;color:#0b1b3a;font:500 14px "JetBrains Mono",monospace}
.step{width:44px;height:44px;border:1px solid #c9d3e8;border-radius:12px;background:#f4f7fd;font-size:20px;color:#0b1b3a}
.num{width:64px;height:44px;text-align:center;border:1px solid #c9d3e8;border-radius:12px;font:700 18px "Space Grotesk",sans-serif;color:#0b1b3a}
.btn{min-height:44px;padding:0 16px;border:0;border-radius:12px;font-weight:700;color:#fff;background:#2453ff}
.btn.red{background:#c42b3c}.btn.navy{background:#0b1b3a}.btn.amber{background:#e8890c;color:#1d1200}
.btn.wide{flex:1}
.btn:disabled{opacity:.55;cursor:progress}
.note{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:12px;background:#eaf0ff;font-size:14px}
.note.err{background:#fde4e7;color:#7d1424}
.note[hidden]{display:none}
.ev{width:100%;border-collapse:collapse;font-size:14px;min-width:640px}
.ev th{text-align:left;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#4a5a7d;padding:8px 10px;border-bottom:1px solid #d6deee}
.ev td{padding:9px 10px;border-bottom:1px solid #edf1f8;vertical-align:top}
.ev td:first-child,.ev td:nth-child(3){font-family:"JetBrains Mono",monospace;font-size:12px;white-space:nowrap}
.ev td:first-child{color:#4a5a7d}
.login{min-height:100vh;display:grid;place-items:center;padding:16px}
.login form{background:#fff;border-radius:20px;padding:28px;width:min(400px,100%);display:flex;flex-direction:column;gap:14px}
.login input{height:44px;border:1px solid #c9d3e8;border-radius:12px;padding:0 12px;width:100%}
.login label{font-size:13px;font-weight:600}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

LOGO = ('<svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true"><rect x="1" y="1" width="32" height="32" rx="10" fill="#2453ff"/>'
        '<path d="M17 7l8.5 4.9v9.8L17 26.6l-8.5-4.9v-9.8z" fill="none" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>'
        '<path d="M18.5 11.5l-4 6h3.5l-1.5 5 4.5-6.5h-3.5z" fill="#e8890c"/></svg>')

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700'
         '&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">')


def _doc(title, body, js=""):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{e(title)}</title>
{FONTS}
<style>{CSS}</style>
</head>
<body>
{body}
<script>{js}</script>
</body>
</html>"""


def login_page(error=""):
    if not (ENABLED and PASSWORD):
        inner = ('<p class="muted">The control room can\'t reach the cluster yet. Try again in a moment; if it '
                 'keeps happening, check the kube-power pod\'s logs.</p>')
    else:
        msg = {"wrong": "That password isn't right.",
               "locked": "Too many tries. Wait five minutes and try again."}.get(error, "")
        inner = f"""<p class="muted" style="font-size:14px">Admins only. Sign in to see and control the cluster behind the bank.</p>
<div style="display:flex;flex-direction:column;gap:6px"><label for="pw">Admin password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus></div>
{f'<p role="alert" style="color:#b42335;font-weight:600;font-size:14px">{e(msg)}</p>' if msg else ''}
<button class="btn" type="submit">Sign in</button>"""
    body = f"""<main class="login"><form method="post" action="{BASE}/login">
<div class="brand" style="color:#0b1b3a">{LOGO}<span><b>Kube Power</b><small style="color:#4a5a7d">DMOBNK control room</small></span></div>
{inner}
<a href="{e(APP_URL)}" style="font-size:14px">Back to {e(APP_NAME)}</a>
</form></main>"""
    return _doc("Sign in · Kube Power", body)


def control_page():
    SURGE_HTML = (f"""      <div class="act">
        <div class="ah"><h3>Traffic surge</h3><span>Autoscaling (HPA)</span></div>
        <p>Four pods hammer {e(SURGE)} for 90 seconds. The autoscaler adds pods when CPU passes its target, then removes them a minute after it calms down.</p>
        <button type="button" class="btn amber" id="s-go">Start the surge</button>
      </div>
""" if SURGE else "")
    body = f"""<header><div class="wrap">
  <a class="brand" href="{BASE}">{LOGO}<span><b>Kube Power</b><small>Break things. Watch Kubernetes fix them.</small></span></a>
  <div class="pills">
    <span class="pill"><span class="dot" id="live-dot"></span><span id="cluster">…</span></span>
    <span class="pill mono" style="font-size:12px" id="served">…</span>
    <a class="pill" href="{e(APP_URL)}" style="color:#fff;text-decoration:none">Open {e(APP_NAME)}</a>
    <form method="post" action="{BASE}/logout" class="pill"><button type="submit">Sign out</button></form>
  </div>
</div></header>
<main class="wrap">
  <section class="tiles" aria-label="Namespace health">
    <div class="tile"><span class="k">Pods ready</span><span class="v" id="t-pods">…</span><span class="s" id="t-pods-s"></span></div>
    <div class="tile"><span class="k">Deployments at strength</span><span class="v" id="t-deps">…</span><div class="bar"><span id="t-deps-bar" style="width:0"></span></div></div>
    {f'<div class="tile"><span class="k">Autoscaler · {e(sorted(HPA_APPS)[0])}</span><span class="v" id="t-hpa">…</span><span class="s" id="t-hpa-s"></span></div>' if HPA_APPS else ''}
    {f'<div class="tile"><span class="k">Last {e(CRON)} run</span><span class="v" id="t-cron">…</span><span class="s">by its CronJob</span></div>' if CRON else ''}
  </section>
  <p class="note" id="note" role="status" aria-live="polite" hidden></p>
  <div class="cols">
    <section class="panel map" aria-labelledby="map-h">
      <div class="headrow"><h2 id="map-h">Cluster map</h2><div class="legend" id="legend"></div></div>
      <div class="nodes" id="nodes"></div>
      <div class="legend"><span><i style="border:2px dashed #1f4fe0"></i>starting</span><span><i style="background:#c8d0e0"></i>terminating</span><span><i style="box-shadow:0 0 0 2px #4ade80"></i>served this page</span></div>
      <h3 style="margin-top:6px">Deployments</h3>
      <div class="scroll"><table class="deps"><thead><tr><th>Deployment</th><th>Ready</th><th></th><th>Version</th><th>Status</th></tr></thead><tbody id="deps"></tbody></table></div>
    </section>
    <aside aria-labelledby="try-h">
      <h2 id="try-h">Try it</h2>
      <div class="act">
        <div class="ah"><h3>Scale out</h3><span>Deployment replicas</span></div>
        <div class="ctl"><label for="sc-app" class="sr">Deployment</label><select id="sc-app"></select></div>
        <div class="ctl">
          <button type="button" class="step" id="sc-minus" aria-label="Fewer replicas">−</button>
          <label for="sc-n" class="sr">Replicas</label><input id="sc-n" class="num" type="number" min="{SCALE_MIN}" max="{SCALE_MAX}" value="2">
          <button type="button" class="step" id="sc-plus" aria-label="More replicas">+</button>
          <button type="button" class="btn wide" id="sc-go">Apply</button>
        </div>
      </div>
      <div class="act">
        <div class="ah"><h3>Kill a pod</h3><span>Self-healing</span></div>
        <p>Deletes a pod. Its ReplicaSet notices the gap and starts a new one, while the other pods keep the service up.</p>
        <div class="ctl"><label for="k-app" class="sr">Which deployment</label><select id="k-app"><option value="">Any deployment</option></select>
          <button type="button" class="btn red" id="k-go">Kill a pod</button></div>
      </div>
      <div class="act">
        <div class="ah"><h3>Rolling restart</h3><span>Zero-downtime update</span></div>
        <p>Replaces every pod of a deployment, a few at a time, the same way a new version rolls out.</p>
        <div class="ctl"><label for="r-app" class="sr">Deployment to restart</label><select id="r-app"></select>
          <button type="button" class="btn navy" id="r-go">Restart</button></div>
      </div>
{SURGE_HTML}    </aside>
  </div>
  <section class="panel" aria-labelledby="ev-h">
    <div class="headrow"><h2 id="ev-h">What Kubernetes just did</h2><span class="muted" style="font-size:13px">Events in the dmobnk namespace, newest first</span></div>
    <div class="scroll"><table class="ev"><thead><tr><th>Time</th><th>Reason</th><th>Object</th><th>What happened</th></tr></thead><tbody id="events"></tbody></table></div>
  </section>
  <p class="muted" style="font-size:12px">Controls are limited to the dmobnk namespace and these deployments: {e(", ".join(APPS))}. Redis isn't offered: it holds the app's state.</p>
</main>
<style>.sr{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}}</style>"""
    cfg = json.dumps({"base": BASE, "apps": APPS, "hpa": sorted(HPA_APPS), "min": SCALE_MIN, "max": SCALE_MAX})
    return _doc("Kube Power · DMOBNK control room", body, "const CFG = " + cfg.replace("</", "<\\/") + ";\n" + CONTROL_JS)


CONTROL_JS = r"""
const $ = id => document.getElementById(id);
const COLORS = {
  'web': ['#0b1b3a', '#fff'], 'accounts-svc': ['#2453ff', '#fff'], 'cards-svc': ['#7c3aed', '#fff'],
  'loans-svc': ['#0e9f6e', '#fff'], 'mortgage-svc': ['#e8890c', '#1d1200'], 'rates-svc': ['#d6336c', '#fff'],
  'crossborder-svc': ['#0891b2', '#fff'], 'redis': ['#475569', '#fff'], 'branch-web': ['#2453ff', '#fff'],
  'branch-redis': ['#475569', '#fff'], 'kube-power': ['#e8890c', '#1d1200'], 'job': ['#cbd5e1', '#0b1b3a'],
  'rates-rush': ['#fca5a5', '#450a0a'], 'other': ['#94a3b8', '#0b1b3a'],
};
const color = app => COLORS[app] || COLORS.other;
const shortApp = app => app.replace(/-svc$/, '').replace('crossborder', 'xborder');
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function ago(iso) {
  if (!iso) return '—';
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? s + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago';
}
function clock(iso) { return iso ? new Date(iso).toLocaleTimeString('en-GB') : ''; }
function note(text, isErr) { const n = $('note'); n.textContent = text; n.className = 'note' + (isErr ? ' err' : ''); n.hidden = false; }

async function call(path, body) {
  const resp = await fetch(CFG.base + '/api/' + path, {method: 'POST', cache: 'no-store', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Kube-Power': '1'}, body: JSON.stringify(body || {})});
  let data = {}; try { data = await resp.json(); } catch (err) {}
  if (resp.status === 401) { location.reload(); }
  if (!resp.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}
function action(btn, path, body) {
  return async () => {
    btn.disabled = true;
    try { note((await call(path, body())).message); tick(); } catch (err) { note(err.message, true); }
    setTimeout(() => { btn.disabled = false; }, 1200);
  };
}

function fillSelects(deps) {
  const names = deps.filter(d => d.controllable).map(d => d.name);
  for (const [id, filter] of [['sc-app', n => !CFG.hpa.includes(n)], ['k-app', () => true], ['r-app', () => true]]) {
    const sel = $(id); const keep = sel.value; const first = id === 'k-app' ? [sel.options[0]] : [];
    if (sel.dataset.filled === names.join()) continue;
    sel.replaceChildren(...first, ...names.filter(filter).map(n => { const o = el('option', '', n); o.value = n; return o; }));
    if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
    sel.dataset.filled = names.join();
  }
}
let lastScaleApp = null;
function syncScale(deps) {
  const app = $('sc-app').value; if (app === lastScaleApp) return;
  const d = deps.find(x => x.name === app); if (d) $('sc-n').value = d.desired; lastScaleApp = app;
}
let DEPS = [];

function draw(s) {
  $('cluster').textContent = [s.version || 'Kubernetes', new Set(s.pods.map(p => p.node).filter(Boolean)).size + ' nodes in use'].join(' · ');
  $('served').textContent = s.served.pod ? 'served by ' + s.served.pod + ' on ' + s.served.node : '';
  const svcPods = s.pods.filter(p => p.app !== 'job' && p.app !== 'rates-rush');
  const ready = svcPods.filter(p => p.state === 'running').length;
  const starting = svcPods.filter(p => p.state === 'starting').length, dying = svcPods.filter(p => p.state === 'terminating').length;
  $('t-pods').textContent = ready + ' / ' + (svcPods.length - dying);
  $('t-pods-s').textContent = starting || dying ? [starting && starting + ' starting', dying && dying + ' terminating'].filter(Boolean).join(', ') : 'all settled';
  const full = s.deployments.filter(d => d.ready >= d.desired && !d.rolling).length;
  $('t-deps').textContent = full + ' / ' + s.deployments.length;
  $('t-deps-bar').style.width = (s.deployments.length ? 100 * full / s.deployments.length : 0) + '%';
  if (!$('t-hpa')) {} else if (s.hpa) {
    $('t-hpa').textContent = s.hpa.current + (s.hpa.desired !== s.hpa.current ? ' → ' + s.hpa.desired : '') + ' pods';
    $('t-hpa-s').textContent = 'CPU ' + (s.hpa.cpu === null || s.hpa.cpu === undefined ? '—' : s.hpa.cpu + '%') + ' of ' + s.hpa.target + '% target · ' + s.hpa.min + '–' + s.hpa.max + ' pods' + (s.surge.length ? ' · surge running' : '');
  } else { $('t-hpa').textContent = '—'; $('t-hpa-s').textContent = 'no autoscaler found'; }
  if ($('t-cron')) $('t-cron').textContent = ago(s.cron_last);

  const apps = [...new Set(s.pods.map(p => p.app))];
  $('legend').replaceChildren(...apps.map(a => { const sp = el('span'); const i = el('i'); i.style.background = color(a)[0]; sp.append(i, document.createTextNode(a)); return sp; }));
  const byNode = {};
  for (const p of s.pods) (byNode[p.node || 'unscheduled'] ||= []).push(p);
  $('nodes').replaceChildren(...Object.keys(byNode).sort().map(n => {
    const box = el('div', 'node'); const head = el('div', 'nh');
    head.append(el('b', '', n), el('span', 'muted', byNode[n].length + ' pods')); head.lastChild.style.fontSize = '12px';
    const grid = el('div', 'podgrid');
    for (const p of byNode[n]) {
      const [bg, fg] = color(p.app);
      const chip = el('span', 'pod ' + (p.state !== 'running' ? p.state : '') + (p.name === s.served.pod ? ' served' : ''));
      chip.style.background = bg; chip.style.color = fg;
      chip.title = p.name + ' · ' + p.state + (p.restarts ? ' · ' + p.restarts + ' restarts' : '');
      chip.append(el('b', '', shortApp(p.app)), el('span', '', p.name.slice(-5)));
      grid.append(chip);
    }
    box.append(head, grid); return box;
  }));

  $('deps').replaceChildren(...s.deployments.map(d => {
    const tr = el('tr'); const nm = el('td'); const sw = el('i'); sw.style.cssText = 'display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:8px;background:' + color(d.name)[0];
    nm.append(sw, el('span', 'mono', d.name)); if (d.hpa) nm.append(el('span', 'badge b-info', 'HPA')); if (d.hpa) nm.lastChild.style.marginLeft = '8px';
    const barTd = el('td'); const bar = el('div', 'bar'); const fill = el('span'); fill.style.width = (d.desired ? Math.min(100, 100 * d.ready / d.desired) : 0) + '%'; bar.append(fill); barTd.append(bar);
    const st = el('td'); const ok = d.ready >= d.desired && !d.rolling;
    st.append(el('span', 'badge ' + (d.rolling ? 'b-warn' : ok ? 'b-ok' : 'b-bad'), d.rolling ? 'rolling out' : ok ? 'healthy' : d.ready ? 'degraded' : 'down'));
    tr.append(nm, el('td', 'mono', d.ready + ' / ' + d.desired), barTd, el('td', 'mono', d.version ? 'v' + d.version : '—'), st);
    return tr;
  }));

  $('events').replaceChildren(...s.events.map(ev => {
    const tr = el('tr'); const r = el('td');
    const warn = ev.type === 'Warning';
    const cls = warn ? 'b-warn' : /Killing|Delete|Evict/i.test(ev.reason) ? 'b-warn' : /Scaled|Rescale|Scaling/.test(ev.reason) ? 'b-info' : 'b-ok';
    r.append(el('span', 'badge ' + cls, ev.reason));
    tr.append(el('td', '', clock(ev.t)), r, el('td', '', ev.obj), el('td', '', ev.msg + (ev.count > 1 ? ' (×' + ev.count + ')' : '')));
    return tr;
  }));
  if ($('s-go')) { $('s-go').textContent = s.surge.length ? 'Surge running…' : 'Start the surge'; $('s-go').dataset.busy = s.surge.length ? '1' : ''; }

  DEPS = s.deployments; fillSelects(s.deployments); syncScale(s.deployments);
}

let busy = false;
async function tick() {
  if (busy) return; busy = true;
  try {
    const resp = await fetch(CFG.base + '/api/state', {cache: 'no-store', credentials: 'same-origin'});
    if (resp.status === 401) { location.reload(); return; }
    const s = await resp.json(); if (!resp.ok) throw new Error(s.error);
    draw(s); $('live-dot').style.background = '#4ade80';
  } catch (err) { $('live-dot').style.background = '#f87171'; }
  finally { busy = false; }
}

const clamp = n => Math.max(CFG.min, Math.min(CFG.max, n | 0));
$('sc-minus').onclick = () => { $('sc-n').value = clamp(+$('sc-n').value - 1); };
$('sc-plus').onclick = () => { $('sc-n').value = clamp(+$('sc-n').value + 1); };
$('sc-app').onchange = () => { lastScaleApp = null; syncScale(DEPS); };
$('sc-go').onclick = action($('sc-go'), 'scale', () => ({app: $('sc-app').value, replicas: clamp(+$('sc-n').value)}));
$('k-go').onclick = action($('k-go'), 'kill', () => ({app: $('k-app').value}));
$('r-go').onclick = action($('r-go'), 'restart', () => ({app: $('r-app').value}));
if ($('s-go')) $('s-go').onclick = action($('s-go'), 'surge', () => ({}));
tick(); setInterval(tick, 2000);
"""
