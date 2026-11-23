# DMOBNK · Demo Bank Ldn

A fictional UK bank, in British English and pounds sterling. All the figures are examples and no real money moves.

Pages: **Home** (services, your card at a glance, exchange rates, gold price), **Cards** (compare cards, balance, recent spending, freeze a card), **Mortgages** (repayment calculator and an agreement in principle) and **Rates** (exchange rates, gold price, savings rates).

## How it's built

| Piece | Role |
|---|---|
| `web` | The front end (`web.py`). Its text and number formats come from the `locale-uk` ConfigMap. |
| `accounts-svc`, `cards-svc`, `loans-svc`, `mortgage-svc`, `rates-svc`, `crossborder-svc` | One backend service per banking service, all running `svc.py`. |
| `mortgage-svc` | Runs each agreement-in-principle check as a short Job. |
| `rates-refresh` CronJob | Moves the example rates and gold price every minute. |
| `redis` | Shared state (card frozen, applications, rates). |

## Deploy

```sh
kubectl config use-context lenny-new
./dmobnk/deploy.sh
```

The script creates the `dmobnk` namespace, runs `kubectl apply -k`, waits for every rollout and seeds the first rates.

Editing `web.py` changes the ConfigMap's hash name, so the next deploy rolls the web pods.

Old per-country links (`/dmobnk/de/cards` and so on) redirect to the same page under `/dmobnk/`.

### With Rancher Fleet

Point a GitRepo at this repo with path `dmobnk`. `fleet.yaml` sets `defaultNamespace: dmobnk`, so Fleet creates the namespace itself. The target cluster needs ingress-nginx, cert-manager with the `letsencrypt-cloudflare` ClusterIssuer and metrics-server (for the rates autoscaler).

Don't mix the two methods on one cluster: Helm won't adopt resources that `deploy.sh` already created. Delete the namespace first (see below), then let Fleet install it.

## Remove

```sh
kubectl delete namespace dmobnk
```

dev.smclab.net's own site (`apps/dev-website`) is a separate Ingress and is left alone.
