"""Points a DMOBNK Ingress at the cluster it's running on, so the same
manifests work on any cluster: shared by the online bank (web.py), the
branch kiosk (branch.py), the CRM (crm.py) and Kube Power, mounted from the
dmobnk-ingress-host ConfigMap.

start() runs a background loop that, while the Ingress carries the
annotation dmobnk.smclab.net/host: auto, sets
  - its host to <prefix>.<cluster external address>.sslip.io, or to a fixed
    host when one is configured (see cluster_address for the order), and
  - its IngressClass, when the cluster has no default class.
It creates the Ingress if it's missing (see ensure). An Ingress without the
annotation (e.g. dev.smclab.net on lenny-new) is left alone. The
ServiceAccount needs: create on ingresses, get/patch on that Ingress, get on
its Deployment, get and list on nodes, list on services and ingressclasses."""
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

ANNOTATION = "dmobnk.smclab.net/host"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = os.environ.get("POD_NAMESPACE", "dmobnk")


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


def to_ipv4(addr):
    """An IPv4 address for addr, resolving a hostname (an AWS load balancer
    publishes a DNS name, not an IP). None if it doesn't resolve."""
    try:
        return str(ipaddress.IPv4Address(addr))
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(addr, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError):
        return None


def is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def published_addresses(ingress):
    """The cluster's external ingress address(es), as [(address, source)]:
    what the ingress controller wrote into this Ingress's status, then the
    EXTERNAL-IP of any LoadBalancer Service serving port 80 or 443 (the
    ingress controller's, e.g. traefik on k3s or ingress-nginx behind an AWS
    load balancer)."""
    out = []
    try:
        for lb in k8s("GET", ingress).get("status", {}).get("loadBalancer", {}).get("ingress") or []:
            if lb.get("ip") or lb.get("hostname"):
                out.append((lb.get("ip") or lb["hostname"], "Ingress status"))
    except Exception as err:
        print(f"couldn't read the Ingress status: {err}", flush=True)
    try:
        for svc in k8s("GET", "/api/v1/services")["items"]:
            spec = svc.get("spec", {})
            if spec.get("type") != "LoadBalancer" or not {p.get("port") for p in spec.get("ports", [])} & {80, 443}:
                continue
            name = f"{svc['metadata']['namespace']}/{svc['metadata']['name']}"
            for lb in svc.get("status", {}).get("loadBalancer", {}).get("ingress") or []:
                if lb.get("ip") or lb.get("hostname"):
                    out.append((lb.get("ip") or lb["hostname"], f"LoadBalancer {name}"))
    except Exception as err:
        print(f"couldn't list LoadBalancer Services: {err}", flush=True)
    return out


def cluster_address(ingress):
    """(IPv4, where it came from) for the sslip.io host. The first PUBLIC
    address of: the cluster's external ingress address (published_addresses),
    the ExternalIP of the first Ready worker node by name (the same for every
    replica), the EC2 instance's public IP (metadata, then on EC2
    its egress IP). With none public (on-prem): the ingress address, else the
    node's ExternalIP, else its InternalIP."""
    private = []

    def consider(addr, source):
        ip = to_ipv4(addr) if addr else None
        if not ip:
            return None
        src = source if ip == addr else f"{source} ({addr})"
        if is_public(ip):
            return ip, src
        private.append((ip, src))
        return None

    for addr, source in published_addresses(ingress):
        hit = consider(addr, source)
        if hit:
            return hit
    # Every replica must pick the SAME node, or they keep overwriting each
    # other's host: so not "my node" but the first Ready worker by name.
    pool = []
    try:
        pool = node_pool()
    except Exception as err:
        print(f"couldn't list nodes: {err}", flush=True)
    for n in pool:
        for a in n["status"].get("addresses", []):
            if a.get("type") == "ExternalIP":
                hit = consider(a.get("address"), f"node ExternalIP of {n['metadata']['name']}")
                if hit:
                    return hit
    name = os.environ.get("NODE_NAME", "")
    node = next((n for n in pool if n["metadata"]["name"] == name), {})
    hit = consider(ec2_public_ip(), "EC2 instance metadata")
    if hit:
        return hit
    if on_ec2(node):
        hit = consider(egress_ip(), "EC2 egress IP (checkip.amazonaws.com)")
        if hit:
            return hit
        print("on EC2 but found no public address: set a fixed host (see the README)", flush=True)
    if private:
        return private[0]
    for n in pool:
        for a in n["status"].get("addresses", []):
            if a.get("type") == "InternalIP" and a.get("address"):
                return a["address"], f"node InternalIP of {n['metadata']['name']}"
    ip = os.environ.get("NODE_IP", "").strip()
    return (ip, "node InternalIP") if ip else (None, None)


# Where an address came from that only this pod's node can see: another
# replica on another node would find a different one.
PER_NODE = ("EC2 instance metadata", "EC2 egress IP")


def node_pool():
    """Ready, schedulable nodes, workers first (if any), sorted by name."""
    ready = []
    for n in k8s("GET", "/api/v1/nodes")["items"]:
        conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        if conds.get("Ready") == "True" and not n.get("spec", {}).get("unschedulable"):
            ready.append(n)
    labels = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
    workers = [n for n in ready if not any(k in n["metadata"].get("labels", {}) for k in labels)]
    return sorted(workers or ready, key=lambda n: n["metadata"]["name"])


def ingress_class(current):
    """The class to use: the current one if the cluster has it, else the
    cluster's default class, else its only class, else traefik or nginx.
    (Never keeps a class the cluster doesn't have, e.g. nginx left over from
    an older manifest on a traefik cluster.)"""
    try:
        items = k8s("GET", "/apis/networking.k8s.io/v1/ingressclasses")["items"]
    except Exception:
        return current
    names = [i["metadata"]["name"] for i in items]
    if current in names:
        return current
    for i in items:
        if i["metadata"].get("annotations", {}).get("ingressclass.kubernetes.io/is-default-class") == "true":
            return i["metadata"]["name"]
    if len(names) == 1:
        return names[0]
    for pick in ("traefik", "nginx"):
        if pick in names:
            return pick
    return current


def ensure(name, prefix, path, service, owner):
    """Creates Ingress `name` if it doesn't exist, owned by Deployment `owner`
    (so it goes when the app does). The app makes its own Ingress, rather
    than shipping one in the Fleet bundle, because it rewrites the host: with
    both Fleet (server-side apply) and the app owning .spec.rules, every Fleet
    upgrade failed on the conflict."""
    try:
        k8s("GET", f"/apis/networking.k8s.io/v1/namespaces/{NS}/ingresses/{name}")
        return
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
    dep = k8s("GET", f"/apis/apps/v1/namespaces/{NS}/deployments/{owner}")
    body = {
        "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
        "metadata": {"name": name, "namespace": NS, "annotations": {ANNOTATION: "auto"},
                     "labels": {"app.kubernetes.io/part-of": NS},
                     "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": owner,
                                          "uid": dep["metadata"]["uid"], "blockOwnerDeletion": False}]},
        "spec": {"rules": [{"host": f"{prefix}.node-ip.sslip.io", "http": {"paths": [
            {"path": path, "pathType": "Prefix", "backend": {"service": {"name": service, "port": {"number": 80}}}}]}}]},
    }
    try:
        k8s("POST", f"/apis/networking.k8s.io/v1/namespaces/{NS}/ingresses", body)
        print(f"created Ingress {name} ({path} -> {service})", flush=True)
    except urllib.error.HTTPError as err:
        if err.code != 409:     # 409: another replica made it first
            raise


def _loop(ingress, prefix, fixed_host, fixed_class, follow, create):
    address, checked, ttl, source = None, 0.0, 0, None
    while True:
        try:
            if create:
                ensure(*create)
            ing = k8s("GET", ingress)
            if ing["metadata"].get("annotations", {}).get(ANNOTATION) != "auto":
                time.sleep(30)
                continue
            host = fixed_host
            if follow:
                # Share another Ingress's host and class (Kube Power sits on its app's host).
                other = k8s("GET", f"/apis/networking.k8s.io/v1/namespaces/{NS}/ingresses/{follow}")
                h = other["spec"]["rules"][0].get("host", "")
                host = h if h and "node-ip.sslip.io" not in h else None
                fixed_class = other["spec"].get("ingressClassName") or fixed_class
            elif not host:
                # Re-resolved every 5 minutes once it's a public or published
                # address; every 30 s while it's only a node's private address,
                # since a cloud load balancer can take a minute or two to get
                # its external address.
                if not address or time.time() - checked > ttl:
                    found, source = cluster_address(ingress)
                    if found and found != address:
                        print(f"using {source} {found}", flush=True)
                    address, checked = found or address, time.time()
                    ttl = 30 if not found or (source or "").startswith("node ") else 300
                host = f"{prefix}.{address}.sslip.io" if address else None
                current = ing["spec"]["rules"][0].get("host", "")
                if (source or "").startswith(PER_NODE) and current.startswith(prefix + ".") and "node-ip.sslip.io" not in current:
                    # Only this node knows that address, so another replica may
                    # have set a different, equally good one: keep it.
                    host = current
            spec = ing["spec"]
            ops = []
            if host and spec["rules"][0].get("host") != host:
                ops.append({"op": "replace", "path": "/spec/rules/0/host", "value": host})
            cls = fixed_class or ingress_class(spec.get("ingressClassName"))
            if cls and cls != spec.get("ingressClassName"):
                ops.append({"op": "add", "path": "/spec/ingressClassName", "value": cls})
            if ops:
                k8s("PATCH", ingress, ops, "application/json-patch+json")
                print(f"ingress set to host {host}, class {cls or '(cluster default)'}", flush=True)
        except Exception as err:
            print(f"ingress update failed: {err}", flush=True)
        time.sleep(10)


def start(name, prefix, fixed_host="", fixed_class="", follow="", path="/", service="", owner=""):
    """Keep Ingress `name` (in this pod's namespace) pointed at this cluster,
    creating it (path -> service:80, owned by Deployment `owner`) if it's
    missing. An existing Ingress without the auto annotation (e.g. the static
    one for dev.smclab.net) is left alone."""
    if not os.path.exists(f"{SA}/token"):
        return
    ingress = f"/apis/networking.k8s.io/v1/namespaces/{NS}/ingresses/{name}"
    threading.Thread(target=_loop, args=(ingress, prefix, fixed_host.strip(), fixed_class.strip(), follow.strip(),
                                                      (name, prefix, path, service or name, owner or service or name)),
                     daemon=True).start()
