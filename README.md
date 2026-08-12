# SmartFoodOps — Backend (Week 1)

A containerised, four-service food-ordering backend fronted by an Nginx API gateway.
Everything runs locally through Docker Compose: PostgreSQL, MongoDB, Redis, the gateway,
and the four FastAPI services.

---

## Architecture

```
                       ┌───────────────────────┐
  http://localhost:80  │   Nginx API Gateway   │
  ────────────────────▶│  (path-based routing) │
                       └───────────┬───────────┘
              ┌────────────────┬───┴────────────┬────────────────┐
              ▼                ▼                ▼                ▼
       ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
       │   User     │   │ Restaurant │   │    Menu    │   │   Order    │
       │   :8001    │◀──│   :8002    │◀──│   :8003    │◀──│   :8004    │
       └─────┬──────┘   └─────┬──────┘   └─────┬──────┘   └─────┬──────┘
             │                │            ┌───┴───┐            │
             └────────┬───────┘            ▼       ▼            │
                      ▼                 MongoDB   Redis         │
                  PostgreSQL             :27017   :6379         │
                    :5432 ◀──────────────────────────────────────┘
```

Arrows between services are **HTTP calls, not shared tables**. Each service owns its data:

| Service | Port | Owns | Reaches out to |
|---|---|---|---|
| `user-service` | 8001 | Postgres `users`, `roles` | — |
| `restaurant-service` | 8002 | Postgres `restaurants` | User Service (owner check) |
| `menu-service` | 8003 | Mongo `menus`, `order_tracking_logs` | Restaurant Service (active check) |
| `order-service` | 8004 | Postgres `orders` | Menu Service (pricing + audit log) |

Two rules the code enforces deliberately:

- The Restaurant Service never reads the `users` table — it calls `GET /api/v1/users/{id}`.
- The Order Service never writes to MongoDB — it POSTs to `/api/v1/menus/logs`.

---

## Prerequisites

- Docker Desktop (Compose v2) — `docker compose version`
- `curl` and `python3` for the smoke tests below
- Ports free on the host: **80, 5432, 6379, 27017**
- A `.env` file at the repo root — see below, it is not committed

---

## Environment file

`.env` is listed in [.gitignore](.gitignore) and is **not** committed, so a fresh clone will
not have one. Create it at the repo root before your first run:

```bash
cat > .env <<'EOF'
# Database Credentials
POSTGRES_DB=smartfoodops_core
POSTGRES_USER=sfo_admin
POSTGRES_PASSWORD=<must match db-postgres in docker-compose.yml>
POSTGRES_HOST=db-postgres
POSTGRES_PORT=5432

MONGO_URI=mongodb://db-nosql:27017/smartfoodops_menus
REDIS_URL=redis://cache-redis:6379/0

# Service endpoints (within the Docker network)
USER_SERVICE_URL=http://user-service:8001
RESTAURANT_SERVICE_URL=http://restaurant-service:8002
MENU_SERVICE_URL=http://menu-service:8003
ORDER_SERVICE_URL=http://order-service:8004
EOF
```

`POSTGRES_PASSWORD` is **not** free choice — copy the value `docker-compose.yml` passes to
the `db-postgres` service. The two must agree or a host-run service cannot authenticate
against the container's database. It is not repeated here so this file stays the only place
you read it from.

Where these are actually read matters: `docker-compose.yml` currently **hardcodes** the
Postgres credentials and the `*_SERVICE_URL` values inline rather than interpolating
`${POSTGRES_USER}` / `${POSTGRES_PASSWORD}`, so the stack boots even without a `.env`. The
file is consumed by the host-run flow in
[Running one service on the host](#running-one-service-on-the-host).

`POSTGRES_HOST=db-postgres` is the **Docker DNS name**, reachable only from inside the
Compose network. A service running on your host reaches the same database at
`localhost:5432` — which is why the host-run section builds `DATABASE_URL` by hand instead
of composing it from `POSTGRES_HOST`. The `*_SERVICE_URL` values have the same constraint.

If you change `POSTGRES_PASSWORD`, change it in both places **and** reset the volume with
`docker compose down -v`. Postgres only applies that variable when it initialises an empty
data directory; editing it afterwards has no effect on an existing volume.

Never commit `.env`. The values above are local-laptop defaults only — anything real belongs
in a secrets manager, not in this file.

---

## Quick start

```bash
docker compose up --build -d      # build images and start all 8 containers
docker compose ps                 # all should read "Up" / "healthy"
```

First boot takes a few minutes while the Python images build. Postgres runs
[init.sql](init.sql) automatically on the **first** boot of its volume — see
[Resetting the databases](#resetting-the-databases) if you change the schema.

### Verify everything routes

```bash
for p in /health /api/v1/users/health /api/v1/restaurants/health \
         /api/v1/menus/health /api/v1/orders/health; do
  printf '%-30s ' "$p"; curl -s -w ' [%{http_code}]\n' "http://localhost$p"
done
```

All five must return `200`. The service health endpoints also report whether their
backing stores actually round-trip (`database_reachable`, `mongo_reachable`, `redis_reachable`)
— a `200` with `"database_reachable": false` means the app is up but the DB is not.

### Shut down

```bash
docker compose down          # stop containers, keep data
docker compose down -v       # stop and wipe all volumes (fresh databases)
```

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

**2 — Onboard a restaurant** (owner must resolve to `restaurant_admin`)

```bash
curl -s -X POST http://localhost/api/v1/restaurants/onboard \
  -H 'Content-Type: application/json' \
  -d '{"owner_id":"<OWNER_UUID>","name":"SFO Diner","address":"12 Blue Area, Islamabad",
       "latitude":33.6844,"longitude":73.0479,"capacity":40}'
```

**3 — Publish a menu** (upsert — re-posting replaces the tree and keeps `created_at`)

```bash
curl -s -X POST http://localhost/api/v1/menus \
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

**4 — Place an order** (the header is mandatory)

```bash
curl -s -X POST http://localhost/api/v1/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: sfo-key-0001' \
  -d '{"customer_id":"<CUSTOMER_UUID>","restaurant_id":"<RESTAURANT_UUID>",
       "items":[{"item_id":"item_burger_001","quantity":2,
                 "customizations":{"grp_add_ons":["Extra Cheddar Cheese"]}}],
       "total_amount":28.98}'
```

The total is **recalculated server-side** from the live menu (12.99 + 1.50 × 2 = 28.98) —
the client's `total_amount` is only checked, never trusted. Repeating the same
`X-Idempotency-Key` returns the original order as `200` instead of creating a second one.

### Endpoint reference

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Gateway only, does not touch services |
| `GET` | `/api/v1/{users,restaurants,menus,orders}/health` | Per-service + backing store |
| `POST` | `/api/v1/users/register` | `201`; bcrypt hash, role resolved via DB |
| `GET` | `/api/v1/users/{user_id}` | Joins `roles`, returns the role **name** |
| `POST` | `/api/v1/restaurants/onboard` | `201`; verifies owner over HTTP |
| `GET` | `/api/v1/restaurants/{restaurant_id}` | Exposes `is_active` to other services |
| `POST` | `/api/v1/menus` | Upsert full category/item/customization tree |
| `GET` | `/api/v1/menus/{restaurant_id}` | Used by the Order Service to price a cart |
| `POST` | `/api/v1/menus/logs` | Appends to `order_tracking_logs.status_history` |
| `POST` | `/api/v1/orders` | `201` new / `200` idempotent replay |

Interactive docs per service, once you expose a port (see below): `http://localhost:<port>/docs`.

### Error contract

| Code | When |
|---|---|
| `400` | Unknown role name; missing `X-Idempotency-Key` |
| `403` | Owner exists but is not a `restaurant_admin` |
| `404` | Unknown user / restaurant / menu; inactive restaurant |
| `409` | Duplicate email (case-insensitive) or phone |
| `422` | Pydantic validation; `min_selection > max_selection`; unavailable or off-menu item; total mismatch |
| `500` | Postgres connection pool starved |
| `502` | Unexpected response from an upstream service |
| `503` | Upstream service unreachable; MongoDB socket timeout |

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

# Substitute the database username and password before running this line.
# Take them from .env (POSTGRES_USER / POSTGRES_PASSWORD) — they are not repeated here.
export DATABASE_URL=postgresql://<DB_USER>:<DB_PASSWORD>@localhost:5432/smartfoodops_core

uvicorn main:app --reload --port 8001
```

If you would rather not type the password into your shell (it lands in your history),
source it from the env file instead:

```bash
set -a && source ../../.env && set +a
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}"
```

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

```bash
# PostgreSQL
docker exec -it sfo-postgres psql -U sfo_admin -d smartfoodops_core
#   \dt              list tables
#   SELECT u.email, r.name FROM users u JOIN roles r ON r.id = u.role_id;

# MongoDB
docker exec -it sfo-mongodb mongosh smartfoodops_menus
#   db.menus.find().pretty()
#   db.order_tracking_logs.find().pretty()

# Redis
docker exec -it sfo-redis redis-cli ping
```

### Resetting the databases

`init.sql` runs **only** when the Postgres data volume is empty. After editing it:

```bash
docker compose down -v && docker compose up --build -d
```

This wipes Postgres, Mongo, and Redis. There is no migration tooling in Week 1 — schema
changes mean a volume reset.

### Adding a dependency

Dependencies are pinned inline in each service's `Dockerfile` (no `requirements.txt` in
Week 1). Add the package to that service's `pip install` block and rebuild with
`--build`. Keep versions pinned so local builds stay reproducible.

---

## Project layout

```text
smartfoodops-backend/
├── api-gateway/nginx.conf     # Path-based routing + /health
├── services/                  # Shared Docker build context
│   ├── common/                # Shared chassis — infrastructure only, no domain code
│   │   ├── config.py          # Env defaults, timeouts, pool bounds
│   │   ├── errors.py          # HTTPException factories (400/403/404/409/422/500/502/503)
│   │   ├── logging_config.py  # Uniform log format
│   │   ├── postgres.py        # PostgresPool: lifespan, cursor, health probe
│   │   └── service_client.py  # Inter-service HTTP + failure translation
│   ├── user/                  # main.py, repository.py, schemas.py           (:8001)
│   ├── restaurant/            # + clients.py (User Service)                  (:8002)
│   ├── menu/                  # + clients.py, datastores.py (Mongo/Redis)    (:8003)
│   └── order/                 # + clients.py, pricing.py (re-pricing rules)  (:8004)
├── readme/                    # Week 1 blueprints and contracts
├── docker-compose.yml         # Orchestration
├── init.sql                   # Postgres DDL + role seed data
├── .gitignore                 # Excludes .env, __pycache__, venvs, OS cruft
└── .env                       # Local environment variables — gitignored, create it yourself
```

Every service follows the same layering, so any one of them can be read the same way:

| File | Responsibility |
|---|---|
| `main.py` | Wiring and route handlers only — no SQL, no HTTP calls |
| `repository.py` | All datastore access for the tables/collections this service owns |
| `clients.py` | Outbound calls to sibling services |
| `schemas.py` | Pydantic request/response models (the service's public contract) |
| `pricing.py`, `datastores.py` | Service-specific domain or infrastructure detail |

`services/common/` is a shared *chassis*, not a shared domain. It holds connection
pooling, logging, error mapping, and HTTP transport — the plumbing that would otherwise be
copy-pasted into every new service. Domain models, business rules, and table knowledge
stay inside their owning service, so no service can reason about another's data. Because
all four images need it, the Docker build context is `./services` (not the individual
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
| `database_reachable: false` | Postgres still starting, or the volume is mid-init. Re-check after ~10s |
| Schema changes not visible | `init.sql` only runs on an empty volume — `docker compose down -v` |
| `503` on menu writes | MongoDB unreachable. `docker compose ps db-nosql` |
| `409` on a repeated register | Working as intended — email/phone are unique |
| Edits do nothing | Image is stale. `docker compose up -d --build <service>`, or use the hot-reload override |

---

## Notes and known deviations

The blueprint's shared Dockerfile hardcodes port 8000, but `nginx.conf` proxies to
8001–8004. Each Dockerfile therefore binds its own service's port — a literal copy of the
blueprint would make every route a 502.

`UserRegisterRequest.role` is a plain `str` rather than the `UserRole` enum, so an unknown
role is rejected with `400` (per the Week 1 spec) instead of Pydantic's `422`, with the
`roles` table as the single source of truth. `UserRole` remains defined in
[services/user/schemas.py](services/user/schemas.py) for reference.

`USER_SERVICE_URL` and `RESTAURANT_SERVICE_URL` are only set on `order-service` in
`docker-compose.yml`; the other services fall back to the in-network defaults in code.
Setting them in Compose later overrides cleanly.

Audit logging is best-effort. The order is already committed when the log call fires, so a
Menu Service failure is logged loudly rather than returned as a `500` — a `500` there would
tell the client the order failed when it exists.

Two additions beyond the v6 contracts, both required by the flow:
`GET /api/v1/menus/{restaurant_id}` (the Order Service cannot price a cart without it) and
`OrderItemSnapshot`, an `OrderItemSelection` subclass carrying `unit_price` / `line_total` /
`selected_options` so the JSONB snapshot survives into the response.

**Credentials in this repo are local development defaults only.** They live in
`docker-compose.yml` and `.env` for convenience on a laptop. Do not reuse them anywhere
else, and move real values to a secrets manager before this leaves a local environment.
`.env` is gitignored, but `docker-compose.yml` still carries its Postgres password inline —
switching Compose to `${POSTGRES_USER}` / `${POSTGRES_PASSWORD}` interpolation would leave
`.env` as the single place credentials live.
