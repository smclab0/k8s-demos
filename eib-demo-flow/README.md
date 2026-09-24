# EIB Demo Flow

A live demo of [SUSE Edge Image Builder](https://github.com/suse-edge/edge-image-builder) (EIB)'s build pipeline -- a definition file and a base image go in, a customized bootable image comes out. Same architecture and conventions as `trading/` and `website/`, unrelated to either.

This demos the **build pipeline only** (definition file -> EIB build -> output image), not what happens after the image boots (network self-config, Kubernetes bootstrap, workload deploy) -- that's a real, separate flow and out of scope here.

## What it shows

- **Pipeline diagram** -- hand-authored inline SVG: `Definition file` + `Base image` feed into `EIB Build`, which produces `Output image`. The build box glows blue while running; the output box turns green once the build reaches `done`.
- **Stage checklist** -- the six real EIB pipeline stages (validate definition, fetch base image, inject OS customization, resolve/cache RPM packages, embed Kubernetes + air-gap images, assemble output image), each showing pending / active (pulsing) / done.
- **Build log** -- a scrolling, real log of what the simulated build is doing, stage by stage.
- **Recent builds** -- last 10 runs (finish time, duration, success/interrupted), persisted in Redis.

## Why it's simulated, not a real EIB build

A real EIB run pulls a multi-GB SLE Micro base image and needs real SUSE registry access -- impractical for a demo Job. `eib-flow-build`'s script instead runs the same *shape* of pipeline (same stage names, same relative time weights) compressed to ~25 seconds, so the whole thing is watchable end to end rather than accurate down to the second.

## Architecture

Two Deployments, same shared `apps` namespace as `trading/` and `website/`, own `app.kubernetes.io/part-of=eib-demo-flow` label:

- **`eib-flow`** (2 replicas) -- `nginx` serving the static page (no per-pod downward-API rendering needed here, unlike `website/`, since this page doesn't show its own pod/node identity) + an `api` sidecar (`python:3.12-alpine`, stdlib `http.server`) exposing `GET/POST /build`, `GET /history`.
- **`eib-flow-redis`** -- `redis:7-alpine`, `--appendonly yes` on a 256Mi PVC (`eib-flow-redis-data`, `longhorn`), so build history and in-progress state survive a pod restart. `strategy: Recreate`, same RWO-volume reasoning as `website-redis`.

**"Run build" dispatches a one-off Job, not an in-process thread** -- `website/`'s original "chaos to madness" button learned this lesson the hard way (a background thread inside a pod something else in the namespace could kill mid-run left the Deployment stuck). Here there's no `website-chaos` equivalent threatening the `eib-flow` pods, but the same shape of bug is possible in principle (a rollout, an eviction, anything that recycles the pod mid-build), so it's built the safe way from the start: `POST /api/build` creates a `Job` (`serviceAccountName: eib-flow-build`, pod labeled `app: eib-flow-build` so it never joins the `eib-flow` Service's routing) which runs entirely independently and writes progress to a Redis hash (`eib:build`) plus a log list (`eib:build:log`), so `GET /api/build` reports the same state regardless of which `eib-flow` pod answers. The Job's script traps `EXIT` to record an `interrupted` history entry and release the `SET NX EX 300` Redis lock on *any* exit path (success, a `set -e` failure, or a `SIGTERM` from `activeDeadlineSeconds`), so a mid-run failure can't leave the button stuck saying "in progress" forever, and can't block the next run indefinitely either.

RBAC (`rbac.yaml`): the `eib-flow` ServiceAccount can only `create` `jobs` (batch/v1) -- nothing else, no k8s API topology reads the way `website/` has (this demo doesn't show cluster topology, just the pipeline). The build Job's own `eib-flow-build` ServiceAccount has no RBAC bound to it at all -- it never touches the k8s API, only Redis over the network.

A `NetworkPolicy` (`redis.yaml`) locks `eib-flow-redis` to only `app: eib-flow` and `app: eib-flow-build` pods on port 6379 -- selecting it at all flips it to default-deny Ingress, so `trading/`'s and `website/`'s own unrelated redis instances (and everything else in the shared namespace) lose access unless explicitly allowed.

Verified against the live cluster end to end: triggered a real build, watched it progress through every stage via `GET /api/build` (log lines appearing in order, progress climbing 15 -> 30 -> 50 -> 68 -> 85 -> 97 -> 100), confirmed the Job showed `Complete`, confirmed a concurrent second `POST` while one's running correctly returns `409`, and confirmed two consecutive runs both landed in `GET /api/history`.

## Deploying

```
eib-demo-flow/deploy.sh
```

Looks up the cluster's own `traefik` Service (by label, `app.kubernetes.io/name=rke2-traefik`, wherever it lives) for its *current* LoadBalancer IP, then applies `eib-demo-flow/` with the Ingress host set to `eib-flow.<that-ip>.sslip.io` -- resolved fresh from the cluster every run, same pattern as `trading/deploy.sh` and `website/deploy.sh`.

`kubectl apply -k eib-demo-flow/` also works directly, but leaves the placeholder host `eib-flow.example.com` in place (useful for `--dry-run=server`/CI, or when you don't need the Ingress reachable at all).

Everything carries the label `app.kubernetes.io/part-of=eib-demo-flow`, so it can be queried or torn down as one unit:

```
kubectl get all,ingress,pvc,networkpolicy,serviceaccount,role,rolebinding -l app.kubernetes.io/part-of=eib-demo-flow -n apps

kubectl delete all,ingress,configmap,networkpolicy,serviceaccount,role,rolebinding -l app.kubernetes.io/part-of=eib-demo-flow -n apps
```

The `delete` above intentionally leaves the `eib-flow-redis-data` PVC in place, so build history survives a teardown/redeploy -- add `pvc` to the command if you want that wiped too.

Reuses the shared `apps` namespace (same as `trading/` and `website/`), but nothing else is shared -- separate Deployments, Services, Ingress, and label set.
