# Website Demo

A minimal static site for testing this k8s cluster -- deployments, rollouts, scaling, Service load-balancing -- unrelated to the `trading/` demo.

Each of the 3 replica pods runs three containers:
- `render` (`alpine`, init) -- substitutes the pod name, node name, and pod IP into the page template via the downward API, once at startup.
- `nginx` (`nginx:1.27-alpine`) -- serves the rendered page and reverse-proxies `/api/` to the sidecar below. Sends `Cache-Control: no-store` on everything, so the page's own polling always reaches a live pod instead of a cache.
- `api` (`python:3.12-alpine`) -- a small stdlib `http.server` sidecar exposing `GET /topology`, which queries the k8s API (via its mounted ServiceAccount token) for every node and every `app=website` pod on it, containers included.

The page itself does the rest client-side, no manual reload needed:
- **Served-by card** -- polls `/` every ~1.5s (cache-busted) and updates in place when the answering pod changes.
- **Leaderboard** -- a live bar chart tallying hits per pod from those same polls. Each pod gets a fixed color the first time it's seen (from a colorblind-validated 4-hue set) and keeps it regardless of rank, so the bars can freely re-sort by count without repainting.
- **Cluster topology** -- refetches `/api/topology` every ~4s and renders node > pod > container, highlighting whichever pod is currently answering. Nodes with no website pod scheduled show as empty, so scaling or draining a node is visible here too.

RBAC (`rbac.yaml`): the `website` ServiceAccount can only `list` Pods (namespaced) and `list` Nodes (cluster-scoped, since nodes aren't namespaced) -- read-only, same narrow-grant pattern `trading/dashboard-rbac.yaml` uses.

## Deploying

```
website/deploy.sh
```

Looks up the cluster's own `traefik` Service (by label, `app.kubernetes.io/name=rke2-traefik`, wherever it lives) for its *current* LoadBalancer IP, then applies `website/` with the Ingress host set to `website.<that-ip>.sslip.io` -- resolved fresh from the cluster every run, same pattern as `trading/deploy.sh`.

`kubectl apply -k website/` also works directly, but leaves the placeholder host `website.example.com` in place (useful for `--dry-run=server`/CI, or when you don't need the Ingress reachable at all).

Everything carries the label `app.kubernetes.io/part-of=website-demo`, so it can be queried or torn down as one unit:

```
kubectl get all,ingress -l app.kubernetes.io/part-of=website-demo -n apps

kubectl delete all,ingress,configmap -l app.kubernetes.io/part-of=website-demo -n apps
```

Reuses the shared `apps` namespace (same as `trading/`), but nothing else is shared -- separate Deployment, Service, Ingress, and label set.
