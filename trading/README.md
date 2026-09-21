# Stock Trading Demo

Built by AI.
**This isn't real and is only intended to load test the cluster** — fictitious tickers, fictitious currency (`§`), fictitious money.
Dashboard exposed at **`http://<metallb-ip>/`** via MetalLB, running in the `apps` namespace.
Also reachable at **`https://trading.<metallb-ip>.sslip.io/`** via the cluster's `traefik` ingress (`ingress.yaml`), using sslip.io's wildcard DNS (`<name>.<ip>.sslip.io` resolves to `<ip>`) so no real DNS entry is needed, with a cert from the cluster's internal CA (`lenny-internal-ca-issuer`) — the same pattern every other app here uses, since public ACME (Let's Encrypt) can't validate a hostname that resolves to a private address.
**Note:** the `caddy` ingress class was tried first per an earlier request, but this cluster's `caddy-ingress-controller` (v0.2.1) has a real bug/limitation — it never serves a manually-supplied (cert-manager) TLS secret, failing the handshake with "no certificate available" for the SNI even with a valid secret in place, and exposes no annotation to disable its automatic HTTPS-redirect to work around it.
Switched to `traefik` instead, which works correctly.

## Deploying

Everything here is one Kustomize app — deploy or update all 21 resources (ServiceAccounts, RBAC, ConfigMaps, Services, Deployments, the CronJob) with:

```
kubectl apply -k deploymenrs/trading/
```

It's safe to re-run any time — `kubectl apply` is idempotent, and none of the Deployments' pod templates or selectors are touched by re-applying, so a re-apply after only editing one file (e.g. `dashboard.yaml`) won't restart unrelated components.
Note: editing a ConfigMap's content and re-applying does **not** by itself restart the pod that mounts it (Kubernetes doesn't hot-reload mounted ConfigMaps into a running process here) — follow up with `kubectl rollout restart deployment/<name> -n apps` for whichever component's script changed.
Recommend a `--dry-run=server` first for anything beyond a routine edit, to catch immutable-field conflicts before they hit the live cluster.

Every resource carries the label `app.kubernetes.io/part-of=stock-trading-demo`, so the whole app can be queried (or torn down) as one unit:

```
kubectl get all,cronjob,sa,role,rolebinding,clusterrole,clusterrolebinding \
  -l app.kubernetes.io/part-of=stock-trading-demo -n apps

kubectl delete all,cronjob,sa,role,rolebinding,configmap \
  -l app.kubernetes.io/part-of=stock-trading-demo -n apps
# ClusterRole/ClusterRoleBinding aren't namespaced -- delete those separately if tearing down for good:
kubectl delete clusterrole,clusterrolebinding -l app.kubernetes.io/part-of=stock-trading-demo
```

## Components

| Component | What it does |
|---|---|
| `redis` | Shared state: live prices, portfolios (cash/positions), trade history, events, history series |
| `price-feed` | Random-walk price simulator for 26 tickers (AAA-ZZZ), updates every 2s, also records per-ticker price history |
| `trading-engine` | Internal HTTP API (`/prices`, `/portfolio?source=user\|bot`, `/trades[?source=&limit=]`, `/events`, `/pending[?source=]`, `POST /order`, `POST /reset`, `POST /market-open`, `POST /market-close`). Orders execute atomically via a Redis Lua script (`ORDER_SCRIPT`) so concurrent orders can't race each other's cash/position updates. The human player and the bot swarm have **separate portfolios** (`portfolio:cash:user` / `portfolio:cash:bot`, same for positions) so they trade the same market independently rather than sharing one pot. Orders placed while the market is closed are queued (`pending_orders`) rather than rejected, and executed in order at the next open. Also snapshots the user's portfolio value every 2s for the history chart. Runs at 3 replicas (see capacity section). |
| `trading-dashboard` | Public page — live prices + sparklines (26 tickers, one merged view), portfolio with P&L vs. starting balance, a "queued for next open" panel when you have pending orders, trade history (all / mine filter, your own orders highlighted, "mine" reads a dedicated longer-retained list), portfolio value chart with hover crosshair, order form (round quantity-preset buttons 5/10/20/30/40/50, buy / sell / sell all / dump and run / reset my portfolio), sticky two-column layout, live connection indicator, market-phase timeline with countdown (segment widths track actual phase duration), and top-right badges showing live bot count, trading-engine replica count, cluster node count, and orders/sec (read from the k8s API and Redis — see RBAC below) |
| `hft-bot` | Swarm of bots firing random buy/sell orders every 20-50ms to simulate high-frequency trading load. Baseline 2 replicas; scaled by the `market-open` CronJob during its cycle (see below) |
| `market-open` (CronJob) | Every 14 minutes, runs a 3-phase session: **open** (8 min: volatility jolt + queued-order drain + `hft-bot` scaled to 30) → **quiet** (3 min: `hft-bot` scaled to 2) → **closed** (3 min: `hft-bot` scaled to 0), then loops. Uses a scoped `market-open` ServiceAccount/Role that can only patch `hft-bot`'s `/scale` subresource. |

Starting balance: §100,000 simulated cash, per portfolio (user and bot each start fresh).
Currency symbol is `§` (not real currency).

### RBAC granted to the dashboard

`trading-dashboard` runs under its own ServiceAccount with two narrow grants, both in `dashboard-rbac.yaml`:
- namespaced `Role` — `get` on the named Deployments `hft-bot` and `trading-engine` (for the bot-count and engine-count badges)
- cluster-scoped `ClusterRole` — `list` on `nodes` (for the node-count badge; nodes aren't namespaced, so this is the one thing here that needs a ClusterRole instead of a Role)

Neither grant allows writing anything.
The `market-open` CronJob has its own separate, equally narrow ServiceAccount that can only patch `hft-bot`'s `/scale` subresource (`market-open-cron.yaml`) — the two service accounts are not related.

### Your own portfolio vs. the bot swarm

Your account (`source=user`) and the `hft-bot` swarm (`source=bot`) are fully separate: cash, positions, and trade history each have their own Redis keys.
They trade against the same shared price feed, but your "sell all" / "dump and run" / P&L only ever reflect your own orders.
Your trades also get a dedicated, longer-retained history list (`trades:user`, last 150) so they don't get flushed out of view by bot volume the way the old shared list did — the "recent trades" panel's **mine** filter reads from that dedicated list, while **all** still shows the shared realtime firehose (last 20, churns fast under bot load, by design).

Use the **reset my portfolio** button (or `POST /reset` with `{"source":"user"}`) to wipe your cash back to §100,000 and clear your own trade history.
It never touches the bot swarm's state.

### Market cycle (every 14 minutes)

```
open (8 min, hft-bot=30) -> quiet (3 min, hft-bot=2) -> closed (3 min, hft-bot=0) -> repeat
```

Orders placed while closed aren't rejected — they queue (`pending_orders` in Redis) and execute in order at the next open, against whatever price results from that open's volatility jolt.
See `GET /pending?source=user` and the dashboard's "queued for next open" panel.

The dashboard's market-status line reflects whichever of `market_open` / `market_close` happened most recently.
`hft-bot`'s replica count set directly via `kubectl scale` (or in `hft-bot.yaml`) is only the *resting* baseline between automated cycles — the CronJob will override it on its own schedule regardless.

### Live-tunable settings

The bot counts per phase (30/2/0), phase durations (8/3/3 min), and the per-bot order rate (50/sec) are all defaults, not fixed — tune them live, no redeploy needed, via `POST /config` on `trading-engine` directly (or the dashboard's `POST /api/config`, which just proxies it).
There used to be an `/admin` page for this; it was removed as unnecessary — the same values are just as easy to read/set with `curl`:

```
curl -sS http://trading-engine.apps.svc.cluster.local/config
curl -sS -X POST http://trading-engine.apps.svc.cluster.local/config \
  -d '{"bot_rate": 3, "open_bots": 50, "quiet_bots": 10}'
```

Everything is stored in one Redis hash (`config`) behind that endpoint:
- **Bot counts** are read by the `market-open` CronJob once at the start of each cycle — a change takes effect at the *next* phase transition, not mid-phase.
- **Phase durations** (`open_minutes`/`quiet_minutes`/`closed_minutes`) are also snapshotted once per cycle by the CronJob, *and* read live (every check, no caching) by both `trading-engine` and `price-feed` to compute the open/quiet/closed boundaries — all three independently derive the same boundaries from the same config rather than duplicating hardcoded numbers, so they can't drift out of sync with each other.
  The CronJob's own *schedule* (how often a new cycle starts, currently `*/14 * * * *`) is a static Kubernetes field this can't rewrite — if the durations no longer sum to close to that, cycles run back-to-back with a gap or get skipped by `concurrencyPolicy: Forbid`, not corrupted.
- **Order rate** is re-read by every running `hft-bot` pod roughly every 10s and applied immediately, no restart required.

Keep in mind the ~200 orders/sec cluster-wide ceiling from the capacity section below — cranking bot count × rate well past that just trades throughput for latency and errors (already true of the default 30×50/sec open-phase target, which is deliberately above the empirical sweet spot).

## Capacity: how many hft-bots can this take?

Initial estimate (12 bots, 1 engine replica) pointed at the Python `trading-engine` process as the bottleneck (1059m CPU vs Redis's 159m).
That was tested empirically on 2026-09-21 with a controlled load generator run from inside the cluster (bypassing the bots' own pacing, hitting `trading-engine`'s ClusterIP service directly at fixed concurrency levels) — **scheduling capacity was ruled out** (20 allocatable CPU cores cluster-wide, 11+ free, nodes never exceeded 48% CPU during any test), and the result was more interesting than the initial estimate:

| Concurrency | trading-engine replicas | req/sec | infra failures | p99 latency |
|---|---|---|---|---|
| 10 | 3 | 107 | 0% | 252ms |
| 30 | 3 | 146 | 0% | 631ms |
| 60 | 3 | 203 | 0.1% | 1453ms |
| 120 | 3 | **216 (peak)** | 1.9% | 2799ms |
| 200 | 3 | 158 (regressed) | 3.3% | 5115ms |
| 120 | 6 | 194 | **0%** | 3002ms |
| 200 | 6 | 191 | 0.8% | 5113ms |

**Redis's single-threaded command processing is the real hard ceiling, not the engine's CPU.**
Doubling `trading-engine` from 3 to 6 replicas barely moved the throughput ceiling (still ~190-220 req/sec) but did cut the error rate at 120 concurrent requesters from 1.9% to 0% — more replicas buy reliability headroom, not a higher ceiling.
Confirmed via `redis-cli SLOWLOG`: a `HINCRBYFLOAT` inside the order script was caught taking 10.5ms during the heavy test (vs. Redis's normal sub-millisecond latency) — a classic sign of commands queuing behind each other, since Redis executes one command (or one Lua script, atomically) at a time regardless of how many client connections are waiting.

**Practical ceiling: ~200 sustained orders/sec**, with latency staying reasonable (p99 under ~1.5s) up to about 60-90 concurrent requesters; push past that and latency and error rate both degrade sharply, and throughput actually *drops* (158 req/sec at 200 concurrent vs 216 at 120 — thrashing, not scaling).

Real `hft-bot` replicas pace themselves (20-50ms sleep between requests, blocking on each response) rather than firing in an open loop like the test generator, so they're gentler per-bot than a raw concurrent worker.
Given the ~200 req/sec ceiling and each bot's natural idle-latency rate of ~15-25 req/sec, **the practical sweet spot is roughly 12-20 `hft-bot` replicas** — meaning the original 12-bot swarm was already close to this architecture's real capacity, not far below it as the CPU numbers alone suggested.
`trading-engine` is kept at 3 replicas (see `trading-engine.yaml`) since it measurably reduces error rate for free; going further only helps once Redis itself is scaled (e.g. Redis Cluster, or splitting hot keys across multiple instances) — out of scope for this demo.

Note: the `market-open` CronJob's **open** phase now scales `hft-bot` to 30, intentionally above this empirical sweet spot — expect a higher (though still bounded, per the 6-replica numbers above) error/latency rate during the first part of each open phase versus the earlier 20-bot cycle.

## Operational incidents

**2026-09-21: `market-open` CronJob failure from self-inflicted overload.**
A load test pushed the bot swarm to 50 replicas × 200 orders/sec each (~660+ combined req/sec) — well past the ~200 req/sec Redis ceiling documented above.
`trading-engine` slowed enough under that backlog that the CronJob's own `curl -X POST .../market-open` call (which had a hardcoded 10s timeout) exceeded it, failed, retried once (`backoffLimit: 1`), failed again, and hit `BackoffLimitExceeded` — leaving the job `Failed` and `hft-bot` stranded at 0 replicas with no active job to correct it until the next scheduled tick.

Root cause confirmed from the job's own log (`curl: (28) Operation timed out after 10002 milliseconds with 0 bytes received`) plus `trading-engine` CPU/latency readings at the time, cross-checked against a stable `orders_per_sec` reading (via `redis-cli INFO commandstats` delta) that matched the overload window.

**Fix** (`market-open-cron.yaml`): every `curl` call in the CronJob script (`/config`, `/market-open`, `/market-close`) now uses `-m 60` instead of `-m 10` — a 14-minute cycle budget easily absorbs a slow response instead of the job dying to a tight timeout — and `backoffLimit` went from `1` to `2` for extra headroom against transient blips.
Recovery from the stuck state itself was a one-off manual `kubectl create job --from=cronjob/market-open`, same as the command below.

Standing mitigation: don't run bot-count × order-rate combinations that push sustained load meaningfully past the ~200 req/sec ceiling (see Capacity section) — that's what triggers the overload in the first place.
The live-tuned settings at the time of writing (50 open-phase bots × 3 orders/sec ≈ 150 req/sec) sit at roughly 75% of the ceiling for exactly this reason.

## Useful commands

```
# stop the bot swarm (note: the market-open CronJob will override this on its own schedule)
kubectl scale deployment/hft-bot -n apps --replicas=0

# resume it at the baseline
kubectl scale deployment/hft-bot -n apps --replicas=2

# manually run a full open/quiet/closed market cycle right now (normally every 14 min)
kubectl create job market-open-manual --from=cronjob/market-open -n apps

# reset just your own portfolio (cash/positions/trade history) -- same as the dashboard button
curl -sS -X POST http://<metallb-ip>/reset

# wipe ALL trading state for everyone (prices, both portfolios, trades, events, history)
kubectl exec -n apps deploy/redis -- redis-cli FLUSHALL
```
