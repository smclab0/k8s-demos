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

## Layout

- `base/`: all the manifests, `web.py` and the four `locale-<country>` ConfigMaps. The web pods mount `locale-uk`.
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
