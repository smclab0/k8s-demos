# DMOSHP

DMOSHP is the retail arm of DMOCRP, the fictional group behind DMOBNK (`../dmobnk`). It has two apps:

- **DMOSHP online** (`shop/`): a web shop with a catalogue, live stock, a basket, checkout and order history (kept in that browser). It's served at `http://shop.<address>.sslip.io/`.
- **DMOSOK** (`sok/`): the self-checkout in a DMOSHP store. Customers scan items by tapping them or typing a barcode, pay by card and get a receipt. It's served at `http://sok.<address>.sslip.io/`. The store name comes from the `name` key of the `dmoshp-sok` ConfigMap (default: London Bridge).

Both sell from the same stock, kept in `shop-redis`, so a self-checkout sale shows up in the online shop's stock within seconds. Every page has a DEMO banner and the "Created By Steve McBride" footer.

## With DMOBNK on the same cluster

- **Card payments.** Paying with the DMOBNK credit card (•••• 4821, the online banking customer's card) goes to DMOBNK's `cards-svc` (`POST /authorise`).
  - The bank declines it while the card is frozen in online banking, or when the payment is over the available credit.
  - Approved payments appear on the card's statement in online banking.
  - Without DMOBNK, that option is greyed out. "Another card" is simulated and always approved.
- **CRM.** With DMOBNK's CRM (`crm=true`), every sale reaches its live feed.
  - A DMOBNK-card sale is also added to that customer's 360 view: activity, a transaction and the card balance.
  - The apps read the CRM's ingest token from `dmobnk/dmobnk-crm` through the API. The `dmoshp-crm-token` ClusterRole allows reading that one Secret by name.
  - To use a CRM on another cluster, create a `dmobnk-crm-link` Secret in `dmoshp`, as you would for DMOBNK.

## Kube Power

`admin=true` adds Kube Power, the same control room as DMOBNK's, for `shop-web` and `sok-web`:

- It's served at `/admin` on the first app's host.
- The password is `dmopwd1`, kept in the `dmoshp-admin` Secret.
- Fleet doesn't set those Deployments' replica counts. Kube Power keeps them at what was last chosen, starting from the `dmobnk.smclab.net/replicas` annotation. With both writing `.spec.replicas`, Fleet's server-side apply would refuse to upgrade.

## Deploying

**With Fleet**, add a GitRepo for this repo with path `dmoshp`. Cluster labels pick the apps (see `fleet.yaml`):

| Label | Adds |
|---|---|
| `shop=true` | the online shop |
| `sok=true` | the self-checkout |
| `admin=true` | Kube Power |

The labels combine freely. A cluster with neither `shop` nor `sok` gets the shop. The list of targets is generated: edit `targets/generate.py` and run it.

**Without Fleet:** `dmoshp/deploy.sh [shop] [sok] [admin]`.

The apps create their own Ingresses (owned by their Deployments) and point them at the cluster, exactly as DMOBNK's apps do. To fix a host or class, set `host` / `ingressClass` in the `dmoshp-shop` or `dmoshp-sok` ConfigMap.

## Layout

- `shop/`, `sok/`: the apps.
- `base/`: `shop-redis`, the shared modules and the cluster-wide read permissions.
- `common/store.py`: the catalogue, stock, orders, payments and page chrome.
- `common/{ingress_host,redis_lite,crm_feed}.py` and `admin/base/{control,kube_power}.py` are **copies** from `../dmobnk`, because Fleet bundles only see their own directory. After changing them there, run `./sync-shared.sh`.
- `admin/<combo>/`, `targets/<combo>/`: generated.
