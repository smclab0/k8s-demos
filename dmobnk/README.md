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

## Layout

- `base/`: all the manifests, `web.py`, `control.py`, the control room's RBAC (`admin.yaml`) and the four `locale-<country>` ConfigMaps. The web pods mount `locale-uk`.
- `kustomization.yaml`: the default, UK deployment (just `base/`).
- `countries/us`, `countries/de`, `countries/sim`: overlays that switch the web pods to that country's locale.
- `fleet.yaml`: Fleet settings, including which overlay each cluster gets.

## Deploy with Rancher Fleet

Point a GitRepo at this repo with path `dmobnk`, then set each cluster's country with a **cluster label** in Rancher (Cluster Management → the cluster → Edit Config → Labels):

| Label | Site |
|---|---|
| `dmobnk.smclab.net/country=uk`, or no label | English, £ |
| `dmobnk.smclab.net/country=us` | US English, $ |
| `dmobnk.smclab.net/country=de` | German, € |
| `dmobnk.smclab.net/country=sim` | Simlish, § |

Fleet matches the label in `fleet.yaml` (`targetCustomizations`) and deploys that country's overlay. Change the label and Fleet redeploys the web pods with the new language and currency on its own. `fleet.yaml` also sets `defaultNamespace: dmobnk`, so Fleet creates the namespace.

The target cluster needs ingress-nginx, cert-manager with the `letsencrypt-cloudflare` ClusterIssuer and metrics-server (for the rates autoscaler). The Ingress host is `dev.smclab.net` on every cluster.

Don't mix Fleet and `deploy.sh` on one cluster: Helm won't adopt resources that `deploy.sh` already created. Delete the namespace first (see below), then let Fleet install it.

## Deploy without Fleet

```sh
kubectl config use-context lenny-new
./dmobnk/deploy.sh        # UK; or: ./dmobnk/deploy.sh de   (uk | us | de | sim)
```

The script creates the `dmobnk` namespace, applies the chosen overlay, waits for every rollout and seeds the first rates. Running it again with another country switches the site.

Editing `base/web.py` changes the ConfigMap's hash name, so the next deploy rolls the web pods.

## Adding a country

1. Add a `locale-<cc>` ConfigMap to `base/locales.yaml`.
2. Copy `countries/de` to `countries/<cc>` and change `locale-de` in it.
3. Add a matching entry to `targetCustomizations` in `fleet.yaml`.

## Remove

```sh
kubectl delete namespace dmobnk
```

dev.smclab.net's own site (`apps/dev-website`) is a separate Ingress and is left alone.
