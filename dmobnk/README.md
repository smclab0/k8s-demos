# DMOBNK · Demo Bank Ldn

A fictional bank that runs as one of four countries: UK (£), US ($), Germany (€) or the Sims (Simlish, §). Each cluster's country, and with it the site's language, currency and number format, comes from a label on that cluster (see below). All the figures are examples and no real money moves.

Pages: **Home** (services, your card at a glance, exchange rates, gold price), **Cards** (compare cards, balance, recent spending, freeze a card), **Mortgages** (repayment calculator and an agreement in principle) and **Rates** (exchange rates, gold price, savings rates).

## How it's built

| Piece | Role |
|---|---|
| `web` | The front end (`web.py`). Its text, currency and number formats come from the `locale-<country>` ConfigMap it mounts. |
| `accounts-svc`, `cards-svc`, `loans-svc`, `mortgage-svc`, `rates-svc`, `crossborder-svc` | One backend service per banking service, all running `svc.py`. |
| `mortgage-svc` | Runs each agreement-in-principle check as a short Job. |
| `rates-refresh` CronJob | Moves the example rates and gold price every minute. |
| `redis` | Shared state (card frozen, applications, rates). |
| `control.py` | **Kube Power**, the admin-only control room at `/dmobnk/admin`, served by the `web` pods. |

## Kube Power control room (admins only)

`https://dev.smclab.net/dmobnk/admin` (or the **Admin** link in the bank's footer) opens the control room after you sign in with the admin password. It shows the `dmobnk` namespace live: pods on each node, the deployments, the rates autoscaler, the rates CronJob and recent events. You can also:

| Control | What it shows |
|---|---|
| **Scale out** | Set a deployment's replicas (1 to 6). Not `rates-svc`, which its autoscaler sizes. |
| **Kill a pod** | Self-healing: a pod is deleted and its ReplicaSet replaces it. It only kills where another ready pod keeps the service up. |
| **Rolling restart** | Zero-downtime rollout: every pod of a deployment is replaced, a few at a time. |
| **Traffic surge** | Autoscaling: a Job of 4 curl pods loads `rates-svc` for 90 s, and the HPA adds pods, then removes them. |

`redis` is never offered, since it holds the bank's state. The web pods' `dmobnk-web` Role (`base/admin.yaml`) only covers the `dmobnk` namespace, and it can only change the bank's own deployments.

The password is the `password` key of the `dmobnk-admin` Secret, which isn't kept in git. `deploy.sh` creates one with a random password the first time and prints it. With Fleet, create the Secret yourself (the control room stays switched off until you do):

```sh
kubectl -n dmobnk create secret generic dmobnk-admin --from-literal=password='choose-one'
kubectl -n dmobnk rollout restart deploy/web
# read it back later:
kubectl -n dmobnk get secret dmobnk-admin -o jsonpath='{.data.password}' | base64 -d
```

A sign-in lasts 8 hours. Five wrong passwords from one address lock it out for 5 minutes. If Fleet's drift correction is on, it will undo scaling done from the control room.

## Branch counter (clusters labelled as a branch)

A cluster labelled `dmobnk.smclab.net/role=branch` gets the **branch counter** app *instead of* the online bank: the web app for one high-street branch, running on the branch's own (edge) cluster.

| Screen | Path | For |
|---|---|---|
| **Take a ticket** | `/` | Customers: a kiosk to take a queue ticket for cash, accounts or mortgages, with the wait ahead of them. |
| **Queue board** | `/board` | The "now serving" display: each counter's current ticket and who's waiting. |
| **Teller** | `/teller` | Staff: call the next customer to your counter, record cash paid in or taken out, see the branch's cash (with a low-cash warning), check in today's appointments and see today's transactions. |

It's `branch-web` (`branch/base/branch.py`, 2 replicas) plus its own `branch-redis`, which holds the queue, counters and cash. The branch's language, currency and name follow the cluster's `dmobnk.smclab.net/country` label, the same as the online bank. All customers and figures are examples.

**Where it's served.** A kustomize bundle can't read a cluster label's value, so `branch-web` sets its own Ingress (plain HTTP) once it's running:
- host: `branch.<node address>.sslip.io` (the node's ExternalIP, else its InternalIP);
- class: the cluster's default IngressClass, else its only one, else `traefik` or `nginx`.

To override either, or the branch's name, create a `dmobnk-branch` ConfigMap on that cluster, then restart `branch-web`:

```sh
kubectl -n dmobnk create configmap dmobnk-branch \
  --from-literal=host=branch.example.com --from-literal=ingressClass=nginx --from-literal=name='Canary Wharf branch'
kubectl -n dmobnk rollout restart deploy/branch-web
```

The teller screen has no sign-in: it's for the branch's own network. Don't expose a branch cluster's Ingress to the internet without putting one in front of it.

## Layout

- `base/`: the online bank's manifests, `web.py`, `control.py` and the control room's RBAC (`admin.yaml`). The web pods mount `locale-uk`.
- `locales/`: the four `locale-<country>` ConfigMaps, used by both apps (bank text under `t`, branch text under `b`).
- `branch/`: the branch counter: `branch/base/` and `branch/countries/<cc>` overlays, laid out like the bank's.
- `kustomization.yaml`: the default, UK deployment (just `base/`).
- `countries/us`, `countries/de`, `countries/sim`: overlays that switch the web pods to that country's locale.
- `fleet.yaml`: Fleet settings, including which app and overlay each cluster gets.

## Deploy with Rancher Fleet

Point a GitRepo at this repo with path `dmobnk`, then label each cluster in Rancher (Cluster Management → the cluster → Edit Config → Labels).

| Label | App |
|---|---|
| `dmobnk.smclab.net/role=branch` | The branch counter (`branch/`) |
| no `role` label, or another value | The online bank |

| Label | Language and currency (either app) |
|---|---|
| `dmobnk.smclab.net/country=uk`, or no label | English, £ |
| `dmobnk.smclab.net/country=us` | US English, $ |
| `dmobnk.smclab.net/country=de` | German, € |
| `dmobnk.smclab.net/country=sim` | Simlish, § |

Fleet matches the labels against `fleet.yaml` (`targetCustomizations`, first match wins, branch entries first) and deploys that app and country's overlay. Change a label and Fleet redeploys on its own: adding `role=branch` swaps the online bank for the branch app. `fleet.yaml` also sets `defaultNamespace: dmobnk`, so Fleet creates the namespace.

For the online bank, the target cluster needs ingress-nginx, cert-manager with the `letsencrypt-cloudflare` ClusterIssuer and metrics-server (for the rates autoscaler); its Ingress host is `dev.smclab.net` on every cluster. A branch cluster only needs an ingress controller (see above).

Don't mix Fleet and `deploy.sh` on one cluster: Helm won't adopt resources that `deploy.sh` already created. Delete the namespace first (see below), then let Fleet install it.

## Deploy without Fleet

```sh
kubectl config use-context lenny-new
./dmobnk/deploy.sh               # the online bank, UK; or: ./dmobnk/deploy.sh de   (uk | us | de | sim)
./dmobnk/deploy.sh branch        # the branch counter instead; or: ./dmobnk/deploy.sh branch de
```

The script creates the `dmobnk` namespace, applies the chosen overlay, waits for every rollout and seeds the first rates. Running it again with another country switches the site.

Editing `base/web.py` (or `branch/base/branch.py`) changes the ConfigMap's hash name, so the next deploy rolls those pods.

## Adding a country

1. Add a `locale-<cc>` ConfigMap to `locales/locales.yaml`, with both the `t` (bank) and `b` (branch) text.
2. Copy `countries/de` to `countries/<cc>`, and `branch/countries/de` to `branch/countries/<cc>`, and change `locale-de` in each.
3. Add matching entries to `targetCustomizations` in `fleet.yaml`: `branch-<cc>` with the branch entries, `<cc>` with the bank's.

## Remove

```sh
kubectl delete namespace dmobnk
kubectl delete clusterrole,clusterrolebinding dmobnk-branch-web   # if the branch app was deployed
```

dev.smclab.net's own site (`apps/dev-website`) is a separate Ingress and is left alone.
