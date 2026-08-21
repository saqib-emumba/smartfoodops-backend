# SmartFoodOps — Backend (Weeks 1–2)

A containerised, six-service food-ordering backend fronted by an Nginx API gateway, with the
order lifecycle driven by a durable Temporal workflow. Everything runs locally through Docker
Compose: six PostgreSQL databases, Redis, the Temporal dev server, the gateway, six FastAPI
services and one workflow worker.

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
                                        │
                          ┌─────────────┴──────────────┐
                          │   Temporal dev server      │
                          │   :7233 gRPC  :8233 UI     │
                          │   :9233 /metrics           │
                          └─────────────┬──────────────┘
                                        │ polls "order-tasks"
                                 ┌──────┴───────┐
                                 │ order-worker │  ← runs the saga
                                 └──────────────┘
```

Arrows between services are **HTTP calls, not shared tables**. Each service owns its data:

| Service | Port | Owns | Its database (host port) | Reaches out to |
|---|---|---|---|---|
| `user-service` | 8001 | `roles`, `users` | `sfo_user_core` @ `sfo-user-db` (5432) | — |
| `restaurant-service` | 8002 | `restaurants`, `order_tickets` | `sfo_restaurant_core` @ `sfo-restaurant-db` (5433) | User Service (owner check), Order Service (signal relay) |
| `menu-service` | 8003 | `menus` | `sfo_menu_core` @ `sfo-menu-db` (5436), cached in Redis DB 0 | Restaurant Service (active check) |
| `order-service` | 8004 | `orders`, `order_tracking_logs` | `sfo_order_core` @ `sfo-order-db` (5434) | Menu Service (pricing), User + Restaurant Services (participant checks), Temporal |
| `payment-service` | 8005 | `payments` | `sfo_payment_core` @ `sfo-payment-db` (5435) | Order Service (order + amount check) |
| `rider-service` | 8006 | `riders` | `sfo_rider_core` @ `sfo-rider-db` (5437) | User Service (role check), Order Service (signal relay) |
| `order-worker` | — | nothing | reads/writes `sfo_order_core` | Payment, Restaurant and Rider Services (saga activities) |

Rules the code enforces deliberately:

- The Restaurant Service never reads the `users` table — it calls `GET /api/v1/users/{id}`.
- The Order Service never reads the `menus` table — it calls `GET /api/v1/menus/{id}`.
- The Payment Service never reads the `orders` table — it calls `GET /api/v1/orders/{id}`.
- The Rider Service never reads the `users` table — it calls `GET /api/v1/users/{id}`.
- No service holds credentials for a database it does not own.
- Only two processes hold a Temporal client: `order-service` (starts sagas, relays signals)
  and `order-worker` (runs them). Every other service reports what it observed over HTTP and
  is unaware an orchestrator exists.

### The order saga

`POST /api/v1/orders` still does everything it did in Week 1 — re-prices the cart, verifies
the customer and restaurant over HTTP, and commits the order with the opening entry of its
audit trail in one transaction. It then starts a workflow whose id is derived from the order
id, so starting one twice is a no-op.

```
created ──payment authorised──▶ confirmed
                                    │  ticket on the kitchen's rail,
                                    │  durable timer (120s)
                    ┌───────────────┴───────────────┐
              accepted                      rejected / silence
                    │                               │
          rider search (6 × 10s)                    │
                    │                               │
       ┌────────────┴────────────┐                  │
   assigned                 nobody free             │
       │                         │                  │
 rider signals               ────┴──────────────────┴────▶ COMPENSATE
 picked_up ──▶ delivered                                   refund payment
       │                                                   release rider
 release rider                                             expire ticket
                                                           ──▶ cancelled
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

Two references keep a real foreign key, because both of their ends live in one database:
`order_tracking_logs.order_id` (which is why that table moved out of MongoDB — D24) and
`order_tickets.restaurant_id`. `riders.user_id` used to be a third, until the fleet moved
into its own database in Week 2 and it became a checked reference like the others (D28).
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
- Ports free on the host: **80, 5432, 5433, 5434, 5435, 5436, 6379**
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

REDIS_URL=redis://cache-redis:6379/0

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
| `USER_POSTGRES_PASSWORD`, `RESTAURANT_POSTGRES_PASSWORD`, `ORDER_POSTGRES_PASSWORD`, `PAYMENT_POSTGRES_PASSWORD`, `MENU_POSTGRES_PASSWORD` | yes | One per database. A missing key aborts **every** compose command with `set <KEY> in the root .env` |
| `*_SERVICE_URL` | no | Compose sets these explicitly per service; the copies here are for the host-run flow below |

Nothing falls back to a baked-in password, at either layer. Compose refuses to start with an
unset password, and a service started without `DATABASE_URL` aborts with
`RuntimeError: DATABASE_URL is not set…` rather than silently connecting as a default user.

Verify substitution resolved before debugging anything else — this prints the effective
config **including passwords**, so redirect it rather than pasting the output anywhere:

```bash
docker compose config | grep DATABASE_URL
```

Four DSNs must come back, each naming a different host and database.

Host names like `db-user-postgres` are **Docker DNS names**, reachable only from inside the
Compose network. From your host the same databases are `localhost:5432`, `:5433`, `:5434` and
`:5435` — which is why the host-run section below builds `DATABASE_URL` by hand. The
`*_SERVICE_URL` values have the same constraint.

If you change a password, reset that volume with `docker compose down -v`. Postgres only
applies the variable when it initialises an empty data directory; editing it afterwards has
no effect on an existing volume, so the new password and the existing database will disagree.

Never commit `.env`. The values above are local-laptop defaults only — anything real belongs
in a secrets manager, not in this file.

---

## Quick start

```bash
docker compose up --build -d      # build images and start all 16 containers
docker compose ps                 # all should read "Up" / "healthy"
```

First boot takes a few minutes while the Python images build. Each Postgres container runs
its own schema — [db/user/init.sql](db/user/init.sql),
[db/restaurant/init.sql](db/restaurant/init.sql), [db/order/init.sql](db/order/init.sql),
[db/payment/init.sql](db/payment/init.sql), [db/menu/init.sql](db/menu/init.sql),
[db/rider/init.sql](db/rider/init.sql) — automatically on the **first** boot of its volume.
See [Resetting the databases](#resetting-the-databases) if you change one.

Two things to look at once it is up: `docker compose logs order-worker` should show
`Worker polling task queue 'order-tasks'`, and the **Temporal Web UI** is at
<http://localhost:8233>, where every order's workflow history is browsable. The UI is
deliberately *not* behind the gateway — it has no authentication of its own, so proxying it
would publish every workflow's history, including the arguments each was started with.

> **Upgrading a Week 1 checkout? `-v` is mandatory.** The `riders` table moved out of
> `sfo_user_core` into its own database (D28), and `init.sql` only runs on an empty volume —
> so without wiping, the user database keeps a stale `riders` table and the rider database is
> never created. Add `RIDER_POSTGRES_PASSWORD`, `RIDER_SERVICE_URL` and `TEMPORAL_ADDRESS`
> to `.env`, then:
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

### Run the test suite

[scripts/smoke-test.sh](scripts/smoke-test.sh) drives all six services through the gateway
exactly as a client would — the full checkout chain, the whole order lifecycle, and every
edge case in the contract — and asserts status codes and response fields:

```bash
./scripts/smoke-test.sh            # 174 assertions against http://localhost
./scripts/smoke-test.sh --wait     # poll until services are up, then run
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

Because the lifecycle is now driven by a workflow, every post-checkout status assertion
**polls**. Asserting immediately would be a race that passes on an idle laptop and fails
under load.

[scripts/saga-resilience-test.sh](scripts/saga-resilience-test.sh) is separate, and slower
(a few minutes), because it is destructive and because it waits out real timeouts. It holds
the two checks that actually justify running a workflow engine — everything in the smoke test
could be passed by a synchronous implementation with a retry loop; neither of these could:

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

Each run generates unique emails, phone numbers, and idempotency keys, so both are safe to
run repeatedly against the same database. They only ever create data — nothing is deleted —
so use `docker compose down -v` when you want a clean slate.

Requires `bash`, `curl`, and `python3` on the host; nothing is installed into the containers.
The resilience script additionally needs `docker` access to the local stack.

For poking at a single endpoint by hand, see
[readme/api-testing-guide.md](readme/api-testing-guide.md) — the same scenarios as
copy-pasteable `curl` commands, grouped by service, with the expected response for each.

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
curl -s http://localhost/api/v1/restaurants/<RESTAURANT_UUID>/tickets \
  -H "Authorization: Bearer $OWNER"

curl -s -X POST http://localhost/api/v1/restaurants/tickets/<ORDER_UUID>/accept \
  -H "Authorization: Bearer $OWNER"
```

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
| `GET` | `/api/v1/restaurants/{restaurant_id}/tickets` | `restaurant_admin` owning it | The kitchen's rail, oldest first; `?ticket_status=` to filter |
| `POST` | `/api/v1/restaurants/tickets/{order_id}/accept` | `restaurant_admin` owning it | Releases the saga to dispatch a rider; second call is a no-op |
| `POST` | `/api/v1/restaurants/tickets/{order_id}/reject` | `restaurant_admin` owning it | Triggers refund + cancel |
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
| `POST` | `/api/v1/orders/{order_id}/signals` | Restaurant, Rider | The one door into a running saga; `404` if it has finished |
| `GET` | `/api/v1/orders/{order_id}/internal` | Payment, worker | Same order as the bearer path, for callers with no user |
| `GET` | `/api/v1/restaurants/{restaurant_id}/internal` | worker | Coordinates for proximity dispatch |
| `POST` | `/api/v1/restaurants/tickets` | worker | Idempotent on `order_id`; `{"queued": false}` when at capacity |
| `POST` | `/api/v1/restaurants/tickets/{order_id}/expire` | worker | Compensation; `pending`-only, so it cannot overwrite a decision |
| `POST` | `/api/v1/payments/authorize` | worker | Amount as a **string** so the decimal stays exact (D07) |
| `POST` | `/api/v1/payments/refund` | worker | Compensation; idempotent by status, and sweeps stranded `pending` rows |
| `POST` | `/api/v1/riders/dispatch` | worker | Claims the nearest rider; `{"assigned": false}` is a `200`, not an error |
| `POST` | `/api/v1/riders/release` | worker | Compensation; releasing an unheld order is success |

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

Mount the service directory at `/app` and the shared chassis at `/app/common`, so edits to
either are picked up:

```yaml
services:
  user-service:
    volumes: ["./services/user:/app", "./services/common:/app/common"]
    command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
  restaurant-service:
    volumes: ["./services/restaurant:/app", "./services/common:/app/common"]
    command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002", "--reload"]
  menu-service:
    volumes: ["./services/menu:/app", "./services/common:/app/common"]
    command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--reload"]
  order-service:
    volumes: ["./services/order:/app", "./services/common:/app/common"]
    command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004", "--reload"]
  payment-service:
    volumes: ["./services/payment:/app", "./services/common:/app/common"]
    command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005", "--reload"]
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
cd services/user
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn "pydantic[email]" psycopg2-binary bcrypt

# In the image, `common/` sits inside /app; on the host it is one level up.
export PYTHONPATH=..

# Source the password from .env so it never lands in your shell history.
set -a && source ../../.env && set +a
export DATABASE_URL="postgresql://sfo_user_admin:${USER_POSTGRES_PASSWORD}@localhost:5432/sfo_user_core"

uvicorn main:app --reload --port 8001
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

# Restaurant database — restaurants and the kitchen queue
docker exec -it sfo-restaurant-db psql -U sfo_restaurant_admin -d sfo_restaurant_core
#   SELECT order_id, status, created_at FROM order_tickets ORDER BY created_at DESC LIMIT 5;
#   -- capacity is a count of *pending* tickets, so this is what a full kitchen looks like:
#   SELECT r.name, r.capacity, count(t.id) FILTER (WHERE t.status = 'pending') AS on_rail
#     FROM restaurants r LEFT JOIN order_tickets t ON t.restaurant_id = r.id GROUP BY 1, 2;

# Order database — orders (payments are NOT here any more)
docker exec -it sfo-order-db psql -U sfo_order_admin -d sfo_order_core
#   SELECT id, status, rider_id, total_amount FROM orders ORDER BY created_at DESC LIMIT 5;

# Payment database — payments
docker exec -it sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core
#   SELECT order_id, amount, status, transaction_reference FROM payments;

# Order tracking trail — lives beside `orders`, in the same database
#   SELECT seq, old_status, new_status, service, updated_by
#     FROM order_tracking_logs WHERE order_id = '<uuid>' ORDER BY seq;

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
```

`psql` inside the container needs no password (local trust); from a GUI client on your host,
connect to `localhost:5432` / `:5433` / `:5434` / `:5435` / `:5436` with the matching role
and `.env` password.

Joining across services is deliberately impossible now. To follow an order to its customer,
read `customer_id` and call `GET /api/v1/users/{id}` — the same path the services take.

### Resetting the databases

A schema file runs **only** when its own Postgres volume is empty. After editing one:

```bash
docker compose down -v && docker compose up --build -d
```

This wipes all five Postgres volumes plus Redis. To reset a single database,
target its volume — the others keep their data:

```bash
docker compose rm -sf db-order-postgres
docker volume rm smartfoodops-backend_order_postgres_data
docker compose up -d db-order-postgres
```

There is no migration tooling in Week 1 — schema changes mean a volume reset.

### Adding a dependency

Dependencies are pinned inline in each service's `Dockerfile` (no `requirements.txt` in
Week 1). Add the package to that service's `pip install` block and rebuild with
`--build`. Keep versions pinned so local builds stay reproducible.

---

## Project layout

```text
smartfoodops-backend/
├── api-gateway/nginx.conf     # Path-based routing + /health
├── db/                        # One schema per physical database, mounted into its container
│   ├── user/init.sql          # roles (+ seed data), users, riders
│   ├── restaurant/init.sql    # restaurants
│   ├── order/init.sql         # order_status enum, orders
│   └── payment/init.sql       # payment_status enum, payments
├── services/                  # Shared Docker build context
│   ├── common/                # Shared chassis — infrastructure only, no domain code
│   │   ├── config.py          # Env defaults, timeouts, pool bounds
│   │   ├── errors.py          # HTTPException factories (400/403/404/409/422/500/502/503)
│   │   ├── logging_config.py  # Uniform log format
│   │   ├── postgres.py        # PostgresPool: lifespan, cursor, health probe
│   │   └── service_client.py  # Inter-service HTTP + failure translation
│   ├── user/                  # main.py, repository.py, schemas.py           (:8001)
│   ├── restaurant/            # + clients.py (User Service)                  (:8002)
│   ├── menu/                  # + clients.py, cache.py (Redis cache-aside)   (:8003)
│   ├── order/                 # + clients.py, pricing.py (re-pricing rules)  (:8004)
│   └── payment/               # + clients.py, gateway.py, amounts.py         (:8005)
├── scripts/smoke-test.sh      # End-to-end assertions across all five services
├── readme/                    # Blueprints, contracts, and the decision record
├── docker-compose.yml         # Orchestration
├── .gitignore                 # Excludes .env, __pycache__, venvs, OS cruft
└── .env                       # Local environment variables — gitignored, create it yourself
```

A service's schema lives under `db/<service>/`, not next to its code, because it is consumed
by that service's *database container* at first boot — the service image never reads it.

Every service follows the same layering, so any one of them can be read the same way:

| File | Responsibility |
|---|---|
| `main.py` | Wiring and route handlers only — no SQL, no HTTP calls |
| `repository.py` | All database access for the tables this service owns |
| `clients.py` | Outbound calls to sibling services |
| `schemas.py` | Pydantic request/response models (the service's public contract) |
| `pricing.py`, `cache.py`, `amounts.py`, `gateway.py` | Service-specific domain or infrastructure detail |

`services/common/` is a shared *chassis*, not a shared domain. It holds connection
pooling, logging, error mapping, and HTTP transport — the plumbing that would otherwise be
copy-pasted into every new service. Domain models, business rules, and table knowledge
stay inside their owning service, so no service can reason about another's data. Because
all five images need it, the Docker build context is `./services` (not the individual
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
