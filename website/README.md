# Website Demo

A minimal static site for testing this k8s cluster -- deployments, rollouts, scaling, Service load-balancing, self-healing -- unrelated to the `trading/` demo.

Inspired by [stevemc-suse/rancher-k3s-fleet-examples](https://github.com/stevemc-suse/rancher-k3s-fleet-examples/tree/master/fleet-examples).

`website` is a Deployment, normally 3 replicas (`website-load.yaml` bursts it to 10 every 5 minutes -- see below), with a **preferred** pod anti-affinity on `kubernetes.io/hostname` -- the scheduler tries not to place a website pod on a node that already has one, so a `website-chaos` kill's replacement generally lands on a free node instead of piling onto an occupied one. Deliberately *preferred*, not required: a required anti-affinity would make 10 replicas on 5 nodes unsatisfiable (5 pods stuck `Pending` forever). Each pod runs three containers:
- `render` (`alpine`, init) -- substitutes the pod name, node name, and pod IP into the page template via the downward API, once at startup.
- `nginx` (`nginx:1.27-alpine`) -- serves the rendered page and reverse-proxies `/api/` to the sidecar below. Sends `Cache-Control: no-store` on everything, so the page's own polling always reaches a live pod instead of a cache.
- `api` (`python:3.12-alpine`) -- a small stdlib `http.server` sidecar exposing `GET /topology`, `GET /stats`, and `POST /visit` (see below). Queries the k8s API (via its mounted ServiceAccount token) for topology, and talks to `website-redis` for the visit counter.

Plus `website-redis` (`redis.yaml`) -- a single `redis:7-alpine` pod with `--appendonly yes` on a 256Mi PVC (`website-redis-data`, `longhorn`), backing the visit counter below. `strategy: Recreate` since a rolling update would try to attach the same RWO volume to two pods at once.

A `NetworkPolicy` (also `redis.yaml`) locks it down to only `app: website` pods on port 6379 -- selecting `website-redis` at all flips it to default-deny Ingress, so every other pod in the namespace (including `trading/`'s own unrelated `redis`, and `website-chaos`, which never needed access) loses access unless explicitly allowed. This cluster runs Calico (confirmed `calico-node` on all 5 nodes before relying on it -- `NetworkPolicy` is a no-op object on a CNI that doesn't enforce it), so it's actually enforced, not just documentation. Verified both directions live: a raw socket connect from a `website` pod's `api` container succeeds; the same attempt from `website-chaos` hangs until it times out (Calico drops the packets rather than rejecting them, so a blocked connection looks like a hang, not a fast refusal).

The page itself does the rest client-side, no manual reload needed:
- **Served-by card** -- polls `/` every ~1.5s (cache-busted) and updates in place when the answering pod changes.
- **Total visits** -- `POST /api/visit` fires once per real page load (not on every 1.5s poll, or the number would be meaningless) and `INCR`s a counter in Redis; the card re-`GET /api/stats`s every ~5s so it also creeps up if you open the page in another tab. Survives a pod restart, a rollout, even `website-redis` itself being deleted and recreated -- it's on a PVC, not in a container's ephemeral filesystem.
- **Cluster topology** -- refetches `/api/topology` every ~4s and draws it as a network diagram (hand-authored inline SVG, no charting library): browser to the traefik Ingress to the `website` Service, fanning out to every pod the Service can route to, grouped under the node each is scheduled on (node box shows its OS, from `status.nodeInfo.osImage`, abbreviated -- e.g. "SUSE Linux Enterprise Server 16.0" to "SLES 16.0" -- hover for the full string), containers listed inside each pod box (hover a container for its image). The one path an actual poll took -- Service to whichever pod answered -- is highlighted in green. Nodes with no website pod scheduled are drawn empty, so scaling or draining a node is visible here too. Each pod gets a fixed-height slot stacked vertically within its node's box, and the box grows to fit however many pods land there rather than shrinking them to fit -- matters once `website-load.yaml` piles several pods onto the same node during a burst. Below the node row, `website-redis` is drawn as its own tier -- which node it landed on, its container/ready state, and the PVC backing it (drawn as a cylinder, the standard database-icon shorthand) -- with fan-in arrows from every pod, since it's each pod's `api` container talking to Redis directly, not something the Service routes to. A small dot continuously travels browser -> ingress -> service (pure CSS `@keyframes` on `cy`, loops on its own, no JS timer) -- those three boxes sit on a fixed vertical line regardless of node count, so it never needs recomputing per render. The "You (browser)" box also shows a live count of every request this tab has actually made (every `fetch()` in the page is routed through one `trackedFetch()` wrapper that increments it) -- not a simulated number, the real count.
- **Live DB traffic** -- the Redis box and its fan-in arrows flash blue with a `WRITE ↓` / `READ ↑` tag the instant a real request goes out: once per page load for the visit-counting `POST /api/visit`, and every ~5s for the counter-refreshing `GET /api/stats`. Not a decorative animation -- it only fires from an actual fetch resolving.

The topology fetch has a 3s `AbortController` timeout, and a failed poll only shows the "temporarily unavailable" placeholder if nothing has ever rendered yet -- otherwise the last-good diagram just stays on screen until the next poll succeeds. Learned this the hard way: `website-load`'s 10-replica burst (or `website-chaos` catching a pod mid-poll) can make a single `/api/topology` request miss, and wiping a perfectly fine diagram over one flaky poll read as broken, not "recovering."

RBAC (`rbac.yaml`): the `website` ServiceAccount can `list` Pods and `list` Nodes (cluster-scoped, since nodes aren't namespaced) -- both read-only, same narrow-grant pattern `trading/dashboard-rbac.yaml` uses -- plus `create` on `jobs` (dispatches "chaos to madness" runs, below) and `get`+`patch` on `website-chaos`'s own `/scale` subresource (the pause button, below). Talking to Redis doesn't need RBAC -- it's a plain Service connection, not a k8s API call.

### Chaos to madness

A control in the "Danger zone" panel: pick how many requests to send (10-5,000), and the platform scales `website` to accommodate them -- roughly 1 pod per 10 requests, clamped to 3-100 (`replicas_for()` in `api.py`; `MADNESS_MAX_REPLICAS=100` was resource-checked safe earlier this session against the cluster's real headroom, not picked arbitrarily) -- fires that many requests at `website`'s ClusterIP, holds for 60s at that pod count, then scales back to 3. Requests drive the pod count, not the other way around, the way a real autoscaler reacts to demand rather than you picking a replica count out of the air. The number-input's live "≈ N pods" preview mirrors the same formula purely for display; the server always recomputes it itself from the request count rather than trusting a client-supplied replica count.

**Runs as a one-off Kubernetes Job, not a thread in the `api` sidecar** -- that was the original version, and it had a real bug: the sidecar is itself an `app=website` pod, and `website-chaos` picks its targets at random every 70s. If it happened to kill the *specific* pod running the background thread mid-sequence, the thread died with it -- the scale-back-to-3 step never ran, silently leaving the Deployment stuck at 50+ replicas with nothing left to fix it, and every *other* pod's `GET /api/madness` reported `running: false` the whole time since the state was that one pod's local memory, not shared. Both are now fixed: the run is a Job (`serviceAccountName: website-load`, reusing its existing scale grant; labeled `app: website-madness`, not `app: website`, so `website-chaos` can never target it and it never joins the Service's routing), and state lives in a Redis hash (`website:madness`) so every `api` pod's `GET /api/madness` reads the same thing regardless of which one answers. The Job's own script traps `EXIT` to force the scale-back-down and lock release on *any* exit path -- success, a `set -e` failure mid-script, or a `SIGTERM` from `activeDeadlineSeconds` -- so a mid-run failure can't leave `website` stuck scaled up either. A `SET NX EX 300` lock in Redis is what actually enforces "one run at a time" now (not an in-process lock, which only ever protected against races within one pod).

The Job's pod gets its own `NetworkPolicy` entry (`redis.yaml`) to reach `website-redis` and write progress, since it isn't labeled `app: website`.

Verified against the live cluster end to end: triggered a real run (`{"requests": 30}` -> correctly computed `replicas: 3`, the clamped minimum), watched it progress `starting -> scaling up -> firing requests -> holding -> idle` via `GET /api/madness`, confirmed the Job showed `Complete` and `website` was back at `3/3`; also confirmed a concurrent second `POST` while one's running correctly returns `409` rather than starting a second Job.

### Chaos (`chaos.yaml`)

`website-chaos` is a separate single-replica Deployment (its own `app: website-chaos` label and ServiceAccount, so it can never target itself) that loops forever: sleep 70s, list pods labeled `app=website`, pick one at random (`$RANDOM` inside the one long-lived shell -- not `awk`'s `srand()`, which reseeds from wall-clock time and gives the *same* pick if two calls land in the same second), then a plain graceful `DELETE`. Only ever touches `app=website` pods -- never `website-redis`, never anything in `trading/` -- and the Deployment controller recreates whatever it kills within a couple seconds, so the replica count self-heals continuously. Its Role can technically `delete` any pod in the namespace (no `resourceNames` filter, same tradeoff as `website-read-pods`); the label scoping happens in the script, not RBAC.

A graceful delete, not `gracePeriodSeconds=0` -- that was the original version, and it was the actual cause of real "site unavailable" windows: the endpoints controller removes a pod from the Service the moment `deletionTimestamp` is set (before the grace period even starts), so a graceful delete already stops new traffic to it immediately; the force-kill bought nothing except severing whatever request was already in flight. Self-healing is still fully visible, just not disruptive to watch.

**Pause button** in the Danger zone panel does the same thing as `kubectl scale deployment/website-chaos -n apps --replicas=0`, just from the page -- `POST /api/chaos` toggles between 0 and 1, `GET /api/chaos` (polled every 5s by every open tab, same shared-state pattern as madness) reports which. No lock needed here, unlike madness: it's one idempotent scale call either way, not a multi-step sequence that can be interrupted mid-flight. Verified live: toggled it off (`website-chaos` settled to `0/0`), then back on (`0/1`).

Watch chaos happen from the CLI: `kubectl logs -n apps deploy/website-chaos -f` (logs the pod name and timestamp of every kill), alongside `kubectl get pods -n apps -l app=website -w`.

### Load simulation (`load-cron.yaml`)

`website-load` is a CronJob, `*/5 * * * *`, with its own ServiceAccount scoped to `patch` `website`'s `/scale` subresource and `get` its own `Ingress` object (both `resourceNames`-restricted to `website`). Each run:

1. Reads the live Ingress host (`website.<traefik-ip>.sslip.io` -- `deploy.sh` sets it dynamically, so the job can't hardcode it) and fires 40 requests at it *through traefik*, using curl's `--connect-to` to redirect only the TCP connection to traefik's in-cluster Service address while keeping SNI and the `Host` header as the real hostname -- this is genuine ingress traffic, not a shortcut straight to the `website` Service.
2. Scales `website` to 10 in response, holds for 90s, scales back to 3.

Verified against the live cluster with a manually triggered run (`kubectl create job --from=cronjob/website-load`): 40/40 requests returned 200, the Deployment reached `10/10 Ready`, and it correctly scaled back down after the hold.

## Deploying

```
website/deploy.sh
```

Looks up the cluster's own `traefik` Service (by label, `app.kubernetes.io/name=rke2-traefik`, wherever it lives) for its *current* LoadBalancer IP, then applies `website/` with the Ingress host set to `website.<that-ip>.sslip.io` -- resolved fresh from the cluster every run, same pattern as `trading/deploy.sh`.

`kubectl apply -k website/` also works directly, but leaves the placeholder host `website.example.com` in place (useful for `--dry-run=server`/CI, or when you don't need the Ingress reachable at all).

Everything carries the label `app.kubernetes.io/part-of=website-demo`, so it can be queried or torn down as one unit:

```
kubectl get all,ingress,pvc -l app.kubernetes.io/part-of=website-demo -n apps

kubectl delete all,ingress,configmap,pvc -l app.kubernetes.io/part-of=website-demo -n apps
```

The `delete` above also deletes `website-redis-data`, the PVC -- and with it, the visit counter. Leave `pvc` off that command if you want the count to survive a teardown/redeploy.

Reuses the shared `apps` namespace (same as `trading/`), but nothing else is shared -- separate Deployment, Service, Ingress, and label set.
