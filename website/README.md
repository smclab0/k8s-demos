# Website Demo

A minimal static site for testing this k8s cluster -- deployments, rollouts, scaling, Service load-balancing -- unrelated to the `trading/` demo.

`nginx:1.27-alpine` serving one HTML page (`website.yaml`'s ConfigMap), rendered at pod startup by an `initContainer` that substitutes the pod name, node name, and pod IP into it via the downward API, so reloading the page shows which of the 3 replicas (and which node) answered -- useful for watching the Service spread load, or for seeing pods rotate out during a rollout or `kubectl drain`.

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
