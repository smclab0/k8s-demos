# Website Demo

A minimal static site for testing this k8s cluster -- deployments, rollouts, scaling, Service load-balancing -- unrelated to the `trading/` demo.

Each of the 3 replica pods runs three containers:
- `render` (`alpine`, init) -- substitutes the pod name, node name, and pod IP into the page template via the downward API, once at startup.
- `nginx` (`nginx:1.27-alpine`) -- serves the rendered page and reverse-proxies `/api/` to the sidecar below. Sends `Cache-Control: no-store` on everything, so the page's own polling always reaches a live pod instead of a cache.
- `api` (`python:3.12-alpine`) -- a small stdlib `http.server` sidecar exposing `GET /topology`, `GET /stats`, and `POST /visit` (see below). Queries the k8s API (via its mounted ServiceAccount token) for topology, and talks to `website-redis` for the visit counter.

Plus `website-redis` (`redis.yaml`) -- a single `redis:7-alpine` pod with `--appendonly yes` on a 256Mi PVC (`website-redis-data`, `longhorn`), backing the visit counter below. `strategy: Recreate` since a rolling update would try to attach the same RWO volume to two pods at once.

The page itself does the rest client-side, no manual reload needed:
- **Served-by card** -- polls `/` every ~1.5s (cache-busted) and updates in place when the answering pod changes.
- **Total visits** -- `POST /api/visit` fires once per real page load (not on every 1.5s poll, or the number would be meaningless) and `INCR`s a counter in Redis; the card re-`GET /api/stats`s every ~5s so it also creeps up if you open the page in another tab. Survives a pod restart, a rollout, even `website-redis` itself being deleted and recreated -- it's on a PVC, not in a container's ephemeral filesystem.
- **Cluster topology** -- refetches `/api/topology` every ~4s and draws it as a network diagram (hand-authored inline SVG, no charting library): browser to the traefik Ingress to the `website` Service, fanning out to every pod the Service can route to, grouped under the node each is scheduled on, containers listed inside each pod box (hover a container for its image). The one path an actual poll took -- Service to whichever pod answered -- is highlighted in green. Nodes with no website pod scheduled are drawn empty, so scaling or draining a node is visible here too. Below the node row, `website-redis` is drawn as its own tier -- which node it landed on, its container/ready state, and the PVC backing it (drawn as a cylinder, the standard database-icon shorthand) -- with fan-in arrows from every pod, since it's each pod's `api` container talking to Redis directly, not something the Service routes to.
- **Live DB traffic** -- the Redis box and its fan-in arrows flash blue with a `WRITE ↓` / `READ ↑` tag the instant a real request goes out: once per page load for the visit-counting `POST /api/visit`, and every ~5s for the counter-refreshing `GET /api/stats`. Not a decorative animation -- it only fires from an actual fetch resolving.

RBAC (`rbac.yaml`): the `website` ServiceAccount can only `list` Pods (namespaced) and `list` Nodes (cluster-scoped, since nodes aren't namespaced) -- read-only, same narrow-grant pattern `trading/dashboard-rbac.yaml` uses. Talking to Redis doesn't need RBAC -- it's a plain Service connection, not a k8s API call.

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
