# DMOBNK · Demo Bank Ldn

A fictional bank, available in four countries: UK (£), US ($), Germany (€) and the Sims (Simlish, §). Choosing a country switches the language, currency and number format. All the figures are examples and no real money moves.

Pages: **Home** (services, your card at a glance, exchange rates, gold price), **Cards** (compare cards, balance, recent spending, freeze a card), **Mortgages** (repayment calculator and an agreement in principle) and **Rates** (exchange rates, gold price, savings rates).

## How it's built

| Piece | Role |
|---|---|
| `web-uk`, `web-us`, `web-de`, `web-sim` | The same `web.py` four times, each mounting its own `locale-<country>` ConfigMap. |
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

## Adding a country

1. Add a `locale-<cc>` ConfigMap to `locales.yaml`.
2. Add a `web-<cc>` Deployment and Service to `web.yaml`.
3. Add an Ingress path to `ingress.yaml`.
4. Add the country to the `COUNTRIES` list in `web.py`.

## Remove

```sh
kubectl delete namespace dmobnk
```

dev.smclab.net's own site (`apps/dev-website`) is a separate Ingress and is left alone.
