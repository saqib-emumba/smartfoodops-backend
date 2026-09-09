# SmartFoodOps — Backend (Weeks 1–3)

A containerised, eight-FastAPI-service food-ordering backend fronted by an Nginx API
gateway, with the order lifecycle driven by a durable Temporal workflow that runs in its own
deployable — the Orchestrator Service and its worker (D36), sharing neither image nor
database with any other service. Since Week 3, every state change an order goes through is
also published to Kafka through a transactional outbox (D39), read by an independent
Analytics read-model and a Notification pipeline that dispatches simulated SMS/email over
Celery — and the whole platform is traced end to end with OpenTelemetry into Jaeger, and
scraped by Prometheus into Grafana dashboards. Everything runs locally through Docker
Compose: seven PostgreSQL databases, Redis, the Temporal dev server, Kafka, Schema Registry,
RabbitMQ, Jaeger, Prometheus, Grafana, the gateway, eight FastAPI services and four
background workers (the saga worker, two notification processes and a one-shot topic-init
job) — 27 containers in total.

---

## Architecture

**Database-per-service.** Every service has its own physical database with its own
credentials, so no service can read another's tables even by mistake — the connection it
would need does not exist.

```
                          ┌───────────────────────┐
     http://localhost:80  │   Nginx API Gateway   │
     ────────────────────▶│  (path-based routing) │
                          └───────────┬───────────┘
   ┌──────────┬──────────┬────────────┴───┬──────────┬──────────┐
   ▼          ▼          ▼                ▼          ▼          ▼
┌────────┐┌────────┐┌────────┐      ┌────────┐┌────────┐┌────────┐
│  User  ││Restaur.││  Menu  │      │ Order  ││Payment ││ Rider  │
│ :8001  ││ :8002  ││ :8003  │      │ :8004  ││ :8005  ││ :8006  │
└───┬────┘└───┬────┘└───┬────┘      └───┬────┘└───┬────┘└───┬────┘
    │         │      ┌──┴──┐            │         │         │
    ▼         ▼      ▼     ▼            ▼         ▼         ▼
 Postgres  Postgres  PG  Redis       Postgres  Postgres  Postgres
  :5432     :5433  :5436 :6379        :5434     :5435     :5437
                  (menus)(cache)   (+ tracking)         (fleet)
                                        │▲ gRPC (start/signal, D47)
                                  HTTP  ││ + HTTP, X-Internal-Key
                                        ▼│  (transition / read — D36)
                          ┌──────────────────────┐
                          │ orchestrator-service │  :8007 — no database
                          └─────────┬────────────┘
                                    │▲ gRPC
                              gRPC  ││
                                    ▼│
                          ┌─────────────────────────┐
                          │   Temporal dev server    │
                          │   :7233 gRPC  :8233 UI   │
                          │   :9233 /metrics         │
                          └─────────────┬─────────────┘
                                        │ polls "order-tasks"
                              ┌─────────┴──────────┐
                              │ orchestrator-worker │  ← runs the saga,
                              └─────────────────────┘    no database either
```

Arrows between services are **HTTP calls, not shared tables**. Each service owns its data:

| Service | Port | Owns | Its database (host port) | Reaches out to |
|---|---|---|---|---|
| `user-service` | 8001 | `roles`, `users` | `sfo_user_core` @ `sfo-user-db` (5432) | — |
| `restaurant-service` | 8002 | `restaurants` | `sfo_restaurant_core` @ `sfo-restaurant-db` (5433) | User Service (owner check) |
| `menu-service` | 8003 | `menus` | `sfo_menu_core` @ `sfo-menu-db` (5436), cached in Redis DB 0 | Restaurant Service (active check) |
| `order-service` | 8004 | `orders` (incl. the kitchen queue), `order_tracking_logs` | `sfo_order_core` @ `sfo-order-db` (5434) | Menu Service (pricing), User + Restaurant Services (participant + ownership checks), Temporal (starts the saga, signals the kitchen's decision — D47) |
| `payment-service` | 8005 | `payments` | `sfo_payment_core` @ `sfo-payment-db` (5435) | Order Service (order + amount check) |
| `rider-service` | 8006 | `riders` | `sfo_rider_core` @ `sfo-rider-db` (5437) | User Service (role check), Order Service (records the pickup/delivery stage), Temporal (signals the saga — D47) |
| `orchestrator-service` | 8007 | nothing — no database at all | — | Temporal only; health and `/metrics` alone since D47, and the gateway proxies just its `/health` |
| `orchestrator-worker` | — | nothing — no database either | — | Payment, Rider **and Order** Services (D36) — the saga still does not call the Restaurant Service at all (D32) |
| `analytics-service` | 8008 | `processed_events`, `order_projections` | `sfo_analytics_core` @ `sfo-analytics-db` (5438) | Kafka only — no sibling calls, no JWT keys (D44) |
| `notification-consumer` | — | nothing — no database | — | Kafka (reads), User Service (resolves contact details, D45), RabbitMQ (enqueues) |
| `notification-worker` | — | nothing — no database | — | RabbitMQ only — no sibling calls, no JWT keys |

Rules the code enforces deliberately:

- The Restaurant Service never reads the `users` table — it calls `GET /api/v1/users/{id}`.
- The Order Service never reads the `menus` table — it calls `GET /api/v1/menus/{id}`.
- The Payment Service never reads the `orders` table — it calls `GET /api/v1/orders/{id}`.
- The Rider Service never reads the `users` table — it calls `GET /api/v1/users/{id}`.
- No service holds credentials for a database it does not own — including the Orchestrator
  Service and its worker, which hold none for any database (D36).
- The kitchen's queue is a query over `orders`, not a table of its own. A restaurant
  admin reads and decides it on the Order Service; whether they *own* that restaurant is
  still resolved against the Restaurant Service over HTTP (D32).
- Four processes hold a Temporal client (D47): `order-service` starts the saga and signals
  the kitchen's decision, `rider-service` signals pickups and deliveries, `orchestrator-worker`
  runs the workflows, and `orchestrator-service` uses one only for its health probe. Each
  names workflows and signals by **string**, so no service outside `services/orchestrator/`
  imports a workflow or activity module — that import edge is what D36's HTTP facade existed
  to prevent, and it is asserted in the smoke test rather than assumed.
- **Kafka carries facts; Temporal still owns every decision (D38).** Nothing on the
  checkout path or inside the saga waits on Kafka, the Schema Registry, RabbitMQ or Celery —
  every one of those can be stopped and checkout still completes, because each producer
  writes to its own outbox table first and a background relay is what reaches the broker,
  never the request itself. The Orchestrator Service holds no Kafka client at all: it has no
  outbox of its own, and a `@workflow.defn` may not do I/O in the first place.
- The Analytics and Notification Services never write to `orders`, `payments`, or any table
  another service owns. Both are pure consumers of the same Kafka topic, under their own
  consumer group, and could be stopped for a day without another service noticing.

### The order saga

`POST /api/v1/orders` still does everything it did in Week 1 — re-prices the cart, verifies
the customer and restaurant over HTTP, and commits the order with the opening entry of its
audit trail in one transaction. It then starts a workflow whose id is derived from the order
id, so starting one twice is a no-op.

```
created ──payment authorised──▶ confirmed  (= on the kitchen's rail;
                                    │       entering it claims a capacity slot,
                                    │       checked in the same transaction)
                                    │  durable timer (120s)
                    ┌───────────────┴───────────────┐
              accepted                   rejected / silence / at capacity
                    │                               │
          rider search (6 × 10s)                    │
                    │                               │
       ┌────────────┴────────────┐                  │
   assigned                 nobody free             │
       │                         │                  │
 rider signals               ────┴──────────────────┴────▶ COMPENSATE
 picked_up ──▶ delivered                                   refund payment
       │                                                   release rider
 release rider                                             ──▶ cancelled
                                                    (the capacity slot frees
                                                     itself: a cancelled order
                                                     is no longer on the rail)
```

Every step is idempotent and every wait is a durable timer, so the worker can be killed
mid-saga and the order still completes. `scripts/saga-resilience-test.sh` asserts exactly
that. The full design, and the ten places the original Week 2 blueprint was wrong, are in
[readme/week2-temporal-orchestration-blueprint.md](readme/week2-temporal-orchestration-blueprint.md);
D25–D31 in the decision record cover what shipped and why.

**MongoDB is gone.** The `menus` collection became a JSONB table in the Menu Service's own
Postgres database, read through a Redis cache-aside layer; `order_tracking_logs` became a
relational table in the Order Service's database, where it can hold a real foreign key
against the order it describes. See
[readme/postgres-menu-tracking-migration-v2.md](readme/postgres-menu-tracking-migration-v2.md)
for the blueprint and D22–D24 in the decision record for what shipped and why.

**Why any of this is the way it is** —
[readme/key-decisions.md](readme/key-decisions.md) is the decision record: each entry gives
the choice, the alternative it was taken over, and what it costs. Start there when the
question is "why not the other way?".

**Why payments are their own service** — [readme/payments-service-migration.md](readme/payments-service-migration.md)
has the full rationale; in short, card handling is the one part of the platform worth
isolating for its own sake. The compliance boundary shrinks to one container and one
database, and an outage at the card gateway can no longer starve the threads that place,
read and track orders. It also gives Week 2's Temporal saga two independently compensatable
activities instead of one transaction spanning both concerns.

### Eventing and observability

Week 3 added a second data path beside the synchronous one above: every write to `orders`
that changes something (a new order, a transition, a kitchen decision) and every write to
`payments` also inserts a row into that service's own `order_outbox` / `payment_outbox`
table, in the **same transaction** as the write it describes. Nothing else about checkout or
the saga changed — this is the same argument D24 already made for the audit trail, applied
again: a write that already succeeded must not be reported as a failure just because a
second, unrelated system is unreachable.

```
  order-service            payment-service
  ┌────────────┐           ┌──────────────┐
  │   orders   │           │   payments   │
  │(+ outbox row,          │ (+ outbox row,
  │  same txn) │           │   same txn)  │
  └─────┬──────┘           └──────┬───────┘
        │ background relay        │ background relay
        │ (polls unpublished       │ (same shape)
        │  rows, FOR UPDATE         │
        │  SKIP LOCKED)            │
        ▼                          ▼
  ┌─────────────────────────────────────────┐
  │     Kafka — sfo.order.events.v1           │   3 partitions, keyed by order_id, so one
  │     (schema-checked against the           │   order's events always land in one
  │      Schema Registry before send)         │   partition and are never reordered
  └───────────────┬─────────────┬─────────────┘
                   │             │
      consumer group "analytics" │  consumer group "notification"
                   ▼             ▼
        ┌─────────────────┐   ┌───────────────────────┐
        │ Analytics Service │  │ Notification Consumer │
        │  :8008             │  │ (order.confirmed/     │
        │  sfo_analytics_core │  │  delivered/cancelled  │
        │  (dedup + KPIs)      │  │  only — resolves      │
        └─────────────────┘   │  contact details, then │
                                │  enqueues a task)      │
                                └───────────┬────────────┘
                                            ▼
                                    RabbitMQ ──▶ Notification Worker
                                   (Celery broker)  (simulated SMS/email,
                                                      time.sleep for latency)
```

Every service is also instrumented with OpenTelemetry: a request's trace follows it through
every HTTP hop, into Postgres, across the Temporal boundary (via
`temporalio.contrib.opentelemetry.TracingInterceptor` on both Temporal clients), and — via a
`traceparent` captured at the moment the outbox row is written and carried as a Kafka message
header — into whichever consumer picks the event up. The whole chain is one trace in Jaeger,
not eight disconnected ones. Every service also exposes `/metrics` in Prometheus's own
format (not the D35 envelope — see D41), scraped by Prometheus and rendered in Grafana.

The rule that makes all of this safe to add: **Kafka carries facts, Temporal still owns every
decision (D38).** No consumer signals the workflow, writes to `orders`, or sits on the
checkout path — proved directly by running the full smoke suite twice, once with Kafka
running and once with the Kafka container stopped, both green. If the whole eventing stack
(Kafka, Schema Registry, RabbitMQ, both Celery processes, Analytics) were deleted tomorrow,
checkout and the saga would not notice; only the outbox tables would grow unread.

Full reasoning: D38–D45 in [readme/key-decisions.md](readme/key-decisions.md).

### References that cross a database boundary

A foreign key cannot span two physical databases, so a column pointing at another service's
table (`restaurants.owner_id`, `orders.customer_id`, `orders.restaurant_id`,
`orders.rider_id`, `payments.order_id`) is a plain `UUID`. The engine no longer validates it;
the owning service does, over HTTP, immediately before the write:

| Write | Verified by | Rejects with |
|---|---|---|
| `POST /api/v1/restaurants/onboard` | User Service — owner exists **and** is a `restaurant_admin` | `404` / `403` |
| `POST /api/v1/orders` | User Service — customer exists | `422` |
| `POST /api/v1/orders` | Restaurant Service — restaurant exists | `422` |
| `POST /api/v1/payments` | Order Service — order exists **and** its total equals the amount | `422` |

Those status codes are unchanged from the single-database version, where the same failures
arrived as foreign-key violations.

One reference keeps a real foreign key, because both of its ends live in one database:
`order_tracking_logs.order_id`, which is why that table moved out of MongoDB (D24).
`order_tickets.restaurant_id` was a second until D32 deleted that table, and
`riders.user_id` a third until the fleet moved into its own database (D28) — both are now
checked references like the rest.
`payments.order_id` lost its key when the Payment Service split out, which is why
`GET /api/v1/orders/{order_id}` exists at all — an HTTP call is what replaced that
constraint.

The trade-off is eventual, not immediate, integrity: a user deleted between verification and
insert leaves an order pointing at nobody. A single Postgres instance was hiding that
problem, not solving it.

---

## Prerequisites

- Docker Desktop (Compose v2) — `docker compose version`
- `curl` and `python3` for the smoke tests below
- Ports free on the host: **80** (gateway), **5432–5438** (seven Postgres), **6379** (Redis),
  **7233 / 8233 / 9233** (Temporal gRPC / UI / metrics), **16686 / 4317 / 4318** (Jaeger UI /
  OTLP gRPC / OTLP HTTP), **9090** (Prometheus), **3000** (Grafana), **9092** (Kafka),
  **8081** (Schema Registry), **5672 / 15672 / 15692** (RabbitMQ AMQP / management UI /
  Prometheus metrics)
- A `.env` file at the repo root — see below, it is not committed

---

## Environment file

`.env` is listed in [.gitignore](.gitignore) and is **not** committed, so a fresh clone will
not have one. Create it at the repo root before your first run:

```bash
cat > .env <<'EOF'
# Database Credentials — one password per physical database (database-per-service).
# Pick your own values; these initialise the databases and build the service DSNs.
USER_POSTGRES_PASSWORD=<choose one>
RESTAURANT_POSTGRES_PASSWORD=<choose one>
ORDER_POSTGRES_PASSWORD=<choose one>
PAYMENT_POSTGRES_PASSWORD=<choose one>
MENU_POSTGRES_PASSWORD=<choose one>
RIDER_POSTGRES_PASSWORD=<choose one>
ANALYTICS_POSTGRES_PASSWORD=<choose one>

REDIS_URL=redis://cache-redis:6379/0

# Week 3 — the Grafana dashboard admin account and Celery's RabbitMQ broker credentials.
# Never defaulted in code (same rule as every password above): a missing value aborts the
# stack rather than falling back to a checked-in password.
GRAFANA_ADMIN_PASSWORD=<choose one>
RABBITMQ_USER=<choose one>
RABBITMQ_PASSWORD=<choose one>

# Service endpoints (within the Docker network)
USER_SERVICE_URL=http://user-service:8001
RESTAURANT_SERVICE_URL=http://restaurant-service:8002
MENU_SERVICE_URL=http://menu-service:8003
ORDER_SERVICE_URL=http://order-service:8004
PAYMENT_SERVICE_URL=http://payment-service:8005
RIDER_SERVICE_URL=http://rider-service:8006

# Workflow orchestrator (gRPC, so no scheme). docker-compose.yml also sets this
# per-container; it is here for scripts and for a worker run outside Compose.
TEMPORAL_ADDRESS=temporal-server:7233
EOF

# Access token signing (RS256) plus the internal service key. Generated, never chosen:
# the private key is the ability to mint any identity, so it must not be a memorable string.
priv=$(openssl genrsa 2048 2>/dev/null)
cat >> .env <<EOF

JWT_PRIVATE_KEY_B64=$(printf '%s' "$priv" | openssl base64 -A)
JWT_PUBLIC_KEY_B64=$(printf '%s' "$priv" | openssl rsa -pubout 2>/dev/null | openssl base64 -A)
INTERNAL_API_KEY=$(openssl rand -hex 32)
EOF
```

`scripts/init_bootstrap.sh` writes all of this for you, keypair included; the block above is
the manual equivalent for an existing checkout.

`.env` is the **single source of truth** for credentials, and the stack no longer boots
without it. Each password appears in exactly one place in `docker-compose.yml`: a YAML
anchor that builds that service's `DATABASE_URL` and is merged into both the database
container and the service. No credential appears in a committed file.

Database and role names (`sfo_user_core` / `sfo_user_admin`, and so on) are **not** secrets,
so they stay literal in `docker-compose.yml` rather than being threaded through `.env` —
there is nothing to keep in sync by hand, and the file reads as documentation of which
service talks to which database.

| Key | Required | Notes |
|---|---|---|
| `USER_POSTGRES_PASSWORD`, `RESTAURANT_POSTGRES_PASSWORD`, `ORDER_POSTGRES_PASSWORD`, `PAYMENT_POSTGRES_PASSWORD`, `MENU_POSTGRES_PASSWORD`, `RIDER_POSTGRES_PASSWORD`, `ANALYTICS_POSTGRES_PASSWORD` | yes | One per database. A missing key aborts **every** compose command with `set <KEY> in the root .env` |
| `GRAFANA_ADMIN_PASSWORD` | yes | Grafana's own admin login, not a database password |
| `RABBITMQ_USER`, `RABBITMQ_PASSWORD` | yes | Celery's broker credentials (Week 3, D45) — required even though Kafka, not RabbitMQ, is the durable ledger |
| `*_SERVICE_URL` | no | Compose sets these explicitly per service; the copies here are for the host-run flow below |

Nothing falls back to a baked-in password, at either layer. Compose refuses to start with an
unset password, and a service started without `DATABASE_URL` aborts with
`RuntimeError: DATABASE_URL is not set…` rather than silently connecting as a default user.

Verify substitution resolved before debugging anything else — this prints the effective
config **including passwords**, so redirect it rather than pasting the output anywhere:

```bash
docker compose config | grep DATABASE_URL
```

Seven DSNs must come back, each naming a different host and database.

Host names like `db-user-postgres` are **Docker DNS names**, reachable only from inside the
Compose network. From your host the same databases are `localhost:5432` through `:5438` —
which is why the host-run section below builds `DATABASE_URL` by hand. The `*_SERVICE_URL`
values have the same constraint.

If you change a password, reset that volume with `docker compose down -v`. Postgres only
applies the variable when it initialises an empty data directory; editing it afterwards has
no effect on an existing volume, so the new password and the existing database will disagree.

Never commit `.env`. The values above are local-laptop defaults only — anything real belongs
in a secrets manager, not in this file.

---

## Quick start

```bash
docker compose up --build -d      # build images and start all 27 containers
docker compose ps                 # all should read "Up" / "healthy"
```

First boot takes a few minutes while the Python images build. Each Postgres container runs
its own schema — [db/user/init.sql](db/user/init.sql),
[db/restaurant/init.sql](db/restaurant/init.sql), [db/order/init.sql](db/order/init.sql),
[db/payment/init.sql](db/payment/init.sql), [db/menu/init.sql](db/menu/init.sql),
[db/rider/init.sql](db/rider/init.sql), [db/analytics/init.sql](db/analytics/init.sql) —
automatically on the **first** boot of its volume. See
[Resetting the databases](#resetting-the-databases) if you change one.

Several things to look at once it is up:

- `docker compose logs orchestrator-worker` should show
  `Worker polling task queue 'order-tasks'`.
- The **Temporal Web UI** is at <http://localhost:8233>, where every order's workflow
  history is browsable.
- The **Jaeger UI** is at <http://localhost:16686> — pick `order-service` and "Find Traces"
  after placing an order to see the whole checkout, saga and Kafka publish as one trace.
- The **Prometheus UI** is at <http://localhost:9090/targets> — every target should read
  `UP`.
- **Grafana** is at <http://localhost:3000> (`admin` / your `GRAFANA_ADMIN_PASSWORD`), with
  the Prometheus datasource and dashboards already provisioned from
  [grafana/provisioning/](grafana/provisioning/) — nothing to click together by hand.
- The **RabbitMQ management UI** is at <http://localhost:15672>.

None of these five are behind the gateway — like the Temporal Web UI, they have no
authentication of their own beyond what's noted above, so proxying them would publish more
than intended.

> **Upgrading an older checkout? `-v` is mandatory.** Week 3 added columns to `orders`
> (`rider_reported_stage`, `rider_reported_at` — D43) and two new outbox tables
> (`order_outbox`, `payment_outbox` — D39), and `init.sql` only runs on an empty volume — so
> without wiping, the schema stays exactly as it was before. Add every new key from
> [Environment file](#environment-file) to `.env` first, then:
>
> ```bash
> docker compose down -v && docker compose up --build -d
> ```
>
> There is no data to keep in a local sandbox, and the smoke test recreates everything it
> needs.

### Verify everything routes

```bash
for p in /health /api/v1/users/health /api/v1/restaurants/health /api/v1/menus/health \
         /api/v1/orders/health /api/v1/payments/health /api/v1/riders/health; do
  printf '%-32s ' "$p"; curl -s -w ' [%{http_code}]\n' "http://localhost$p"
done
```

All seven must return `200`. The service health endpoints also report whether their backing
stores actually round-trip (`database_reachable`, `cache_reachable`, `temporal_reachable`) —
a `200` with `"database_reachable": false` means the app is up but the DB is not.

Two of those flags mean something softer than the others, and the difference is deliberate.
The Menu Service's `"cache_reachable": false` still serves menus, straight from Postgres,
because the cache is a copy and never the source of truth. The Order Service's
`"temporal_reachable": false` still accepts and serves orders; what stops is sagas
*advancing* them, and a retry with the same idempotency key repairs that once the
orchestrator returns.

The Analytics Service has no gateway route — like Temporal's UI, it is not user-facing — so
check it directly on the Docker network:

```bash
docker exec sfo-analytics-service curl -s http://localhost:8008/api/v1/analytics/health
```

The Notification Service's two processes have no HTTP health route at all (one is a bare
Kafka consumer, the other a Celery worker); `docker compose ps` and
`docker compose logs notification-consumer` are the way to check on them.

### Run the test suite

[scripts/smoke-test.sh](scripts/smoke-test.sh) drives all seven services through the gateway
exactly as a client would — the full checkout chain, the whole order lifecycle, and every
edge case in the contract — and asserts status codes and response fields:

```bash
./scripts/smoke-test.sh            # 328 assertions against http://localhost
./scripts/smoke-test.sh --wait     # poll until services are up, then run
./scripts/smoke-test.sh --fast     # 209 assertions, skips the saga sections (~20s vs ~12min)
./scripts/smoke-test.sh --verbose  # also print response bodies
BASE_URL=http://host:8080 ./scripts/smoke-test.sh
```

It exits `0` when everything passes and `1` with a list of failures otherwise, so it works
as a pre-commit check or a CI step. Colour is suppressed when the output is piped.

What it covers beyond status codes:

- **The whole lifecycle** — `created → confirmed → assigned → picked_up → delivered`, read
  back out of the tracking trail with each `previous_status` derived rather than asserted
- **Both compensation paths** — a kitchen rejection and an empty fleet each reach
  `cancelled` with the payment `refunded` and **no rider left claimed**
- **Idempotency** — a replayed `X-Idempotency-Key` returns the *same* order, and the saga's
  payment key is derived from the order id so a retried activity cannot double-charge
- **Server-side pricing** — asserts the recalculated unit price and total, not just a `201`
- **The internal boundary** — every one of the saga's eight endpoints answers `401` without
  `X-Internal-Key`, and a customer token does not substitute for it
- **Where each table landed** — that the menu tree is JSONB in `sfo_menu_core`, the tracking
  trail is relational rows in `sfo_order_core`, and the fleet is in `sfo_rider_core`
- **Referential integrity** — that an entry written against a nonexistent order is refused
  by the foreign key, which the MongoDB collection had no way to do
- **Cache-aside** — that a read populates `menu:<restaurant_id>` in Redis, that publishing
  invalidates it, and that the menu still serves with the cache cold
- **`/metrics` is not routed by the gateway**, and every Prometheus scrape target reports
  healthy
- **Route-template cardinality stays bounded** — the guard against exactly the bug the Week
  3 blueprint draft made (labelling metrics by `request.url.path`, so every order id mints a
  new time series forever)
- **The outbox drains, the Kafka topic has the right partition count, the Schema Registry
  has every subject registered, and the Analytics Service has processed at least one event**
  — the whole eventing chain, checked end to end, not just that the containers are up
- **A pickup or delivery report is durable** — `rider_reported_stage` reads back correctly
  through the same internal endpoint the saga's own recovery activity calls (D43)

Because the lifecycle is now driven by a workflow, every post-checkout status assertion
**polls**. Asserting immediately would be a race that passes on an idle laptop and fails
under load.

[scripts/saga-resilience-test.sh](scripts/saga-resilience-test.sh) is separate, and slower
(a few minutes), because it is destructive and because it waits out real timeouts. It holds
the checks that actually justify running a workflow engine — everything in the smoke test
could be passed by a synchronous implementation with a retry loop; none of these could:

```bash
./scripts/saga-resilience-test.sh
```

- **Concurrency** — four orders against a fleet of one. Exactly one is assigned; the other
  three cancel and refund rather than double-booking. This is what proves the
  `FOR UPDATE SKIP LOCKED` claim in the dispatch query.
- **Durability** — the worker is restarted *twice* mid-saga. The order still reaches
  `delivered`, and the trail has one entry per transition, proving the replayed activities
  were idempotent.
- **Kitchen timeout** — nobody answers the ticket. The order cancels, the payment is
  refunded, and the abandoned ticket is expired so it stops occupying a capacity slot.
- **Lost decision signal** — the kitchen's answer is written straight to its database,
  bypassing the relay that would normally carry it, which is exactly what a signal lost in
  flight looks like. A lost *acceptance* must still complete the order without refunding;
  a lost *rejection* must still cancel and refund.
- **Lost rider signal (D43)** — the same idea applied to a pickup report: written straight
  to `orders.rider_reported_stage`, bypassing the relay. Checks the mechanism (the column,
  its forward-only guard) rather than waiting out the live 3600-second saga timeout, which
  no automated suite should block on — see the script's own comment for why.

The **Kafka-down proof** lives in the smoke test, not here: run
`./scripts/smoke-test.sh` once with the stack as-is and once with
`docker compose stop kafka schema-registry`, and both runs pass identically. That is the
machine-checkable version of D38 — nothing on the checkout or saga path depends on the
eventing stack being up.

Each run generates unique emails, phone numbers, and idempotency keys, so both are safe to
run repeatedly against the same database. They only ever create data — nothing is deleted —
so use `docker compose down -v` when you want a clean slate.

Requires `bash`, `curl`, and `python3` on the host; nothing is installed into the containers.
The resilience script additionally needs `docker` access to the local stack.

For poking at a single endpoint by hand, see
[readme/api-testing-guide.md](readme/api-testing-guide.md) — the same scenarios as
copy-pasteable `curl` commands, grouped by service, with the expected response for each.

For exercising the same contract from Postman instead of a terminal, import
[postman/smartfoodops.postman_collection.json](postman/smartfoodops.postman_collection.json) —
129 requests across 9 folders, generated from the same scenarios `smoke-test.sh` drives,
including a Collection-Runner-only happy path through the saga (self-looping status polls,
since checkout is asynchronous). See [postman/README.md](postman/README.md) for import steps,
run order, and what it can't check that the shell script can.

### Shut down

```bash
docker compose down          # stop containers, keep data
docker compose down -v       # stop and wipe all volumes (fresh databases)
```

---

## Authentication

Every endpoint except the health probes, `register`, `login` and `refresh` requires an
access token. **Identity is never accepted in a request body** — who you are is whatever
your token says, so `owner_id` and `customer_id` are no longer fields a client can send.

Tokens are **RS256**, signed by the User Service, which is the only container holding the
private key (`JWT_PRIVATE_KEY_B64`). Every other service gets the public key and can verify
a token but never mint one. Confirm that split at any time:

```bash
docker compose exec order-service printenv | grep JWT   # public key only
```

A session is a short access token plus a long refresh token:

| Token | Lifetime | Stored | Revocable |
|---|---|---|---|
| Access (JWT) | 15 minutes | nowhere — stateless | no, expires on its own |
| Refresh (opaque) | 7 days | Redis DB 1, SHA-256 hashed | yes — that is what logout does |

Refreshing **rotates**: the presented token is consumed in the same round trip it is read
in, so a captured token stops working the moment the real client next refreshes. Because an
already-issued access token cannot be withdrawn, logout ends a session within one access
token lifetime rather than instantly — which is why that lifetime is short.

```bash
# Log in
curl -s -X POST http://localhost/api/v1/users/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@example.com","password":"Sup3rSecret!"}'
# -> {"access_token":"eyJ…","refresh_token":"…","token_type":"bearer","expires_in":900}

# Use it
curl -s http://localhost/api/v1/users/<USER_UUID> -H "Authorization: Bearer $ACCESS"

# Trade a refresh token for a fresh pair (the old one dies here)
curl -s -X POST http://localhost/api/v1/users/refresh \
  -H 'Content-Type: application/json' -d '{"refresh_token":"<REFRESH>"}'

# End the session
curl -s -X POST http://localhost/api/v1/users/logout \
  -H "Authorization: Bearer $ACCESS" \
  -H 'Content-Type: application/json' -d '{"refresh_token":"<REFRESH>"}'
```

A failed login returns the same message whether the email is unknown or the password is
wrong, and takes the same time either way — otherwise the endpoint would answer "does this
person have an account here?" to anyone who asks.

### Service-to-service calls

Two mechanisms, deliberately different:

- **On behalf of a user** — the caller's bearer token is forwarded downstream unchanged, so
  a service can never do more than the user who invoked it. The Payment Service reads an
  order with *your* token, which is exactly why it cannot pay for someone else's.
- **Internal only** — `POST /api/v1/orders/logs` takes `X-Internal-Key` instead. Forwarding
  a user token there would let customers write the audit trail describing their own orders.

---

## API walkthrough

A full checkout, in order. Every call goes through the gateway on port 80.

**1 — Register an owner and a customer**

```bash
curl -s -X POST http://localhost/api/v1/users/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@example.com","password":"Sup3rSecret!","full_name":"Owner One",
       "phone":"+923001112221","role":"restaurant_admin"}'
```

Roles are resolved against the `roles` table at request time: `customer`,
`restaurant_admin`, `rider`, `system_admin`. Passwords are bcrypt-hashed before storage.

**2 — Log in as each of them**

```bash
OWNER=$(curl -s -X POST http://localhost/api/v1/users/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@example.com","password":"Sup3rSecret!"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

Repeat for the customer to get `$CUSTOMER`. Every call below carries one of these.

**3 — Onboard a restaurant** (the token must carry the `restaurant_admin` role)

```bash
curl -s -X POST http://localhost/api/v1/restaurants/onboard \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"name":"SFO Diner","address":"12 Blue Area, Islamabad",
       "latitude":33.6844,"longitude":73.0479,"capacity":40}'
```

The restaurant is owned by the token's subject. There is no `owner_id` to send.

**4 — Publish a menu** (upsert — re-posting replaces the tree and keeps `created_at`)

Requires `restaurant_admin` **and** ownership of this particular restaurant: holding the
role is not enough to rewrite a competitor's prices.

```bash
curl -s -X POST http://localhost/api/v1/menus \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"restaurant_id":"<RESTAURANT_UUID>","categories":[{
        "category_id":"cat_entrees_100","category_name":"Entrees","display_order":1,
        "items":[{"item_id":"item_burger_001","name":"Intelligent SFO Burger",
                  "description":"Double patty beef burger.","base_price":12.99,"is_available":true,
                  "customization_groups":[{"group_id":"grp_add_ons","group_name":"Select Add-Ons",
                    "min_selection":0,"max_selection":3,
                    "options":[{"name":"Extra Cheddar Cheese","extra_price":1.50},
                               {"name":"Smoked Bacon","extra_price":2.25}]}]}]}]}'
```

**5 — Place an order** (the header is mandatory)

```bash
curl -s -X POST http://localhost/api/v1/orders \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: sfo-key-0001' \
  -d '{"restaurant_id":"<RESTAURANT_UUID>",
       "items":[{"item_id":"item_burger_001","quantity":2,
                 "customizations":{"grp_add_ons":["Extra Cheddar Cheese"]}}],
       "total_amount":28.98}'
```

The order belongs to the token's subject — there is no `customer_id` to send, and therefore
no way to place an order in someone else's name. Requires the `customer` role.

The total is **recalculated server-side** from the live menu (12.99 + 1.50 × 2 = 28.98) —
the client's `total_amount` is only checked, never trusted. Repeating the same
`X-Idempotency-Key` returns the original order as `200` instead of creating a second one.

**6 — Payment happens on its own** (the saga's first step)

Nothing to call. The workflow authorises the payment within a second or so, and the order
moves to `confirmed`:

```bash
curl -s http://localhost/api/v1/orders/<ORDER_UUID> -H "Authorization: Bearer $CUSTOMER"
# -> {"status":"confirmed", ...}
```

The Payment Service still cannot read the `orders` table, so it fetches the order over HTTP
and refuses an `amount` that does not equal the recalculated total — `422`, with both figures
in the message. The stored payment carries `status: "authorized"` and a `ch_mock_…`
`transaction_reference`, and its idempotency key is `wf-pay-<order_id>`: derived from the
order rather than client-chosen, so a retried activity collides on the unique index instead
of charging twice.

Since the saga owns this step, calling `POST /api/v1/payments` yourself for an orchestrated
order now returns `409 "Order … has already been paid for"` — one payment per order is a
unique constraint, not a convention. That is a deliberate change to the Week 1 contract; the
endpoint remains for direct and manual use, and D30 records why.

**7 — The kitchen decides**

The saga is now parked on a durable timer waiting for a human. The restaurant reads its rail
and answers:

```bash
curl -s http://localhost/api/v1/orders/kitchen/<RESTAURANT_UUID> \
  -H "Authorization: Bearer $OWNER"

curl -s -X POST http://localhost/api/v1/orders/<ORDER_UUID>/accept \
  -H "Authorization: Bearer $OWNER"
```

Both live on the *Order* Service, because since D32 the kitchen's queue is a query over
`orders` rather than a table of its own. Whether the caller owns that restaurant is still
the Restaurant Service's fact, resolved over HTTP before the decision is recorded.

Accepting releases the saga to search for a rider; rejecting — or saying nothing for 120
seconds — refunds the customer, expires the ticket and cancels the order.

**8 — A rider carries it**

```bash
# The rider joins the fleet once, then reports where they are.
curl -s -X POST http://localhost/api/v1/riders \
  -H "Authorization: Bearer $RIDER" -H 'Content-Type: application/json' \
  -d '{"vehicle_type":"motorbike","vehicle_number":"ISB-1234",
       "current_latitude":33.68,"current_longitude":73.04}'

# Dispatch is automatic. Once assigned, the rider reports progress.
curl -s -X POST http://localhost/api/v1/riders/me/orders/<ORDER_UUID>/picked-up \
  -H "Authorization: Bearer $RIDER"
curl -s -X POST http://localhost/api/v1/riders/me/orders/<ORDER_UUID>/delivered \
  -H "Authorization: Bearer $RIDER"
```

The rider is chosen by proximity to the restaurant in a single claim statement, so two
simultaneous dispatches can never take the same rider. Only the rider whose own row records
this order may report on it — which is why neither endpoint takes a rider id.

The whole run is readable end to end:

```bash
curl -s http://localhost/api/v1/orders/<ORDER_UUID>/logs -H "Authorization: Bearer $CUSTOMER"
```

…or visually, with each activity, retry and timer, in the Temporal UI at
<http://localhost:8233> under workflow id `order-<ORDER_UUID>`.

**9 — Everything above also happened on Kafka, with nobody calling anything new**

No endpoint changed and no extra call was needed: every transition above committed an
outbox row in the same transaction, a background relay published it, and two independent
consumers picked it up.

```bash
# The whole checkout, saga and Kafka publish, as one trace:
open "http://localhost:16686/search?service=order-service&limit=1"

# The business counters the Analytics Service derived from the same events:
curl -s http://localhost:9090/api/v1/query --data-urlencode \
  'query=sfo_business_orders_placed_total' | python3 -m json.tool

# The simulated SMS/email the Notification Service dispatched on confirmation:
docker compose logs notification-worker | grep DISPATCH
```

### Endpoint reference

| Method | Path | Who may call it | Notes |
|---|---|---|---|
| `GET` | `/health` | anyone | Gateway only, does not touch services |
| `GET` | `/api/v1/{users,restaurants,menus,orders,payments,riders}/health` | anyone | Per-service + backing store |
| `POST` | `/api/v1/users/register` | anyone | `201`; bcrypt hash, role resolved via DB |
| `POST` | `/api/v1/users/login` | anyone | Access + refresh pair; one message for every failure |
| `POST` | `/api/v1/users/refresh` | anyone holding a refresh token | Rotates: the presented token is consumed |
| `POST` | `/api/v1/users/logout` | any signed-in user | `204`; revokes the refresh token |
| `GET` | `/api/v1/users/{user_id}` | the subject, or `system_admin` | Joins `roles`, returns the role **name** |
| `POST` | `/api/v1/restaurants/onboard` | `restaurant_admin` | `201`; owner taken from the token, verified over HTTP |
| `GET` | `/api/v1/restaurants/{restaurant_id}` | any signed-in user | Exposes `is_active` to other services |
| `POST` | `/api/v1/menus` | `restaurant_admin` **owning that restaurant** | Upsert full category/item/customization tree |
| `GET` | `/api/v1/menus/{restaurant_id}` | any signed-in user | Used by the Order Service to price a cart |
| `POST` | `/api/v1/orders` | `customer` | `201` new / `200` idempotent replay; customer taken from the token |
| `GET` | `/api/v1/orders/{order_id}` | the order's customer, or `system_admin` | Exposes the recalculated `total_amount` to the Payment Service |
| `POST` | `/api/v1/orders/logs` | services only (`X-Internal-Key`) | Appends one row to `order_tracking_logs`; `422` on an unknown order or an undefined status |
| `GET` | `/api/v1/orders/{order_id}/logs` | the order's customer, or `system_admin` | The full transition timeline, oldest first |
| `POST` | `/api/v1/payments` | `customer` owning the order | `409` for a saga-owned order (D30); otherwise `201`/`200` idempotent replay |
| `GET` | `/api/v1/payments/{payment_id}` | the order's customer | Where a payment stopped — `pending`, `authorized` or `refunded` |
| `GET` | `/api/v1/orders/kitchen/{restaurant_id}` | `restaurant_admin` owning it | The kitchen's rail, oldest first. Returns a narrowed projection — no `total_amount`, `customer_id` or `idempotency_key` |
| `POST` | `/api/v1/orders/{order_id}/accept` | `restaurant_admin` owning it | Releases the saga to dispatch a rider; `changed: false` on a second call |
| `POST` | `/api/v1/orders/{order_id}/reject` | `restaurant_admin` owning it | Triggers refund + cancel |
| `POST` | `/api/v1/riders` | `rider` | `201`; rider taken from the token, live role re-checked over HTTP |
| `GET` | `/api/v1/riders/me` | `rider` | Own profile — availability, location, current order |
| `PATCH` | `/api/v1/riders/me/location` | `rider` | Until a location is reported, the rider is invisible to dispatch |
| `PATCH` | `/api/v1/riders/me/availability` | `rider` | `409` while carrying an order |
| `POST` | `/api/v1/riders/me/orders/{order_id}/picked-up` | the rider carrying it | `204`; signals the saga |
| `POST` | `/api/v1/riders/me/orders/{order_id}/delivered` | the rider carrying it | `204`; signals the saga, which then releases the rider |

**Service-to-service only** (`X-Internal-Key`, never reachable with a user token — D26):

| Method | Path | Called by | Notes |
|---|---|---|---|
| `POST` | `/api/v1/orders/logs` | any service | Appends one reported transition; `422` on an unknown order or undefined status |
| `POST` | `/api/v1/orders/{order_id}/rider-report` | Rider | Records the pickup/delivery stage the saga reads back on a timeout; the Rider Service signals Temporal itself (D47) |
| `GET` | `/api/v1/orders/{order_id}/internal` | Payment, worker | Same order as the bearer path, for callers with no user |
| `POST` | `/api/v1/payments/authorize` | worker | Amount as a **string** so the decimal stays exact (D07) |
| `POST` | `/api/v1/payments/refund` | worker | Compensation; idempotent by status, and sweeps stranded `pending` rows |
| `POST` | `/api/v1/riders/dispatch` | worker | Claims the nearest rider; `{"assigned": false}` is a `200`, not an error |
| `POST` | `/api/v1/riders/release` | worker | Compensation; releasing an unheld order is success |
| `GET` | `/api/v1/users/{user_id}/internal` | Notification Consumer | Resolves `phone`/`email` for a Kafka consumer with no user token to forward (Week 3, D45) |

Interactive docs per service, once you expose a port (see below): `http://localhost:<port>/docs`.

### Error contract

| Code | When |
|---|---|
| `400` | Unknown role name; missing `X-Idempotency-Key`; header disagreeing with `idempotency_key` in a payment body |
| `401` | No bearer token, or one that is malformed, expired or badly signed; failed login; dead or already-used refresh token; internal endpoint reached without `X-Internal-Key`. Carries `WWW-Authenticate: Bearer` |
| `403` | Authenticated, but not allowed: wrong role for the action, or someone else's user / order / payment. Also what a downstream refusal becomes when a forwarded token is rejected |
| `404` | Unknown restaurant / menu / order / payment; inactive restaurant. **Not** an unknown user id — that is a `403`, since the ownership check runs before the lookup and must not reveal which ids exist |
| `409` | Duplicate email (case-insensitive) or phone; an order that already has a payment |
| `422` | Pydantic validation; `min_selection > max_selection`; unavailable or off-menu item; total mismatch; order naming an unknown restaurant; payment naming an unknown order or not settling it exactly |
| `500` | Postgres connection pool starved |
| `502` | Unexpected response from an upstream service |
| `503` | Upstream service unreachable |

---

## Working locally

### Rebuild one service after editing it

Code is baked into the image at build time, so a rebuild is required:

```bash
docker compose up -d --build order-service
```

### Hot reload (recommended while iterating)

Create a `docker-compose.override.yml` — Compose merges it automatically, and it stays
out of the committed `docker-compose.yml`:

Mount the service directory at `/app/<service>` and the shared chassis at `/app/common`, so
edits to either are picked up. Each service is a package, so the mount point and the
`uvicorn` target both carry its name:

```yaml
services:
  user-service:
    volumes: ["./services/user:/app/user", "./services/common:/app/common"]
    command: ["uvicorn", "user.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
  restaurant-service:
    volumes: ["./services/restaurant:/app/restaurant", "./services/common:/app/common"]
    command: ["uvicorn", "restaurant.main:app", "--host", "0.0.0.0", "--port", "8002", "--reload"]
  menu-service:
    volumes: ["./services/menu:/app/menu", "./services/common:/app/common"]
    command: ["uvicorn", "menu.main:app", "--host", "0.0.0.0", "--port", "8003", "--reload"]
  order-service:
    volumes: ["./services/order:/app/order", "./services/common:/app/common"]
    command: ["uvicorn", "order.main:app", "--host", "0.0.0.0", "--port", "8004", "--reload"]
  payment-service:
    volumes: ["./services/payment:/app/payment", "./services/common:/app/common"]
    command: ["uvicorn", "payment.main:app", "--host", "0.0.0.0", "--port", "8005", "--reload"]
  rider-service:
    volumes: ["./services/rider:/app/rider", "./services/common:/app/common"]
    command: ["uvicorn", "rider.main:app", "--host", "0.0.0.0", "--port", "8006", "--reload"]
  orchestrator-service:
    volumes: ["./services/orchestrator:/app/orchestrator", "./services/common:/app/common"]
    command: ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8007", "--reload"]
  analytics-service:
    volumes: ["./services/analytics:/app/analytics", "./services/common:/app/common"]
    command: ["uvicorn", "analytics.main:app", "--host", "0.0.0.0", "--port", "8008", "--reload"]
```

`orchestrator-worker`, `notification-consumer` and `notification-worker` are long-running
processes rather than request handlers, so `--reload` does not apply the same way; restart
them by hand after editing their code:

```bash
docker compose restart orchestrator-worker      # workflows/order.py, activities/order.py
docker compose restart notification-consumer    # consumer.py
docker compose restart notification-worker      # tasks.py, worker.py
```

Then `docker compose up -d` — saving a `.py` file restarts that worker in about a second.

### Reaching a service directly (bypassing the gateway)

Service ports are not published to the host by default. Add to the override file:

```yaml
services:
  order-service:
    ports: ["8004:8004"]
```

Then `http://localhost:8004/docs` gives you Swagger UI for that service.

### Running one service on the host

The datastores publish host ports, so a service can run outside Docker against them.
Point the URLs at `localhost` and keep the rest of the stack in Compose:

```bash
cd services
python3 -m venv .venv && source .venv/bin/activate
pip install -r user/requirements.txt

# No PYTHONPATH juggling: `user` and `common` are both packages in this directory,
# which is the same shape the image has at /app.

# Source the password from .env so it never lands in your shell history.
set -a && source ../.env && set +a
export DATABASE_URL="postgresql://sfo_user_admin:${USER_POSTGRES_PASSWORD}@localhost:5432/sfo_user_core"

uvicorn user.main:app --reload --port 8001
```

Each service reads the same `DATABASE_URL` variable, but points it at a different host port
— that is the whole of the change database-per-service asks of the application code:

| Service | Host DSN |
|---|---|
| `user` | `postgresql://sfo_user_admin:${USER_POSTGRES_PASSWORD}@localhost:5432/sfo_user_core` |
| `restaurant` | `postgresql://sfo_restaurant_admin:${RESTAURANT_POSTGRES_PASSWORD}@localhost:5433/sfo_restaurant_core` |
| `order` | `postgresql://sfo_order_admin:${ORDER_POSTGRES_PASSWORD}@localhost:5434/sfo_order_core` |
| `payment` | `postgresql://sfo_payment_admin:${PAYMENT_POSTGRES_PASSWORD}@localhost:5435/sfo_payment_core` |

Inter-service calls still use Docker DNS names (`http://user-service:8001`), so a
host-run service can call into the stack only if you also override that service's
`*_SERVICE_URL` to `http://localhost:<port>` and publish the target's port.

### Logs

```bash
docker compose logs -f order-service        # follow one service
docker compose logs --tail=50               # last 50 lines, everything
docker compose logs api-gateway | tail -20  # Nginx access log — useful for 502s
```

### Database access

Each database is a separate container, so pick the one that owns the table you want. `\dt`
in any of them is a quick proof of the split — only that service's tables are there.

```bash
# User database — roles, users (riders moved out in Week 2 — see below)
docker exec -it sfo-user-db psql -U sfo_user_admin -d sfo_user_core
#   \dt              list tables
#   SELECT u.email, r.name FROM users u JOIN roles r ON r.id = u.role_id;

# Restaurant database — restaurants only (the kitchen queue moved to `orders`, D32)
docker exec -it sfo-restaurant-db psql -U sfo_restaurant_admin -d sfo_restaurant_core
#   SELECT id, name, capacity FROM restaurants;

# Order database — orders, their kitchen decisions, and the tracking trail
docker exec -it sfo-order-db psql -U sfo_order_admin -d sfo_order_core
#   SELECT id, status, kitchen_decision, rider_id, total_amount
#     FROM orders ORDER BY created_at DESC LIMIT 5;
#   -- the kitchen's rail: confirmed and undecided. This one predicate is both the admin's
#   -- queue and the capacity count, which is why one partial index serves both.
#   SELECT restaurant_id, count(*) FROM orders
#    WHERE status = 'confirmed' AND kitchen_decision IS NULL GROUP BY 1;

# Payment database — payments
docker exec -it sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core
#   SELECT order_id, amount, status, transaction_reference FROM payments;

# Order tracking trail — lives beside `orders`, in the same database
#   SELECT seq, old_status, new_status, service, updated_by
#     FROM order_tracking_logs WHERE order_id = '<uuid>' ORDER BY seq;

# The outbox tables (Week 3, D39) — lives beside `orders`/`payments`, same databases.
# published_at IS NULL means the relay hasn't reached Kafka with it yet; under normal
# operation this should drain to empty within a second or two of a write.
#   SELECT event_type, seq, published_at FROM order_outbox
#     WHERE aggregate_id = '<uuid>' ORDER BY seq;      -- run against sfo-order-db
#   SELECT event_type, seq, published_at FROM payment_outbox
#     WHERE aggregate_id = '<uuid>' ORDER BY seq;      -- run against sfo-payment-db, same order id

# Menu database — menus (the JSONB tree that replaced the MongoDB collection)
docker exec -it sfo-menu-db psql -U sfo_menu_admin -d sfo_menu_core
#   SELECT restaurant_id, jsonb_pretty(categories) FROM menus;

# Rider database — the delivery fleet (moved out of sfo_user_core in Week 2, D28)
docker exec -it sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core
#   SELECT vehicle_number, is_available, current_order_id FROM riders;
#   -- distance from a point, using the schema's own haversine_km():
#   SELECT vehicle_number,
#          round(haversine_km(current_latitude::float, current_longitude::float,
#                             33.68, 73.04)::numeric, 2) AS km
#     FROM riders WHERE is_available ORDER BY km;
#   -- a non-zero count here after every saga has finished means a rider leaked:
#   SELECT count(*) FROM riders WHERE current_order_id IS NOT NULL;

# Redis — the menu cache (database 0) and refresh tokens (database 1)
docker exec -it sfo-redis redis-cli ping
#   redis-cli KEYS 'menu:*'
#   redis-cli TTL menu:<restaurant_id>     # counts down from 3600

# Analytics database — the Kafka read-model's own dedup table and derived projection
# (Week 3, D44). Nothing else in the platform ever reads or writes this database.
docker exec -it sfo-analytics-db psql -U sfo_analytics_admin -d sfo_analytics_core
#   SELECT status, count(*) FROM order_projections GROUP BY status;
#   SELECT event_type, count(*) FROM processed_events GROUP BY event_type;
#   -- if this database were dropped entirely, resetting the "analytics" consumer group's
#   -- Kafka offset and replaying the topic would rebuild both tables exactly.
```

`psql` inside the container needs no password (local trust); from a GUI client on your host,
connect to `localhost:5432` through `:5438` with the matching role and `.env` password.

Joining across services is deliberately impossible now. To follow an order to its customer,
read `customer_id` and call `GET /api/v1/users/{id}` — the same path the services take.

### Resetting the databases

A schema file runs **only** when its own Postgres volume is empty. After editing one:

```bash
docker compose down -v && docker compose up --build -d
```

This wipes all seven Postgres volumes plus Redis, Kafka and Grafana's own storage. To reset
a single database, target its volume — the others keep their data:

```bash
docker compose rm -sf db-order-postgres
docker volume rm smartfoodops-backend_order_postgres_data
docker compose up -d db-order-postgres
```

There is no migration tooling in Week 1 — schema changes mean a volume reset.

### Adding a dependency

Pins live in `services/<service>/requirements.txt`, which `-r`s the shared chassis list at
`services/common/requirements.txt`. Add a chassis-wide dependency (one every service needs,
like `fastapi`) to `common/requirements.txt`; add a service-specific one (like `redis` for
Menu's cache) to that service's own file. Keep versions pinned so local builds stay
reproducible, and rebuild with `--build` after editing either file — the Dockerfiles copy
requirements in before the source, so a dependency change invalidates the pip layer but not
the whole image.

---

## Project layout

```text
smartfoodops-backend/
├── api-gateway/nginx.conf     # Path-based routing + /health
├── prometheus/prometheus.yml  # Scrape config — every FastAPI service, both Temporal ports,
│                              #   the outbox relays' own metrics, RabbitMQ (Week 3)
├── grafana/provisioning/      # Datasource + dashboards, file-provisioned so `down -v`
│                              #   never loses them (Week 3, D42)
├── kafka/init-topics.sh       # One-shot: creates sfo.order.events.v1 (+ .dlq) explicitly,
│                              #   at the partition count per-order ordering depends on
├── rabbitmq/enabled_plugins   # Enables the management UI and the Prometheus exporter
├── db/                        # One schema per physical database, mounted into its container
│   ├── user/init.sql          # roles (+ seed data), users
│   ├── restaurant/init.sql    # restaurants
│   ├── menu/init.sql          # menus (category tree as JSONB)
│   ├── order/init.sql         # order_status enum, orders (+ outbox + rider report columns,
│   │                          #   Week 3), order_tracking_logs, order_outbox
│   ├── payment/init.sql       # payment_status enum, payments, payment_outbox
│   ├── rider/init.sql         # riders
│   └── analytics/init.sql     # processed_events (dedup), order_projections — Week 3, D44
├── services/                  # Shared Docker build context
│   ├── common/                # Shared chassis — infrastructure only, no domain code
│   │   ├── auth.py            # RS256 verify/issue, CurrentUser, require_role, require_self_or_admin
│   │   ├── bootstrap.py       # ServiceRuntime: logging + telemetry + a service's own DB pool, one call
│   │   ├── config.py          # Env defaults, timeouts, pool bounds
│   │   ├── errors.py          # HTTPException factories (400/403/404/409/422/500/502/503)
│   │   ├── health.py          # health_payload(): the {status, service, database_reachable} shape
│   │   ├── kafka.py           # build_producer(), SchemaRegistryValidator, trace-header
│   │   │                      #   inject/extract across the Kafka boundary (Week 3, D40)
│   │   ├── events/            # The event contract: EventEnvelope + one pydantic model per
│   │   │                      #   event type, shared by every producer and consumer (Week 3)
│   │   ├── lifespan.py        # compose_lifespan(): chain several ASGI lifespans into one
│   │   ├── logging_config.py  # Uniform log format, now with trace/span id correlation (Week 3)
│   │   ├── money.py           # Decimal currency resolution shared by Order and Payment
│   │   ├── outbox.py          # append_outbox() + OutboxRelay — the transactional outbox
│   │   │                      #   and the background publisher (Week 3, D39)
│   │   ├── postgres.py        # PostgresPool: lifespan, cursor, health probe, constraint_of()
│   │   ├── redis_store.py     # RedisStore: the connection lifecycle Menu's cache and User's
│   │   │                      #   refresh store both need
│   │   ├── repository.py      # Repository base: one()/all()/write_one() over a leased cursor
│   │   ├── service_client.py  # Inter-service HTTP, failure translation, and ServiceFacade
│   │   ├── telemetry.py       # OpenTelemetry tracing + Prometheus /metrics, one call per
│   │   │                      #   service (Week 3, D41)
│   │   └── temporal.py        # TemporalGateway + workflow_id_for() (Orchestrator Service +
│   │                          #   worker only, since D36) — now with TracingInterceptor (Week 3)
│   ├── user/                  # main.py, deps.py, apis/{users,sessions,health}.py,
│   │                          #   security.py, repositories/{users,sessions}.py,
│   │                          #   schemas/{users,sessions}.py                     (:8001)
│   ├── restaurant/            # + clients/user.py                                (:8002)
│   ├── menu/                  # + clients/restaurant.py, repositories/cache.py    (:8003)
│   ├── order/                 # + apis/{checkout,kitchen,signals,tracking,
│   │                          #   transitions}.py, clients/{user,restaurant,menu,
│   │                          #   orchestrator}.py, schemas/{orders,kitchen,signals,
│   │                          #   tracking,transitions}.py,
│   │                          #   repositories/{orders,tracking,sql}.py, pricing.py
│   │                          #   No Temporal client and no worker (D36) — see the
│   │                          #   Orchestrator entry below. Runs its own outbox relay
│   │                          #   in-process (Week 3, D39)                        (:8004)
│   ├── payment/                # + apis/{payments,saga}.py, clients/order.py,
│   │                            #   authorise.py, gateway.py, amounts.py
│   │                            #   Also runs its own outbox relay (Week 3, D39)   (:8005)
│   ├── rider/                  # + apis/{profile,delivery,dispatch}.py,
│   │                            #   clients/{user,order}.py, fleet.py, eta.py     (:8006)
│   ├── orchestrator/           # main.py, deps.py, worker.py, apis/{health,order}.py,
│   │                            #   schemas/order.py, activities/order.py,
│   │                            #   workflows/order.py, clients/order/{order_service,
│   │                            #   payment,rider}.py
│   │                            #   Split out of the Order Service (D36): its own image,
│   │                            #   its own deployable, no database of its own at all —
│   │                            #   the API side starts/signals sagas over HTTP, the
│   │                            #   worker runs them, and both reach every fact they need,
│   │                            #   including the order itself, over HTTP too. Every
│   │                            #   subdirectory is entity-scoped (today: `order` only),
│   │                            #   so a second workflow this service orchestrates adds
│   │                            #   files beside these rather than growing them. Holds no
│   │                            #   Kafka client at all (Week 3, D38)              (:8007)
│   ├── analytics/               # main.py, deps.py, consumer.py, apis/health.py,
│   │                            #   repositories/projections.py
│   │                            #   A pure Kafka read-model, its own database, no sibling
│   │                            #   calls, no JWT keys (Week 3, D44)               (:8008)
│   └── notification/            # worker.py (Celery app + tasks), consumer.py (the
│                                 #   Kafka-to-Celery bridge), tasks.py, clients/user.py
│                                 #   Two containers, one image (Week 3, D45) — see below
├── scripts/
│   ├── smoke-test.sh           # End-to-end assertions across the whole stack
│   ├── saga-resilience-test.sh # Concurrency, durability and lost-signal recovery
│   └── init_bootstrap.sh       # Regenerates .env / db/*/init.sql / docker-compose.yml /
│                                #   prometheus.yml / grafana provisioning / kafka /
│                                #   rabbitmq byte-for-byte (D20, D42) — the reproducible
│                                #   path from an empty directory to this running stack
├── readme/                    # Blueprints, contracts, and the decision record
├── postman/                   # The same contract as smoke-test.sh, importable into Postman
├── docker-compose.yml         # Orchestration
├── .gitignore                 # Excludes .env, __pycache__, venvs, OS cruft
└── .env                       # Local environment variables — gitignored, create it yourself
```

`notification-consumer` (`python -m notification.consumer`) and `notification-worker`
(`celery -A notification.worker worker`) are two containers built from the same
`services/notification/Dockerfile` — the split is what makes the Celery hop a real
concurrency pool rather than decorative: the consumer commits its Kafka offset only *after*
a task is enqueued, so Kafka is the durable ledger and RabbitMQ never has to be (D45).

Each service directory is a Python *package* — it has an `__init__.py`, and its modules
import each other absolutely (`from order.repositories.orders import ...`). The Dockerfile
copies it to `/app/<service>/` alongside `/app/common/`, so the two are never confusable and
no top-level dependency can shadow a module named `schemas` or `clients` (D34). Concerns are
always a directory, never a bare `.py` file, even where one service has only one module's
worth of content — `restaurant/apis/restaurants.py` is a one-file package, kept a package
for the same reason `restaurant/clients/user.py` already was: uniformity across services
matters more here than the ceremony of an extra folder for one file.

Five `__init__.py` files in `services/orchestrator/` carry a warning worth reading before
editing them — `orchestrator/__init__.py`, `orchestrator/workflows/__init__.py`,
`orchestrator/activities/__init__.py`, `orchestrator/clients/__init__.py` and
`orchestrator/clients/order/__init__.py`. Temporal's workflow sandbox imports all five while
resolving `orchestrator.activities.order` from inside `workflows/order.py`'s
`imports_passed_through()` block — the last two because entity-scoping `clients/` added a
directory to that import path — and a re-export in any of them would put more than the
saga needs back in the worker's import graph — the FastAPI app, `deps.py`, or every sibling
client instead of the two the worker actually calls, among them. All five stay
docstring-only, forever; see D34 and D36. Before D36 this constraint lived on
`services/order/`'s equivalent four files — `repositories/__init__.py` has no counterpart
here because the orchestrator owns no database at all, and two more were added when
`activities/`, `clients/`, `schemas/`, `workflows/` and `apis/` were all made entity-scoped
so a future second workflow gets its own files instead of crowding into `order`'s.

A service's schema lives under `db/<service>/`, not next to its code, because it is consumed
by that service's *database container* at first boot — the service image never reads it.

Every service follows the same layering, so any one of them can be read the same way — the
five concerns below are always a directory, holding one file per domain area (or, for
`clients/`, one file per sibling service called) once there is more than one to separate,
and one file even when there is not:

| Package | Responsibility |
|---|---|
| `main.py` | Composition root: builds the app, composes lifespans, mounts routers — no logic |
| `deps.py` | The process-lifetime singletons (db pool, repositories, clients) |
| `apis/` | Route handlers, grouped by domain area or by caller audience |
| `repositories/` | All database access for the tables this service owns |
| `clients/` | Outbound calls to sibling services — one module per sibling called |
| `schemas/` | Pydantic request/response models (the service's public contract) |
| `pricing.py`, `cache.py`, `amounts.py`, `gateway.py`, `authorise.py`, `fleet.py`, `eta.py`, `security.py` | Service-specific domain or infrastructure detail, extracted only where two or more routes share it |

The Order Service's `clients/orchestrator.py` (handing an order to the saga and signalling
it afterward, both as HTTP calls — request-path only) replaced its old `saga.py`, which held
a Temporal client directly. That client, the Temporal pair `workflows/order.py` /
`worker.py`, and `activities/order.py` all now live in `services/orchestrator/` instead,
split out as their own deployable (D36) — `activities/order.py` stays one module per entity
purely for naming consistency with `clients/order/`, never split further within an entity:
Temporal records every `@activity.defn` method's name in durable workflow history, so
`OrderActivities` stays exactly as it was before either refactor touched anything else; see
D34.

`services/common/` is a shared *chassis*, not a shared domain. It holds connection
pooling, logging, error mapping, HTTP transport, and now a handful of mechanical patterns
that repeated across every service verbatim (health payloads, the bootstrap sequence,
lifespan composition, the cursor/execute/fetch shape) — the plumbing that would otherwise
be copy-pasted into every new service. Domain models, business rules, and table knowledge
stay inside their owning service, so no service can reason about another's data. Because
all seven images need it, the Docker build context is `./services` (not the individual
service directory) and each `Dockerfile` copies `common/` alongside its own source.

The trade-off: a change to `common/` requires rebuilding every service. That is acceptable
in a single-repo Compose setup; if services ever ship on independent release cycles,
`common/` should become a versioned, pip-installed package instead.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `502 Bad Gateway` from Nginx | Target service crashed on boot. `docker compose logs <service>` |
| `502` but the service logs look healthy | Nginx resolved the upstream IP at startup and the container was recreated since. `docker compose restart api-gateway` |
| Port 80 already in use | Another web server is bound. Stop it, or change the gateway's published port |
| `database_reachable: false` | That service's Postgres is still starting, or its volume is mid-init. Re-check after ~10s, then `docker compose ps db-<service>-postgres` |
| `set USER_POSTGRES_PASSWORD in the root .env` on any compose command | The four password keys are missing from `.env` — see [Environment file](#environment-file) |
| Schema changes not visible | A `db/*/init.sql` only runs on an empty volume — `docker compose down -v` |
| `password authentication failed` after changing `.env` | Postgres keeps the password baked into its existing volume. Reset that volume |
| Port 5433 / 5434 / 5435 already in use | Another Postgres is bound. Stop it, or change the published port for that database |
| `503` on menu writes | The menu database is unreachable. `docker compose ps db-menu-postgres` |
| `401` on every call that worked yesterday | The access token expired — they last 15 minutes. `POST /api/v1/users/refresh` with the refresh token, or log in again |
| `401 Access token is invalid` right after a rebuild | `.env` was regenerated, so tokens signed by the old key no longer verify. Log in again |
| Services refuse to start: `JWT_PUBLIC_KEY_B64 is not set` | `.env` predates authentication. Add the keypair — see [Environment file](#environment-file) |
| `403 Not authorised to access this resource in the Order Service` | The order belongs to a different customer. Payments are refused where the order is read, not where the payment is written |
| `401 This endpoint is internal to SmartFoodOps services` | `POST /api/v1/orders/logs` needs `X-Internal-Key`, not a bearer token — it is service-to-service only |
| `409` on a repeated register | Working as intended — email/phone are unique |
| `409 Order … has already been paid for` | Working as intended — `payments.order_id` is unique, so an order can be charged once. Replay the *original* idempotency key to get that payment back |
| A payment stuck at `pending` | The gateway call failed after the row was written. Nothing was charged; Week 2's compensation workflow is what will reconcile these |
| `payments` still in `sfo_order_core` after upgrading | Its volume predates the split. `docker compose down -v`, or reset just that volume as above |
| Edits do nothing | Image is stale. `docker compose up -d --build <service>`, or use the hot-reload override |
| `GET /subjects` on the Schema Registry returns `[]` | Nothing registered yet, or the registry wasn't reachable when a service started. The relay retries registration in the background (5 attempts, 3s apart) — check `docker compose logs order-service \| grep -i schema` |
| Outbox rows never get `published_at` set | The relay logged a Kafka connection failure and is swallowing it by design (checkout must not depend on Kafka). `docker compose ps kafka`, then `docker compose logs order-service \| grep -i outbox` |
| `sfo_outbox_backlog` climbing in Grafana / Prometheus | Same as above, or the topic is missing — `docker compose exec kafka kafka-topics --bootstrap-server localhost:9092 --list` should show `sfo.order.events.v1` |
| Analytics counters not moving after a checkout | Either the outbox hasn't drained (see above) or the consumer is wedged — `docker compose logs analytics-service`, and check `sfo_consumer_last_message_seconds` in Prometheus |
| No SMS/email in the logs after a cancellation | `docker compose ps rabbitmq notification-consumer notification-worker` — three containers, all three need to be up. Check `notification-consumer` logs for the Kafka side and `notification-worker` for the Celery side separately, since a wedge in either looks the same from outside |
| RabbitMQ container unhealthy / `Connection refused` from Celery | `RABBITMQ_USER` / `RABBITMQ_PASSWORD` missing from `.env` — see [Environment file](#environment-file) |
| Grafana shows "No data" on every panel | The datasource is provisioned by file, not by hand — `docker compose ps prometheus grafana`, then confirm targets are `up` at `http://localhost:9090/targets` before suspecting the dashboard |
| A new Prometheus target never shows up as `up` | Prometheus doesn't hot-reload `prometheus.yml`. `docker kill --signal=SIGHUP sfo-prometheus`, or recreate the container |
| Jaeger shows a trace that stops at `POST /api/v1/orders` with nothing from the saga | Expected if the order never reached the orchestrator (e.g. Redis/menu failure short-circuited checkout). If the saga did run, check that `orchestrator-worker` came up after the last rebuild — a stale image predating the tracing interceptor won't propagate the trace |

---

## Notes and known deviations

The blueprint's shared Dockerfile hardcodes port 8000, but `nginx.conf` proxies to
8001–8005. Each Dockerfile therefore binds its own service's port — a literal copy of the
blueprint would make every route a 502.

`UserRegisterRequest.role` is a plain `str` rather than the `UserRole` enum, so an unknown
role is rejected with `400` (per the Week 1 spec) instead of Pydantic's `422`, with the
`roles` table as the single source of truth. `UserRole` remains defined in
[services/user/schemas.py](services/user/schemas.py) for reference.

Every inter-service URL is declared explicitly in `docker-compose.yml` for the service that
calls it, so the wiring is readable from the compose file alone. The identical defaults in
[services/common/config.py](services/common/config.py) exist for the host-run flow, where
nothing sets those variables.

The Database-per-Service guide's compose block drops the schema mount and hardcodes each
password inline. Both are kept as they were: `db/<service>/init.sql` is mounted into its
container (nothing else creates the tables), and passwords are interpolated from `.env`.
Database and role names follow the guide exactly.

The guide also lists `USER_DATABASE_URL` / `RESTAURANT_DATABASE_URL` / `ORDER_DATABASE_URL`
in `.env`. Those are not used here: Compose already assembles each DSN from the one password
key, and a second copy of the same DSN in `.env` would be a second place to keep a password
correct. Each service reads plain `DATABASE_URL` — it has no idea another database exists.

Audit logging is best-effort. The order is already committed when the log call fires, so a
Menu Service failure is logged loudly rather than returned as a `500` — a `500` there would
tell the client the order failed when it exists.

A draft Week 3 blueprint
([readme/week3-event-driven-observability-blueprint.md](readme/week3-event-driven-observability-blueprint.md))
proposed Kafka eventing and observability in a shape that would have broken checkout, undone
the Week 2 saga, and shipped several silent-failure bugs. What was actually built departs from
it point for point — the full verdict, the corrected architecture, and the reasoning behind
every choice live in [readme/key-decisions.md](readme/key-decisions.md) as **D38–D45**. The
short version: Temporal still owns every decision on the order's critical path; Kafka only
carries facts already committed to a database, via the transactional outbox
(`services/common/outbox.py`) rather than a post-commit publish, so nothing on checkout ever
blocks on or fails because of Kafka.

### Deviations from the payments migration blueprint

[readme/payments-service-migration.md](readme/payments-service-migration.md) is followed on
every externally visible point — service on 8005, database `sfo_payment_core` on host 5435,
`order_id` as a plain UUID, mandatory idempotency key. Five things differ, all deliberate:

- **Its `main.py` is boilerplate**: the handler returns a hardcoded UUID and every database
  line is commented out. The service here is implemented for real against the shared chassis
  (`PostgresPool`, `ServiceClient`, `common.errors`) like every other service.
- **A replay answers `200`, not `201`.** The blueprint returns the stored transaction with
  the route's default `201`; `200` is what the Order Service already does for the same
  situation, and a `201` would claim something was created when nothing was.
- **`gen_random_uuid()` → `uuid_generate_v4()`**, and the two extra indexes are dropped. The
  other three databases use the `uuid-ossp` extension, and `order_id` / `idempotency_key` are
  both `UNIQUE`, which Postgres already backs with an index each — a second index on either
  would only cost writes. An index on `status` replaces them, for sweeping stuck payments.
- **Its compose block hardcodes the password inline and omits the schema mount.** Kept as the
  other databases are: password interpolated from `.env`, `db/payment/init.sql` mounted into
  the container (nothing else creates the table).
- **`GET /api/v1/orders/{order_id}` had to be added to the Order Service.** The blueprint
  hands the Payment Service an `ORDER_SERVICE_URL` but no endpoint to call with it, so the
  order behind a payment could not be verified at all. That endpoint is the replacement for
  the `payments.order_id` foreign key, and it makes the Order Service — not the client — the
  authority on what an order costs.

The one contract kept exactly as written despite being unusual: the idempotency key travels
in **both** the `X-Idempotency-Key` header and the request body, and a mismatch is a `400`.
The Order Service reads the header alone. The blueprint is explicit about wanting both, and a
caller that disagrees with itself about which transaction it is retrying is worth rejecting.

Two additions beyond the v6 contracts, both required by the flow:
`GET /api/v1/menus/{restaurant_id}` (the Order Service cannot price a cart without it) and
`OrderItemSnapshot`, an `OrderItemSelection` subclass carrying `unit_price` / `line_total` /
`selected_options` so the JSONB snapshot survives into the response.

**Credentials in this repo are local development values only.** The four database passwords
live in the gitignored `.env` and nowhere else; no committed file contains one. Do not reuse
them anywhere, and move real values to a secrets manager before this leaves a local
environment. Splitting the databases also splits the blast radius: a leaked password now
opens one service's data, not the whole platform.
