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
| `kube-power` (only with `admin=true`) | **Kube Power**, the admin-only control room at `/dmobnk/admin`. See below. |

## Kube Power control room (admins only)

Kube Power is deployed only to clusters labelled **`admin=true`**, next to whatever else they run: on the online bank at `/dmobnk/admin` (linked as **Admin** in the bank's footer), on a kiosk or CRM cluster at `/admin` on the kiosk's (or CRM's) host. Other clusters don't run it at all: no pod, no permissions, no link. `dev.smclab.net` (`sites/dev-smclab`) always has it.

After you sign in with the admin password, it shows the `dmobnk` namespace live: pods on each node, the deployments and recent events (and, on the bank, the rates autoscaler and rates CronJob). You can also:

| Control | What it shows |
|---|---|
| **Scale out** | Set a deployment's replicas (1 to 6). Not `rates-svc`, which its autoscaler sizes. |
| **Kill a pod** | Self-healing: a pod is deleted and its ReplicaSet replaces it. It only kills where another ready pod keeps the service up. |
| **Rolling restart** | Zero-downtime rollout: every pod of a deployment is replaced, a few at a time. |
| **Traffic surge** (bank only) | Autoscaling: a Job of 4 curl pods loads `rates-svc` for 90 s, and the HPA adds pods, then removes them. |

On the bank it controls the bank's services; on a kiosk `branch-web`, on a CRM `crm-web` (or both, where they run side by side). Redis is never offered, since it holds the app's state.

It runs as its own small `kube-power` Deployment (`admin/base/kube_power.py` serving `control.py`), with its own Ingress on the app's host. Its `kube-power` Role (`admin/<app>/role.yaml`) only covers the `dmobnk` namespace and can only change those deployments, so the public web pods hold no such permissions.

**The password** is the `password` key of the `dmobnk-admin` Secret, never kept in git. If the Secret doesn't exist, Kube Power creates it with a random password on its first start. Read it with:

```sh
kubectl -n dmobnk get secret dmobnk-admin -o jsonpath='{.data.password}' | base64 -d
```

To choose your own, replace the Secret and restart: `kubectl -n dmobnk create secret generic dmobnk-admin --from-literal=password='choose-one' --dry-run=client -o yaml | kubectl apply -f - && kubectl -n dmobnk rollout restart deploy/kube-power`. Fleet doesn't own the Secret, so it survives Fleet upgrades and reinstalls; it goes only when the namespace is deleted.

A sign-in lasts 8 hours. Five wrong passwords from one address lock it out for 5 minutes. If Fleet's drift correction is on, it will undo scaling done from the control room.

## Branch kiosk (`branch=true`)

A cluster labelled `branch=true` gets the **branch kiosk** *instead of* the online bank: a full-screen, touch-friendly self-service kiosk for one high-street branch, running on the branch's own (edge) cluster. There are no staff screens.

| Without a card | With a (demo) card and PIN |
|---|---|
| **Take a queue ticket** for cash, accounts or mortgages, with the wait ahead. The queue moves on by itself, one ticket every 45 s, as if advisers were serving; the home screen shows who's being served. | **Balance and statement**, with a printed-style mini statement. |
| **Book an appointment** with an adviser: topic, today or the next two days, a free time and a first name. | **Withdraw cash** (in whole notes, up to a daily limit, and only while the kiosk has the cash) and **pay in cash**. |
| **Opening hours**, with today highlighted and whether it's open now. | **Transfer** between your own accounts and **pay a bill** to a saved payee. Every transaction prints a receipt. |

Tap one of three demo cards and enter PIN `1234` (shown on screen). Three wrong PINs lock that card for a minute, and a signed-in session ends after 45 s without a touch (with a 15 s warning). The demo customers, their accounts and payees, the cash limits and the opening hours are per country, in the `kiosk` section of each `locale-<country>` ConfigMap; balances live in `branch-redis` and start again if it restarts.

It's `branch-web` (`branch/base/branch.py`, 2 replicas) plus `branch-redis`. The language, currency and branch name follow the cluster's `dmobnk.smclab.net/country` label, the same as the online bank. All customers and figures are examples.

**Where it's served.** A kustomize bundle can't read a cluster label's value, so `branch-web` sets its own Ingress (plain HTTP) once it's running:
- host: `branch.<external address>.sslip.io`. The address is the cluster's **external ingress address** when it has one: what the ingress controller wrote into the Ingress's status, or the `EXTERNAL-IP` of the ingress controller's LoadBalancer Service (any LoadBalancer Service on port 80 or 443). An AWS load balancer publishes a DNS name, which is resolved to an IP. A public address wins. Without one, it falls back to the node: its ExternalIP, then on EC2 the instance's public IP (instance metadata, then `checkip.amazonaws.com`). On-prem with no public address, it uses the load balancer's private address (e.g. MetalLB), else the node's InternalIP. It's checked again every 30 s while it only has a node address, then every 5 minutes.
- class: the cluster's default IngressClass, else its only one, else `traefik` or `nginx`.

To see what it picked: `kubectl -n dmobnk logs deploy/branch-web | grep -E 'using|ingress'`.

**On AWS EC2:** the instances' security group must allow inbound TCP 80 to the node(s) running the ingress controller, since the sslip.io name points straight at the node when there's no load balancer. With an AWS load balancer in front of the ingress controller, its address is used, but its IPs can change: for a fixed name, set `host` in the `dmobnk-branch` ConfigMap to the load balancer's DNS name instead.

To override either, or the branch's name, create a `dmobnk-branch` ConfigMap on that cluster, then restart `branch-web`:

```sh
kubectl -n dmobnk create configmap dmobnk-branch \
  --from-literal=host=branch.example.com --from-literal=ingressClass=nginx --from-literal=name='Canary Wharf branch'
kubectl -n dmobnk rollout restart deploy/branch-web
```


## CRM (`crm=true`)

A cluster labelled `crm=true` gets the **CRM**, the bank staff's customer and sales app, at `http://crm.<external address>.sslip.io/`. With `branch=true` as well, it runs next to the kiosk; otherwise it replaces the online bank.

| Page | What it does |
|---|---|
| **Dashboard** | Customers, open pipeline, won this month and tasks due, a live feed of events from linked banks and kiosks, your own tasks and the pipeline by stage. |
| **Customers** | Search by name, email, phone or city; filter by segment (Everyday, Premier, Business, Prospects). |
| **Customer (360)** | Details and owner, accounts, card and loans, recent transactions, opportunities, tasks, and a timeline where you log notes, calls, visits and emails. |
| **Pipeline** | Opportunities by stage (New, Contacted, Application, Approved, Won, Lost): drag a card or use its buttons. Open value, weighted forecast and win rate. |
| **Tasks** | Follow-ups with an owner and due date: yours, all open, overdue, done. |
| **Connect** | How to link a bank or kiosk cluster, and which ones have sent events. |

The 40 example customers (including the kiosk's three demo customers, with the same accounts), staff, pipeline and history are made up per country; the screens are in English. Everything lives in `crm-redis`, so a restart of it starts again from the example data.

**Sign-in:** pick who you are (one of four demo staff, so notes and tasks show who did them) and enter the password from the `dmobnk-crm` Secret. The CRM creates that Secret with a random password, and the token linked apps use, on its first start:

```sh
kubectl -n dmobnk get secret dmobnk-crm -o jsonpath='{.data.password}' | base64 -d
```

**Live from the bank and kiosks.** The online bank and kiosks run on other clusters. To link one, create a `dmobnk-crm-link` Secret on it (the CRM's **Connect** page shows the exact command, with its URL and token). It's mounted as an optional volume, so it takes effect within a minute or so, without a restart; without it, nothing is sent. Then:

| Event | In the CRM |
|---|---|
| Kiosk sign-in, cash withdrawal or deposit, transfer, bill payment | On the customer's timeline, their transactions and their balances. |
| Kiosk queue ticket | In the live feed. |
| Kiosk appointment booking | A new prospect, an opportunity for the topic (Mortgages → mortgage) owned by the right adviser, and a task for the appointment. |
| Online mortgage application | The customer's mortgage opportunity moves to Application (or a new one), plus a follow-up task for the mortgage adviser. |
| Online card freeze | A task to call the customer. |

The online bank's signed-in customer is the country's first demo customer (Priya Shah in the UK). The sending side is `common/crm_feed.py`; it never slows the apps down, and drops events if the CRM can't be reached.

## Layout

- `base/`: the online bank's manifests and `web.py`. The web pods mount `locale-uk`.
- `admin/`: Kube Power. `admin/base/` is the Deployment, Service, Ingress and code; `admin/<app>/` are Components that set what it controls.
- `crm/`: the CRM. `crm/app/` is the app on its own; `crm/base/` adds the shared locales and modules.
- `targets/<combo>/<cc>`: what each combination of labels and country gets, used by Fleet and `deploy.sh`. Written by `targets/generate.py`, with `fleet.yaml`'s target list, `admin/<app>/` and `locales/country/<cc>/`: edit the script and rerun it rather than editing its output.
- `common/`: Python modules the apps share: `ingress_host.py` (points an Ingress at the cluster), `redis_lite.py` (a tiny Redis client) and `crm_feed.py` (sends events to a linked CRM).
- `sites/dev-smclab/`: lenny-new's `dev.smclab.net` Ingress, as a component plus one overlay per country (used by `deploy.sh`).
- `locales/`: the four `locale-<country>` ConfigMaps, used by both apps (bank text under `t`, branch text under `b`).
- `branch/`: the branch kiosk (`branch/base/`).
- `kustomization.yaml`: the default, UK deployment (just `base/`).
- `countries/us`, `countries/de`, `countries/sim`: overlays that switch the bank's web pods to that country's locale (used by `sites/dev-smclab`).
- `fleet.yaml`: Fleet settings, including which app and overlay each cluster gets.

## Deploy with Rancher Fleet

Point a GitRepo at this repo with path `dmobnk`, then label each cluster in Rancher (Cluster Management → the cluster → Edit Config → Labels).

| Labels (each set to `true`) | What the cluster gets |
|---|---|
| none | The online bank |
| `branch` | The branch kiosk instead |
| `crm` | The CRM instead |
| `branch` and `crm` | The kiosk and the CRM side by side |
| `admin`, with any of the above | Adds Kube Power |

Only the value `true` counts: `branch=false` (or any other value) is the same as no label.

| Label | Language and currency (either app) |
|---|---|
| `dmobnk.smclab.net/country=uk`, or no label | English, £ |
| `dmobnk.smclab.net/country=us` | US English, $ |
| `dmobnk.smclab.net/country=de` | German, € |
| `dmobnk.smclab.net/country=sim` | Simlish, § |

Fleet matches the labels against `fleet.yaml` (`targetCustomizations`, first match wins, most specific first) and deploys that combination's overlay in `targets/`. Change a label and Fleet redeploys on its own: adding `branch=true` swaps the online bank for the kiosk. `fleet.yaml` also sets `defaultNamespace: dmobnk`, so Fleet creates the namespace.

A Fleet cluster only needs an ingress controller (any: traefik, nginx…) and, for the rates autoscaler, metrics-server. Each app points its own Ingress at the cluster they land on (`common/ingress_host.py`), using its default IngressClass and an sslip.io host built from the cluster's external address, the same way for all of them (see *Where it's served* above):

| App | URL on a Fleet cluster |
|---|---|
| Online bank | `http://dmobnk.<external address>.sslip.io/dmobnk/` (with `admin=true`, Kube Power at `…/dmobnk/admin`) |
| Branch kiosk | `http://branch.<external address>.sslip.io/` (with `admin=true`, Kube Power at `…/admin`) |
| CRM | `http://crm.<external address>.sslip.io/` (with `admin=true` and no kiosk, Kube Power at `…/admin`) |

To fix the online bank's host or class instead, create a `dmobnk-site` ConfigMap (keys `host`, `ingressClass`) and restart `deploy/web`. To see what was picked: `kubectl -n dmobnk logs deploy/web | grep -E 'using|ingress'`.

Don't mix Fleet and `deploy.sh` on one cluster: Helm won't adopt resources that `deploy.sh` already created. Delete the namespace first (see below), then let Fleet install it.

## Deploy without Fleet

```sh
kubectl config use-context lenny-new
./dmobnk/deploy.sh                    # the online bank, UK; or: ./dmobnk/deploy.sh de   (uk | us | de | sim)
./dmobnk/deploy.sh branch             # the branch kiosk instead
./dmobnk/deploy.sh crm                # the CRM instead
./dmobnk/deploy.sh branch crm admin   # the same words as the labels, in any order
```

The script creates the `dmobnk` namespace, applies the overlay for those words (`targets/<combo>/<cc>`), waits for every rollout, seeds the first rates and prints each app's URL. For the online bank it uses `sites/dev-smclab/<cc>`, which serves it at `https://dev.smclab.net/dmobnk/` (ingress-nginx, cert-manager's `letsencrypt-cloudflare`); run it with `DMOBNK_SITE=auto` for the self-configuring sslip.io Ingress instead. The dev.smclab.net site always includes Kube Power; with `DMOBNK_SITE=auto`, only `admin` adds it. It also prints how to read the CRM's and Kube Power's passwords. Running it again with another country switches the site.

Editing `base/web.py` (or `branch/base/branch.py`, `crm/app/crm.py`) changes the ConfigMap's hash name, so the next deploy rolls those pods.

## Adding a country

1. Add a `locale-<cc>` ConfigMap to `locales/locales.yaml`, with the `t` (bank) and `b` and `kiosk` (branch) sections.
2. Add the country to `COUNTRIES` in `targets/generate.py` and run it; add its example people to `DATA` in `crm/app/crm.py`.
3. Copy `countries/de` to `countries/<cc>` and `sites/dev-smclab/de` to `sites/dev-smclab/<cc>`, and change `de` in each.

## Remove

```sh
kubectl delete namespace dmobnk
kubectl delete clusterrole,clusterrolebinding dmobnk-web dmobnk-branch-web dmobnk-crm-web dmobnk-kube-power
```

dev.smclab.net's own site (`apps/dev-website`) is a separate Ingress and is left alone.
