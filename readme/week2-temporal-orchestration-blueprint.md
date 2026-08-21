# SmartFoodOps — Week 2 End-to-End Temporal Orchestration & Logistics Blueprint

**Revision 2 — corrected against the shipped Week 1 platform.**

Revision 1 of this document presented itself as "complete, copy-pasteable" code. It was
neither. Measured against what actually shipped — five services on per-service PostgreSQL,
RS256 authentication, and the D22–D24 MongoDB→Postgres migration — pasting it would have
regressed the platform in six recorded ways and would not have started at all in four more.
Section 0 lists every departure, because a blueprint followed where it is wrong produces a
broken system and one departed from silently produces an unreviewable one (D21).

Everything asserted about container images, CLI flags, mount points and SDK APIs in this
revision was executed and confirmed, not recalled. Where a claim was checked, the check is
named.

> **Read [key-decisions.md](key-decisions.md) first.** This document is the *how*; the
> decisions it implements — and the ones Revision 1 would have broken — are the *why*.

> **Amended after shipping, by D32.** Sections below describe an `order_tickets` table in
> `sfo_restaurant_core` holding the kitchen's queue. That table no longer exists. The
> kitchen's decision is `orders.kitchen_decision` in the Order Service's own database, its
> queue is a query over `orders`, and the saga makes **zero** HTTP calls to the Restaurant
> Service (it was four). The signal-and-timer mechanism this document specifies is unchanged
> and still correct; only the location of the decision moved. For the shipped design read
> [order-saga-orchestration-guide.md](order-saga-orchestration-guide.md), and D32 for why.

---

## Section 0: Deviations from Revision 1

### 0.1 Changes that prevent a regression

| Rev 1 | Why it had to change |
|---|---|
| §7 put `customer_id` back in the `POST /api/v1/orders` request body, with no auth dependency on the handler. | **Reopens the impersonation hole D13 closed.** While identity arrives in the body, every role check is honest but meaningless. `customer_id` stays out of the schema; the customer is the verified token subject. |
| §7 generated a UUID and started a workflow without inserting the order. | Breaks D06 (the server re-prices every cart), D08 (idempotency is enforced by a unique index), and D24 (`order_tracking_logs.order_id` is a real foreign key, so the opening log insert would be rejected). `GET /api/v1/orders/{id}` would 404, which breaks the Payment Service. `create_order` keeps every step it has today; the workflow starts *after* the commit. |
| §7 returned `{status, workflow_id, run_id}`. | Not `OrderResponse`. Breaks the Payment Service's `fetch_order` and the smoke test. The response model is unchanged; new fields are additive only. |
| §7 wrapped everything in `except Exception` → `500`. | Violates D05 — status codes are chosen once, in `common/errors.py`. |
| §2 restated the `order_tracking_logs` DDL. | **That DDL is stale — it predates D24.** It omits `seq BIGSERIAL`, `service`, `updated_by` and `raw_log`, and indexes `(order_id, created_at DESC)` where the shipped table indexes `(order_id, seq DESC)` — `created_at` cannot order two entries written in one transaction. Applying it would regress the table. It is already correct; do not touch it. |
| §3 hardcoded `sfo_order_admin:sfo_order_password_123` into a module-level DSN. | Violates D19 — secrets live only in a gitignored `.env`, named once. Use `common.config.required("DATABASE_URL")`. |
| §5 opened `sfo_user_core` with `sfo_user_admin`'s credentials from a *different* service. | Violates D01 outright. A credential boundary is the entire reason each service has its own database. Riders get their own service and their own database. |
| §3 replaced psycopg2 with SQLAlchemy async + asyncpg. | D21 already rejected this exact substitution for the menu migration: it would make the Order Service the only one in the platform with a second way to reach a database. Activities are **sync** functions on the existing `PostgresPool`, run by the Worker on a `ThreadPoolExecutor`. |
| §3 had the workflow supply `old_status`. | Contradicts D24 — `_INSERT_LOG` *derives* the previous status from the preceding entry so the chain cannot disagree with itself. The activity passes only `new_status`. |

### 0.2 Code in Revision 1 that does not run

| Rev 1 | Verified reality |
|---|---|
| `image: temporalio/dev:1.1.1` | No such image. `docker pull` fails. |
| `DB=sqlite`, `BIND_ON_IP=0.0.0.0`, `TEMPORAL_CLI_SHOW_STACKS` env vars | Those are `temporalio/auto-setup`'s variable names, and it needs an external database. Ports 7233/8233 match `temporal server start-dev`, so the intent was the dev server — which is configured by **flags, not environment variables**. |
| `volumes: - temporal_data:/var/lib/temporal` | **Fails.** The image runs as non-root `temporal` (uid 1000, confirmed via `docker run --entrypoint sh`), and a fresh named volume at a path the image does not own is root-owned. Observed: `unable to create SQLite admin DB: unable to open database file: out of memory (14)` — SQLite's `CANTOPEN` code, not a memory problem. Mount at `/home/temporal`, which the image *does* own, and the volume inherits uid 1000. Confirmed working. |
| `healthcheck: nc -z 127.0.0.1 7233` | `nc` is not in the image. Use the `temporal` binary, which is the entrypoint. Confirmed `exit 0`. |
| `retry_policy={"initial_interval": ..., }` as a `dict` | The SDK requires `temporalio.common.RetryPolicy`, a dataclass. Confirmed fields: `initial_interval`, `backoff_coefficient`, `maximum_interval`, `maximum_attempts`, `non_retryable_error_types`. |
| `from services.order.activities import OrderActivities` (§4, §6) | Dockerfiles `COPY <svc>/ /app/`, so service code sits at `/app` root. The import is `from activities import …`. Rev 1 would `ModuleNotFoundError` in both the workflow and the worker. |
| `json.dumps(metadata)` in §3 | `json` is never imported. Hard `NameError` on first use. |
| `POST /api/v1/payments/authorize`, `POST /api/v1/payments/refund` | Neither exists. The real endpoint is `POST /api/v1/payments`, and there is **no refund path anywhere** in the platform. Both are built here. |
| `POST /api/v1/restaurant/orders` returning `{"accepted": bool}` | Does not exist — no kitchen queue, no ticket table, and nothing anywhere reads `restaurants.capacity`. The prefix is also wrong (`/api/v1/restaurants`). Built here as a ticket table with a real decision flow. |
| §5 `rider-service` on **port 8004** | Collides with `order-service`. Riders are on **8006**. |
| §5 `restaurant_lat = 33.6844` hardcoded | `restaurants.latitude`/`longitude` are `NOT NULL` and already returned by `GET /api/v1/restaurants/{id}`. |
| `item.dict()` (§7) | Pydantic v1. This repo is v2 — `model_dump()`. |
| `Client.connect()` inside the request handler (§7) | One TCP connect and handshake per order. Lifespan-managed instead, composed with the existing `db.lifespan`. |

### 0.3 Places Revision 1 under-used Temporal

These are not typos; they are the difference between using Temporal and using it as an
HTTP retry loop.

| Rev 1 | Corrected |
|---|---|
| Restaurant rejection raised `ValueError` **inside a 3-attempt retry policy**. | Temporal retries every exception except `ApplicationError(non_retryable=True)`. So a kitchen declining an order was retried three times before compensating. Business outcomes are now non-retryable; only transport failures retry. |
| Restaurant acceptance was a synchronous HTTP call returning `{"accepted": bool}`. | A real kitchen accepts when a human presses a button. That is a **signal plus a durable timer** (`workflow.wait_condition`), which is the pattern Week 2 exists to teach. |
| `start_to_close_timeout=timedelta(seconds=120)` described as the "2 minute rider allocation window". | Wrong semantics — that bounds a *single activity attempt*. A search window is a workflow-level loop over durable timers. |
| Workflow ended at `assigned`. | `picked_up` and `delivered` are in the `order_status` enum and in the Week 2 goal. Rev 1 also never released a claimed rider, so **every dispatched rider leaked `is_available = FALSE` permanently**, including on the compensation paths. |
| `refund_payment_activity` sent no idempotency key. | Temporal retries activities. That is a double refund. Every activity now carries a deterministic, workflow-derived key. |
| `UPDATE orders SET status = :status` unguarded. | A retried or out-of-order activity could walk `delivered` back to `assigned`. Now a compare-and-set. |
| Python-side Haversine over every available rider, then `409` on a lost race. | One SQL statement with `ORDER BY … LIMIT 1 FOR UPDATE SKIP LOCKED`. The race is **prevented**, not detected and retried. |
| No auth on `/api/v1/riders/dispatch`. | Anyone could claim riders. Internal-key only. |
| `worker.py` was provided but never scheduled. | It gets its own container. |

### 0.4 Consequence to accept before starting

Once the workflow owns payment authorisation, the customer-facing `POST /api/v1/payments`
stops being how an order gets paid. `payments` has `UNIQUE (order_id)`, so a client calling
it after the workflow has authorised hits the existing duplicate handler and receives
`409 "Order X has already been paid for"`. That is correct behaviour, but it is a **change to
a Week 1 contract** — recorded as a decision, not absorbed silently. The endpoint remains for
direct and manual use.

---

## Section 1: Architectural Topology

Under peak traffic we do not chain synchronous HTTP calls across services. A stateful
Temporal workflow coordinates state transitions, timing boundaries and compensations across
the isolated per-service databases.

```
POST /api/v1/orders          (customer bearer token)
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Order Service :8004                                          │
│  re-price against live menu (D06)                            │
│  verify customer + restaurant over HTTP (D02)                │
│  INSERT orders + opening 'created' log   ── one txn (D24)     │
│  start_workflow(id="order-{uuid}")       ── after commit     │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ OrderWorkflow          task queue: "order-tasks"             │
│                                                              │
│  authorize_payment ─────────────────────────► confirmed      │
│  send_ticket ──► await restaurant signal (durable timer)     │
│  dispatch_rider ──► retry loop over durable timers           │
│                                             ► assigned       │
│  await rider_pickup signal ─────────────────► picked_up      │
│  await rider_delivery signal ───────────────► delivered      │
│                                                              │
│  compensation on any failure past payment:                   │
│      refund_payment → release_rider → cancelled              │
└──────────────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────┐   ┌────────────────┐   ┌────────────────┐
│ Payment :8005 │   │ Restaurant     │   │ Rider :8006    │
│ sfo_payment   │   │ :8002          │   │ sfo_rider_core │
│ _core         │   │ sfo_restaurant │   │ (NEW)          │
└───────────────┘   │ _core          │   └────────────────┘
                    └────────────────┘
        ▲                    ▲                    ▲
        └──────── all reached with X-Internal-Key ┘
                  (never a forwarded bearer token)

Signals travel inward through exactly one door:
    POST /api/v1/orders/{order_id}/signals   (internal key)
The Order Service relays them into Temporal, so a Temporal client
lives in exactly two processes: order-service and order-worker.
```

### 1.1 Why the worker uses an internal key, not the customer's token

Every existing cross-service call forwards the caller's bearer token, so a service can never
do more than the user who invoked it (D15). The saga cannot work that way, for two
independent reasons:

1. **Access tokens live 15 minutes** (`ACCESS_TOKEN_TTL_MINUTES`). A saga that waits on a
   kitchen and then searches for a rider routinely outlives that, and there is no refresh
   path a workflow could take.
2. **A workflow argument is durable history.** Anything passed to `start_workflow` is
   persisted by Temporal and rendered in the Web UI. Putting a bearer token there writes a
   live credential into a log.

So the worker calls internal-key-guarded endpoints. This is the moment D15's own caveat fires
— "if internal-only endpoints multiply, per-service keypairs become the better answer" — and
that debt is recorded rather than left implicit.

---

## Section 2: Docker Compose additions

Add to the **existing** `docker-compose.yml`. The `networks:` and `volumes:` blocks are
already declared at the top of that file — **add the new entries to them, do not paste new
blocks**, which is what Revision 1's standalone snippet would have done.

Every value below was verified by running the container.

```yaml
# --- add to the existing top-level volumes: block ---
volumes:
  rider_postgres_data:
  temporal_data:

# --- add beside the other x-*-db-env anchors, following the D19 pattern ---
x-rider-db-env: &rider-db-env
  DATABASE_URL: postgresql://sfo_rider_admin:${RIDER_POSTGRES_PASSWORD:?RIDER_POSTGRES_PASSWORD is required}@db-rider-postgres:5432/sfo_rider_core

services:
  # ==============================================================
  # RIDER DATABASE  (new — D01: riders get their own credentials)
  # ==============================================================
  db-rider-postgres:
    <<: *postgres-base
    container_name: sfo-rider-db
    environment:
      POSTGRES_DB: sfo_rider_core
      POSTGRES_USER: sfo_rider_admin
      POSTGRES_PASSWORD: ${RIDER_POSTGRES_PASSWORD}
    ports:
      - "5437:5432"
    volumes:
      - rider_postgres_data:/var/lib/postgresql/data
      - ./db/rider/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  # ==============================================================
  # TEMPORAL DEVELOPMENT ORCHESTRATOR
  #
  # temporalio/temporal is the CLI image; its entrypoint is the
  # `temporal` binary, so the dev server is started by `command`.
  # It is configured by FLAGS, not environment variables.
  #
  #   --ip defaults to "localhost", which is unreachable from
  #   sibling containers, so 0.0.0.0 is mandatory. --ui-ip
  #   defaults to --ip, so the UI is covered by the same flag.
  #
  #   --db-filename lives under /home/temporal, NOT /var/lib/temporal:
  #   the image runs as non-root uid 1000, and a named volume at a
  #   path the image does not own is created root-owned, which fails
  #   with SQLite CANTOPEN. /home/temporal is owned by uid 1000, so
  #   the volume inherits it.
  #
  #   --metrics-port is pinned rather than left random so Week 3's
  #   Prometheus has a stable scrape target.
  # ==============================================================
  temporal-server:
    image: temporalio/temporal:1.8.2
    container_name: sfo-temporal-server
    restart: always
    command:
      - server
      - start-dev
      - --ip
      - 0.0.0.0
      - --port
      - "7233"
      - --ui-port
      - "8233"
      - --metrics-port
      - "9233"
      - --db-filename
      - /home/temporal/temporal.db
    ports:
      - "7233:7233"   # gRPC workflow API
      - "8233:8233"   # Web UI
      - "9233:9233"   # Prometheus metrics (Week 3)
    volumes:
      - temporal_data:/home/temporal
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "temporal", "operator", "namespace", "list", "--address", "127.0.0.1:7233"]
      interval: 5s
      timeout: 5s
      retries: 10

  # ==============================================================
  # RIDER SERVICE (new)
  # ==============================================================
  rider-service:
    build:
      context: ./services
      dockerfile: rider/Dockerfile
    container_name: sfo-rider-service
    restart: always
    environment:
      <<: [*rider-db-env, *jwt-env]
      USER_SERVICE_URL: http://user-service:8001
      RESTAURANT_SERVICE_URL: http://restaurant-service:8002
      ORDER_SERVICE_URL: http://order-service:8004
    depends_on:
      db-rider-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # ==============================================================
  # TEMPORAL WORKER
  #
  # Same image as order-service — the workflow and its activities
  # live in that service's codebase, because the order lifecycle is
  # its fact. Only the command differs.
  # ==============================================================
  order-worker:
    build:
      context: ./services
      dockerfile: order/Dockerfile
    container_name: sfo-order-worker
    restart: always
    command: ["python", "worker.py"]
    environment:
      <<: [*order-db-env, *jwt-env]
      TEMPORAL_ADDRESS: temporal-server:7233
      USER_SERVICE_URL: http://user-service:8001
      RESTAURANT_SERVICE_URL: http://restaurant-service:8002
      MENU_SERVICE_URL: http://menu-service:8003
      PAYMENT_SERVICE_URL: http://payment-service:8005
      RIDER_SERVICE_URL: http://rider-service:8006
    depends_on:
      db-order-postgres:
        condition: service_healthy
      temporal-server:
        condition: service_healthy
    networks:
      - smartfoodops-network
```

Also amend the existing entries:

- **`order-service`** — add `TEMPORAL_ADDRESS: temporal-server:7233`, and
  `temporal-server: {condition: service_healthy}` to `depends_on`.
- **`payment-service`** — nothing new; it already has `ORDER_SERVICE_URL`.
- **`api-gateway`** — add `rider-service` to `depends_on`.

### 2.1 nginx

```nginx
location /api/v1/riders {
    proxy_pass http://rider-service:8006;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

The Temporal Web UI is deliberately **not** proxied. It is an operator tool with no
authentication, reached directly on `http://localhost:8233`; putting it behind the public
gateway would publish every workflow's history.

### 2.2 `.env` and the bootstrap script

Add `RIDER_POSTGRES_PASSWORD` and `RIDER_SERVICE_URL`.

> **D20 — mirror everything above into `scripts/init_bootstrap.sh`.** That script regenerates
> `.env`, every `init.sql`, `docker-compose.yml` and `nginx.conf` byte-for-byte, and it stays
> trustworthy only while it matches. This is the easiest thing in the repo to forget. Verify
> with a `diff` of every generated artifact against the working tree.

> **Breaking change:** moving `riders` out of `sfo_user_core` (Section 3.2) changes an
> already-initialised database. `init.sql` runs only on an empty data directory, so this
> requires `docker compose down -v`.

---

## Section 3: Schema changes

### 3.1 `order_tracking_logs` — do not touch

Revision 1's DDL for this table is stale and applying it would regress D24. The shipped table
already has everything the saga needs: the real foreign key to `orders(id)`, the shared
`order_status` enum on `new_status`, `seq BIGSERIAL` for intra-transaction ordering,
`service`, `updated_by`, `raw_log`, and `metadata JSONB` for rider ids, ETAs and coordinates.
No change.

### 3.2 `db/user/init.sql` — remove `riders`

Delete the `riders` table and `idx_riders_availability`. The table ships there today with no
code behind it; it moves to its own database.

### 3.3 `db/rider/init.sql` — new

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Distance is needed inside the dispatch query's ORDER BY, so it is expressed in SQL
-- rather than in Python. This is the platform's first database function; it is plain
-- `LANGUAGE sql` and IMMUTABLE rather than PL/pgSQL, so the planner can inline it and a
-- functional index on it stays possible if dispatch ever outgrows a sequential scan.
CREATE OR REPLACE FUNCTION haversine_km(
    lat1 DOUBLE PRECISION, lon1 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION, lon2 DOUBLE PRECISION
) RETURNS DOUBLE PRECISION AS $$
    SELECT 6371.0 * 2 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2))
        * power(sin(radians(lon2 - lon1) / 2), 2)
    ));
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

CREATE TABLE IF NOT EXISTS riders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Points at users(id) in sfo_user_core. No foreign key can follow it across a
    -- database, so the User Service verifies it over HTTP before the insert (D02).
    user_id UUID UNIQUE NOT NULL,
    vehicle_type VARCHAR(100) NOT NULL,
    vehicle_number VARCHAR(100) UNIQUE NOT NULL,
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    current_latitude DECIMAL(9, 6),
    current_longitude DECIMAL(9, 6),
    -- The order this rider is currently carrying. Two things depend on it: dispatch is
    -- idempotent because a retry finds the order already held, and pickup/delivery can be
    -- authorised without asking the Order Service who was assigned.
    current_order_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Only rows that can actually be dispatched. Partial, because the unavailable half of the
-- fleet is never a candidate and does not belong in the index.
CREATE INDEX IF NOT EXISTS idx_riders_dispatchable
    ON riders (is_available)
    WHERE is_available AND current_latitude IS NOT NULL AND current_longitude IS NOT NULL;

-- One rider per order, enforced by the engine rather than by a check in Python. This is
-- what makes a retried dispatch activity unable to claim a second rider.
CREATE UNIQUE INDEX IF NOT EXISTS idx_riders_current_order
    ON riders (current_order_id)
    WHERE current_order_id IS NOT NULL;
```

### 3.4 `db/restaurant/init.sql` — add the kitchen queue

```sql
CREATE TYPE ticket_status AS ENUM ('pending', 'accepted', 'rejected', 'expired');

CREATE TABLE IF NOT EXISTS order_tickets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Points into sfo_order_core; unique, so a retried send_ticket activity is a no-op.
    order_id UUID UNIQUE NOT NULL,
    -- Both ends of this one live in this database, so unlike every other cross-service
    -- reference in the platform it gets a real foreign key — the same argument D24 made
    -- for moving the tracking trail next to the orders it describes.
    restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    status ticket_status NOT NULL DEFAULT 'pending',
    decided_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Serves the admin's queue read and the capacity count, which both filter on
-- (restaurant_id, status) and order by arrival.
CREATE INDEX IF NOT EXISTS idx_tickets_queue
    ON order_tickets (restaurant_id, status, created_at);
```

`restaurants.capacity` has existed since Week 1 and nothing has ever read it. It becomes the
depth of the pending queue.

### 3.5 `db/payment/init.sql` — no change

`payment_status` already contains `refunded`. The refund path is code, not DDL.

---

## Section 4: Shared chassis additions

The saga must not introduce a second way to do anything the platform already does once
(D04). Three small additions to `services/common/`, and nothing else.

### 4.1 `ServiceClient` gains POST

Every cross-service call in the platform today is a GET, so `common/service_client.py`
exposes only `get` and `aget`. Activities need POST. It goes here, not into `activities.py`
with a hand-rolled `httpx` client, so the D05 failure contract stays in one place.

```python
# services/common/service_client.py — additions

def post(
    self,
    path: str,
    *,
    json: dict,
    missing: str,
    unreachable_hint: str,
    bad_gateway_hint: str | None = None,
    missing_error: MissingError = not_found,
    headers: dict | None = None,
) -> dict:
    """Blocking POST returning the decoded JSON body.

    Same status mapping as `get`: the point of routing writes through here rather than
    calling httpx directly is that a dependency being down looks like 503 no matter which
    verb reached it.
    """
    url = self._url(path)
    try:
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, json=json, headers=headers)
    except httpx.RequestError as exc:
        raise self._unreachable(url, exc, unreachable_hint) from exc
    return self._payload(
        response,
        missing=missing,
        missing_error=missing_error,
        bad_gateway_hint=bad_gateway_hint,
    )
```

…plus the `async def apost` counterpart, mirroring `aget`.

`_payload` currently treats anything other than `200` as a failure. Creation endpoints in
this platform answer `201`, so it must accept both:

```python
    if response.status_code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
```

### 4.2 `common/config.py` additions

The saga's timing constants belong in one place, so the workflow and the smoke test read the
same numbers.

```python
DEFAULT_PAYMENT_SERVICE_URL = "http://payment-service:8005"
DEFAULT_RIDER_SERVICE_URL = "http://rider-service:8006"
DEFAULT_TEMPORAL_ADDRESS = "temporal-server:7233"

ORDER_TASK_QUEUE = "order-tasks"

# How long a kitchen has to answer before the order is cancelled and refunded. Generous,
# because the alternative is cancelling orders a busy restaurant would have accepted.
RESTAURANT_DECISION_TIMEOUT_SECONDS = 120

# Rider search: repeated attempts separated by durable timers, not one long activity.
RIDER_SEARCH_ATTEMPTS = 6
RIDER_SEARCH_INTERVAL_SECONDS = 10
RIDER_MAX_DISTANCE_KM = 10.0

# A delivery is the one leg with a human walking around in it, so its bound is hours.
DELIVERY_TIMEOUT_SECONDS = 3600
```

### 4.3 `common/temporal.py` — new

`PostgresPool` exists so five services do not each write their own connection handling.
Temporal gets the same treatment rather than a `Client.connect()` in two `main.py`s.

```python
"""Temporal client plumbing, shared by the Order Service and its worker.

Mirrors what PostgresPool does for Postgres: one place that knows how the connection is
made, exposes a FastAPI lifespan, and can answer whether the dependency is reachable
without raising. Revision 1 of the Week 2 blueprint called Client.connect() inside the
request handler, which pays for a TCP connect and handshake on every order.
"""

from contextlib import asynccontextmanager
from logging import Logger

from temporalio.client import Client
from temporalio.service import RPCError

from common.config import DEFAULT_TEMPORAL_ADDRESS


class TemporalGateway:
    """Lazily-connected Temporal client with a lifespan and a health probe."""

    def __init__(self, address: str, *, logger: Logger):
        self.address = address
        self._logger = logger
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            # Raised rather than reconnected on the spot: a handler reaching this means the
            # lifespan did not run, which is a wiring bug and not a transient failure.
            raise RuntimeError("Temporal client is not initialised")
        return self._client

    async def connect(self) -> Client:
        self._client = await Client.connect(self.address)
        self._logger.info("Connected to Temporal at %s", self.address)
        return self._client

    @asynccontextmanager
    async def lifespan(self, _=None):
        """FastAPI lifespan. Composed with PostgresPool.lifespan via AsyncExitStack.

        A failure to reach Temporal at startup is logged and swallowed: orders must still
        be creatable and readable when the orchestrator is down. The workflow start is what
        fails then, and it is repaired by an idempotent retry — see the Order Service.
        """
        try:
            await self.connect()
        except Exception as exc:  # noqa: BLE001 - startup must not be fatal
            self._logger.error("Temporal unreachable at %s: %s", self.address, exc)
        yield

    async def is_reachable(self) -> bool:
        """Never raises — this answers a health endpoint."""
        if self._client is None:
            return False
        try:
            await self._client.service_client.check_health()
            return True
        except (RPCError, OSError) as exc:
            self._logger.warning("Temporal health check failed: %s", exc)
            return False
```

### 4.4 Dockerfiles

- `services/order/Dockerfile` — add `temporalio==1.31.0` to the existing inline `pip install`.
  Confirmed to install on `python:3.11-slim` (it ships a prebuilt Rust core; no toolchain
  needed). SDK 1.31.0 against server 1.31.2.
- `services/rider/Dockerfile` — new, copying the existing five verbatim: `python:3.11-slim`,
  `fastapi`, `uvicorn`, `psycopg2-binary`, `PyJWT[crypto]`, `pydantic`, `httpx`, `EXPOSE 8006`.

---

## Section 5: Rider Service (`services/rider/`, port 8006)

Same five-file shape as every other service: `main.py`, `repository.py`, `schemas.py`,
`clients.py`, `Dockerfile`.

| Method | Path | Guard |
|---|---|---|
| `GET` | `/api/v1/riders/health` | none |
| `POST` | `/api/v1/riders` | `require_role("rider")`; `user_id` from the token (D13), live role re-checked over HTTP (D18) |
| `GET` | `/api/v1/riders/me` | `require_role("rider")` |
| `PATCH` | `/api/v1/riders/me/location` | `require_role("rider")` |
| `POST` | `/api/v1/riders/me/orders/{order_id}/picked-up` | `require_role("rider")` + holds this order |
| `POST` | `/api/v1/riders/me/orders/{order_id}/delivered` | `require_role("rider")` + holds this order |
| `POST` | `/api/v1/riders/dispatch` | `require_internal` — the worker only |
| `POST` | `/api/v1/riders/release` | `require_internal` — compensation |

### 5.1 Dispatch is one statement

```sql
-- repository.py
_CLAIM_NEAREST = """
    UPDATE riders
       SET is_available = FALSE,
           current_order_id = %(order_id)s,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = (
             SELECT id
               FROM riders
              WHERE is_available
                AND current_order_id IS NULL
                AND current_latitude IS NOT NULL
                AND current_longitude IS NOT NULL
                AND haversine_km(current_latitude::double precision,
                                 current_longitude::double precision,
                                 %(lat)s, %(lon)s) <= %(max_km)s
              ORDER BY haversine_km(current_latitude::double precision,
                                    current_longitude::double precision,
                                    %(lat)s, %(lon)s)
              LIMIT 1
              FOR UPDATE SKIP LOCKED
           )
    RETURNING id,
              user_id,
              haversine_km(current_latitude::double precision,
                           current_longitude::double precision,
                           %(lat)s, %(lon)s) AS distance_km
"""
```

`FOR UPDATE SKIP LOCKED` is the whole point. Revision 1 read every available rider into
Python, picked the nearest, tried to claim it, and raised `409` when a concurrent workflow had
already taken it — detecting a race and pushing the retry back to the caller. Here a
concurrent transaction simply *skips* the locked row and claims the next-nearest, so two
simultaneous dispatches for different orders both succeed on the first attempt and can never
select the same rider.

**Idempotency** comes first, because Temporal will retry this activity:

```python
def dispatch(self, order_id: UUID, lat: float, lon: float, max_km: float) -> dict | None:
    """Claim the nearest available rider, or return the one already carrying this order.

    Both branches share a transaction. The prior-claim check is not an optimisation: a
    retried activity that skipped it would claim a second rider and strand the first,
    which is exactly the leak the partial unique index on current_order_id refuses.
    """
    with self._db.cursor(commit=True) as cur:
        cur.execute(_SELECT_BY_ORDER, (str(order_id),))
        held = cur.fetchone()
        if held is not None:
            return held
        cur.execute(_CLAIM_NEAREST, {
            "order_id": str(order_id), "lat": lat, "lon": lon, "max_km": max_km,
        })
        return cur.fetchone()
```

A `None` return means "nobody available", and the endpoint answers `200 {"assigned": false}`
rather than an error status — the workflow has to distinguish an empty fleet, which it should
wait and retry on, from a broken service, which it should not.

Restaurant coordinates come from `GET /api/v1/restaurants/{restaurant_id}`, where `latitude`
and `longitude` are `NOT NULL`. The dispatch request carries them, so the Rider Service does
not need a Restaurant Service client of its own — the activity fetches them.

### 5.2 Release is idempotent

```sql
_RELEASE = """
    UPDATE riders
       SET is_available = TRUE,
           current_order_id = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE current_order_id = %s
    RETURNING id
"""
```

Zero rows means already released, which is success, not a `404`. Every compensation path
calls this, which is the leak Revision 1 left open.

### 5.3 Pickup and delivery relay a signal

`current_order_id` is why these need no cross-service lookup: the rider claiming to have
picked up order X must be the rider the row says is carrying X, and must be the token
subject.

```python
@app.post("/api/v1/riders/me/orders/{order_id}/picked-up", status_code=204)
def mark_picked_up(order_id: UUID, principal: Principal = Depends(require_role("rider"))):
    """Report a pickup, and signal the workflow waiting on it.

    Authorisation is settled entirely inside this service: the rider row already records
    which order this rider holds, so there is no need to ask the Order Service who was
    assigned — and therefore no second place that could disagree about it (D16).
    """
    rider = riders.find_by_user(principal.user_id)
    if rider is None:
        raise not_found("You have no rider profile")
    if str(rider["current_order_id"]) != str(order_id):
        raise forbidden(f"You are not carrying order {order_id}")
    order_service.signal(order_id, "rider_pickup", {"rider_id": str(rider["id"])})
```

---

## Section 6: Internal endpoints the worker calls

### 6.1 Payment Service

```python
@app.post("/api/v1/payments/authorize", response_model=PaymentResponse,
          dependencies=[Depends(require_internal)])
def authorize_for_workflow(payload: PaymentAuthorizeRequest, response: Response):
    """Authorise a payment on behalf of the order saga.

    The same steps as process_payment, minus the customer guard: the workflow is not a
    user, so there is no bearer token to forward and nobody to check ownership against.
    That is safe here precisely because the workflow did not choose the order — it was
    started by an already-authorised POST /api/v1/orders, whose handler verified that the
    caller owns it. The idempotency key is derived from the order id by the workflow, so a
    retried activity collapses onto the unique index rather than charging twice.
    """
```

`amount` arrives as a **string**, not a float: D07 keeps money exact in `Decimal` up to the
JSON boundary, and a workflow argument is a JSON boundary. `"27.00"` survives the round trip;
`27.0` is a float that may not.

```python
@app.post("/api/v1/payments/refund", response_model=PaymentResponse,
          dependencies=[Depends(require_internal)])
def refund_for_workflow(payload: PaymentRefundRequest):
    """Compensating action: void an authorisation the saga can no longer honour.

    Idempotent by status rather than by key — a payment already `refunded` is returned
    unchanged, because Temporal retries this and a second refund is real money. A payment
    still `pending` (its gateway call failed) is also resolved here, which is what finally
    sweeps the stranded rows D10 accepted and nothing has cleaned up since.
    """
```

Add `mark_refunded` to `PaymentRepository`, and `refund()` to `gateway.py` — the single seam
that changes when a live gateway is wired in (D10). References stay `ch_mock_…` so a
simulated refund is never mistaken for a real one.

The worker also needs to read an order without a user token, so
`GET /api/v1/orders/{order_id}` gains an internal-key path alongside its bearer path.

### 6.2 Restaurant Service

```python
POST   /api/v1/restaurants/tickets                      require_internal
GET    /api/v1/restaurants/{restaurant_id}/tickets      require_role("restaurant_admin") + owner
POST   /api/v1/restaurants/tickets/{order_id}/accept    require_role("restaurant_admin") + owner
POST   /api/v1/restaurants/tickets/{order_id}/reject    require_role("restaurant_admin") + owner
```

`POST /tickets` is idempotent on `order_id` and refuses when pending tickets already reach
`restaurants.capacity` — the first thing in the platform to read that column. A capacity
refusal is a **business** answer, `200 {"queued": false, "reason": "at_capacity"}`, not an
error: the workflow must not retry it.

Accept and reject update the ticket and then relay a `restaurant_decision` signal. Who owns a
restaurant is the Restaurant Service's fact, so the authorisation decision lives here and
nowhere else (D16).

### 6.3 Order Service — the one signal door

```python
@app.post("/api/v1/orders/{order_id}/signals", status_code=202,
          dependencies=[Depends(require_internal)])
async def signal_workflow(order_id: UUID, payload: WorkflowSignalRequest):
    """Relay a signal from a sibling service into this order's workflow.

    One endpoint rather than one per event, so a Temporal client lives in exactly two
    processes: this service and its worker. The Restaurant and Rider Services report what
    they observed over HTTP, exactly as they already report status transitions, and stay
    unaware that an orchestrator exists.

    The workflow id is derived from the order id, so nothing has to be stored to find the
    handle. A workflow that has already completed answers 409 rather than 404: the
    distinction matters to the caller — a rider marking a cancelled order delivered is a
    different problem from an order that never existed.
    """
    handle = temporal.client.get_workflow_handle(workflow_id_for(order_id))
    try:
        await handle.signal(payload.signal, payload.payload)
    except RPCError as exc:
        raise _map_signal_failure(order_id, exc) from exc
```

---

## Section 7: Activities (`services/order/activities.py`)

Activities are the non-deterministic half: HTTP calls and database writes. They are **sync
functions** on the existing `PostgresPool`, and the Worker runs them on a
`ThreadPoolExecutor` — which is what keeps psycopg2 and D21's "one way to reach a database"
intact instead of importing SQLAlchemy async alongside it.

Two rules decide everything else in this file:

- **Business outcome → `ApplicationError(..., non_retryable=True)`.** A kitchen declining an
  order is an answer, not a failure. Revision 1 raised `ValueError` under a 3-attempt retry
  policy, so a rejection was re-sent to the restaurant three times before compensating.
- **Transport failure → raise normally**, so the retry policy applies.

```python
"""Temporal activities for the order saga.

The non-deterministic side of the workflow: HTTP to sibling services, and writes to this
service's own database. Everything here is sync, run by the worker on a thread pool, so the
saga reaches Postgres through the same PostgresPool the request handlers use rather than a
second async engine (D21).

Every call outward carries X-Internal-Key rather than a forwarded bearer token. A workflow
argument is durable, UI-visible history, so a bearer token must never be one; and a 15-minute
access token cannot outlive a saga that waits on a kitchen (D26).

Every activity is idempotent, because Temporal retries them. The keys are derived from the
order id rather than generated, so a retry produces the same key as the attempt it replaces.
"""

import json
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from common.auth import internal_headers
from common.errors import ...
from common.service_client import ServiceClient


def payment_key(order_id: str) -> str:
    """The idempotency key for this order's authorisation.

    Derived, never generated: a retried activity must present the key of the attempt it is
    replacing, or the unique index that prevents double charging never sees a collision.
    """
    return f"wf-pay-{order_id}"


class OrderActivities:
    def __init__(self, *, orders, logger):
        self._orders = orders          # the existing OrderRepository
        self._logger = logger
        self._payments = ServiceClient("Payment Service", PAYMENT_SERVICE_URL, logger=logger)
        self._restaurants = ServiceClient("Restaurant Service", RESTAURANT_SERVICE_URL, logger=logger)
        self._riders = ServiceClient("Rider Service", RIDER_SERVICE_URL, logger=logger)

    @activity.defn
    def transition_order_activity(self, details: dict) -> str | None:
        """Move the order to a new status and append the transition, in one transaction.

        `old_status` is not a parameter. It is derived from the preceding trail entry by the
        insert itself, so the chain cannot disagree with itself (D24) — Revision 1 let the
        workflow assert a previous status, which a retry or a reordered activity could
        contradict.

        The update is a compare-and-set. Without it a retried activity could walk a
        delivered order back to assigned, because Temporal guarantees at-least-once
        execution, not exactly-once.
        """
        return self._orders.transition(
            order_id=details["order_id"],
            new_status=details["status"],
            updated_by=details.get("updated_by", "system"),
            raw_log=json.dumps(details.get("event", {})),
            metadata=details.get("metadata", {}),
            rider_id=details.get("rider_id"),
        )

    @activity.defn
    def authorize_payment_activity(self, details: dict) -> dict:
        """Charge the card. A declined card is final; an unreachable gateway is not."""
        result = self._payments.post(
            "/api/v1/payments/authorize",
            json={
                "order_id": details["order_id"],
                # A string, not a float: money stays exact across the JSON boundary (D07).
                "amount": details["amount"],
                "idempotency_key": payment_key(details["order_id"]),
            },
            headers=internal_headers(),
            missing=f"Order {details['order_id']} is unknown to the Payment Service",
            unreachable_hint="cannot authorise the payment",
        )
        if result.get("status") != "authorized":
            raise ApplicationError(
                f"Payment for order {details['order_id']} was not authorised",
                non_retryable=True,
            )
        return result
```

`send_ticket_activity`, `dispatch_rider_activity`, `refund_payment_activity` and
`release_rider_activity` follow the same shape. Two details worth stating:

- `dispatch_rider_activity` returns `{"assigned": False}` rather than raising when the fleet
  is empty. The workflow decides whether to wait and try again; that is a scheduling
  decision, and scheduling belongs in the workflow.
- `send_ticket_activity` raises non-retryably on `at_capacity`, and normally on a 503.

---

## Section 8: The workflow (`services/order/workflows.py`)

Deterministic. No I/O, no clock, no randomness — only activity calls, timers, signals and
`workflow.now()`.

```python
"""The order lifecycle as a durable state machine.

Deterministic by construction: every side effect is an activity, every wait is a Temporal
timer, and every external event arrives as a signal. That is what lets the worker be killed
mid-saga and resume exactly where it stopped — the property the whole of Week 2 exists to
demonstrate.

The order is already `created` and committed before this starts, with the opening entry of
its trail written in the same transaction (D24). Revision 1's first step set the status to
`created` again, recording a `created -> created` transition that never happened.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    # Flat import: the Dockerfile copies the service to /app, so there is no
    # `services.order` package. Revision 1's `from services.order.activities import ...`
    # would ModuleNotFoundError.
    from activities import OrderActivities
    from common.config import (
        DELIVERY_TIMEOUT_SECONDS,
        RESTAURANT_DECISION_TIMEOUT_SECONDS,
        RIDER_SEARCH_ATTEMPTS,
        RIDER_SEARCH_INTERVAL_SECONDS,
    )

# Transport-level retries only. Business outcomes are raised non-retryably by the
# activities, so they are never re-sent — the bug in Revision 1, where a restaurant
# rejection was retried three times before the saga compensated.
TRANSIENT = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

# Compensations retry harder than forward progress. A failed refund leaves a customer
# charged for an order that will never arrive, which is the worst state this system has.
COMPENSATION = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)


@workflow.defn
class OrderWorkflow:
    def __init__(self) -> None:
        self._restaurant_decision: str | None = None
        self._picked_up = False
        self._delivered = False
        self._stage = "starting"
        self._rider_id: str | None = None

    # --- signals: the outside world reporting something it observed ------------------

    @workflow.signal
    def restaurant_decision(self, payload: dict) -> None:
        decision = payload.get("decision")
        # Anything else is ignored rather than raising: a signal handler that throws fails
        # the workflow task, and a malformed report from a sibling must not cancel an order.
        if decision in ("accepted", "rejected"):
            self._restaurant_decision = decision

    @workflow.signal
    def rider_pickup(self, payload: dict) -> None:
        self._picked_up = True

    @workflow.signal
    def rider_delivery(self, payload: dict) -> None:
        self._delivered = True

    @workflow.query
    def stage(self) -> dict:
        """Read the saga's position without touching the database.

        Cheap observability: `temporal workflow query` answers where an order is stuck
        without a psql session, and the Web UI renders it.
        """
        return {"stage": self._stage, "rider_id": self._rider_id}

    # --- the run ----------------------------------------------------------------------

    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]

        # 1. Payment. Nothing has been charged yet, so a failure here needs no refund —
        #    only a cancellation. This is the one branch that does not compensate.
        try:
            await workflow.execute_activity(
                OrderActivities.authorize_payment_activity,
                {"order_id": order_id, "amount": payload["amount"]},
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=TRANSIENT,
            )
        except ActivityError as exc:
            await self._cancel(order_id, "payment_failed", str(exc))
            return {"status": "cancelled", "reason": "payment_failed"}

        await self._transition(order_id, "confirmed", "payment-service")

        # 2. The kitchen. A durable timer, not a long activity timeout: the workflow is
        #    idle here, holding no thread and no connection, and it survives a worker
        #    restart. Rev 1 modelled this as a synchronous HTTP call that returned the
        #    restaurant's decision, which no real kitchen can do.
        ...
```

The remaining steps, in the same shape:

3. **Kitchen decision** — `send_ticket_activity`, then
   `await workflow.wait_condition(lambda: self._restaurant_decision is not None, timeout=...)`.
   Rejection *and* timeout both compensate: refund, then `cancelled`. A timeout is a
   `asyncio.TimeoutError` from `wait_condition` and must be caught.
4. **Rider search** — a loop of `RIDER_SEARCH_ATTEMPTS`, each calling
   `dispatch_rider_activity` and, on `{"assigned": false}`, sleeping
   `RIDER_SEARCH_INTERVAL_SECONDS` on a durable timer. Exhausted → refund, `cancelled`.
   Assigned → `assigned`, recording `rider_id` on the order (the column has existed since
   Week 1 and nothing has ever written it).
5. **Pickup** — `await wait_condition(lambda: self._picked_up, timeout=DELIVERY_TIMEOUT)` →
   `picked_up`.
6. **Delivery** — same → `delivered`, then `release_rider_activity`.

And the compensation helper every failure path past step 1 routes through:

```python
    async def _compensate(self, order_id: str, reason: str, detail: str) -> None:
        """Refund the customer and release any claimed rider, then cancel.

        Order matters. The refund is the customer's money and retries hardest; the release
        is the fleet's availability. Revision 1 refunded and stopped, so every rider claimed
        by a saga that later failed stayed `is_available = FALSE` for good — the fleet
        drained one failed order at a time.
        """
        await workflow.execute_activity(
            OrderActivities.refund_payment_activity,
            {"order_id": order_id, "reason": reason},
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=COMPENSATION,
        )
        if self._rider_id is not None:
            await workflow.execute_activity(
                OrderActivities.release_rider_activity,
                {"order_id": order_id},
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=COMPENSATION,
            )
        await self._transition(order_id, "cancelled", "order-workflow", detail=detail)
```

---

## Section 9: The worker (`services/order/worker.py`)

```python
"""The process that executes workflows and activities.

Its own container, sharing the Order Service's image because the workflow and its activities
are that service's code. Revision 1 supplied this file but never scheduled it, so nothing
would have run the saga.

`activity_executor` is the load-bearing argument: the activities are sync psycopg2 functions,
and without a thread pool they would block the worker's event loop.
"""

import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from activities import OrderActivities
from common.config import ORDER_TASK_QUEUE, DEFAULT_TEMPORAL_ADDRESS, required
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import OrderRepository
from workflows import OrderWorkflow

SERVICE_NAME = "order-worker"


async def main() -> None:
    logger = configure_logging(SERVICE_NAME)
    db = PostgresPool(required("DATABASE_URL"), logger=logger)
    address = os.getenv("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

    async with db.lifespan(None):
        client = await Client.connect(address)
        activities = OrderActivities(
            orders=OrderRepository(db, logger=logger, service_name=SERVICE_NAME),
            logger=logger,
        )
        # One thread per concurrent activity, and the pool is deliberately no larger than
        # POOL_MAX_CONNECTIONS: a thread that cannot lease a connection is a thread that
        # turns a transient shortage into a failed activity.
        with ThreadPoolExecutor(max_workers=10) as executor:
            worker = Worker(
                client,
                task_queue=ORDER_TASK_QUEUE,
                workflows=[OrderWorkflow],
                activities=[
                    activities.transition_order_activity,
                    activities.authorize_payment_activity,
                    activities.refund_payment_activity,
                    activities.send_ticket_activity,
                    activities.dispatch_rider_activity,
                    activities.release_rider_activity,
                ],
                activity_executor=executor,
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            # Without this, `docker compose stop` gets SIGKILL after the grace period and
            # in-flight activities are abandoned rather than finished.
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            logger.info("Worker polling %s at %s", ORDER_TASK_QUEUE, address)
            await worker.run(shutdown_event=stop)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Section 10: Starting the workflow (`services/order/main.py`)

`create_order` keeps **every step it has today** — the idempotency header, the replay branch,
server-side re-pricing, both HTTP verifications, and the transactional insert of the order
with the opening entry of its trail. The workflow start is appended, not substituted.

```python
    # (f) The order is committed; the saga runs it from here. The workflow id is derived
    # from the order id, which is what makes starting one idempotent without a table to
    # record whether we already did: USE_EXISTING returns a handle to the running workflow
    # instead of raising, so a client retry cannot start a second saga for one order.
    await _start_saga(order)
    return OrderResponse(**order)
```

```python
async def _start_saga(order: dict) -> None:
    """Hand a committed order to the orchestrator.

    Deliberately after the commit and deliberately not fatal. Temporal cannot enlist in a
    Postgres transaction, so there is no way to make "order exists" and "saga started" one
    atomic fact. Given the choice, the order wins: it is what the customer was told about,
    and a missing saga is repairable — a retry with the same idempotency key takes the
    replay branch, which starts the saga too.

    This is D09's old argument in a new place, and it resolves the same way: the write that
    already succeeded must not be reported as a failure.
    """
    try:
        await temporal.client.start_workflow(
            OrderWorkflow.run,
            {
                "order_id": str(order["id"]),
                "restaurant_id": str(order["restaurant_id"]),
                # str, not float: an exact decimal has to survive the JSON boundary (D07).
                "amount": str(order["total_amount"]),
                "items": order["items"],
            },
            id=workflow_id_for(order["id"]),
            task_queue=ORDER_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except Exception as exc:  # noqa: BLE001
        # Logged at error: an order with no saga will sit at `created` until someone
        # retries it, which is worth an alert even though it is not worth a 500.
        logger.error("Could not start saga for order %s: %s", order["id"], exc)
```

`WorkflowIDConflictPolicy.USE_EXISTING` (confirmed present in the SDK) is why there is no
`try/except WorkflowAlreadyStartedError` — the conflict is declared away rather than caught.
Note also that `WorkflowAlreadyStartedError` lives in `temporalio.exceptions`, not
`temporalio.client`.

The replay branch calls `_start_saga` too, so a retry repairs a start that failed the first
time.

Also in this file:

- **Lifespan** — `db.lifespan` and `temporal.lifespan` composed with
  `contextlib.AsyncExitStack`, since FastAPI takes one.
- **`/health`** — reports `temporal_reachable`.
- **`create_order` becomes `async def`**, because starting a workflow is awaited. The
  repository calls inside it stay sync; FastAPI runs them on the event loop, which is a
  change worth noting — the existing sync handler ran in a threadpool.
- **`OrderResponse` gains `rider_id`**, and `_COLUMNS` in `repository.py` gains it too.
  Additive, so the Payment Service and the smoke test keep working.

---

## Section 11: Verification

The Week 2 deliverable is not "the code exists"; it is "the saga survives failure". Each of
these is a command, and the last two are the ones that actually prove Temporal is earning its
container.

1. **Bootstrap fidelity (D20)** — run `scripts/init_bootstrap.sh` into a temp directory and
   `diff` all eight generated artifacts against the working tree. Any difference means the
   next bootstrap silently reverts something.
2. **Cold start** — `docker compose down -v && docker compose up -d --build`. Required after
   Section 3.2, because `init.sql` only runs on an empty data directory.
3. **Worker is polling** — `docker compose logs order-worker` shows the task queue, not a
   traceback. `GET /api/v1/orders/health` reports `temporal_reachable: true`. The Web UI
   loads on `http://localhost:8233`.
4. **Credential boundary (D01)** — the Rider Service cannot reach `sfo_user_core`; the
   existing assertion that only `user-service` holds `JWT_PRIVATE_KEY_B64` still passes.
5. **Happy path** — order → poll `confirmed` → accept the ticket → poll `assigned` with a
   non-null `rider_id` → pickup → poll `picked_up` → deliver → poll `delivered`.
   `GET /api/v1/orders/{id}/logs` shows six transitions in order with correctly derived
   `previous_status`, and the rider is back to `is_available = TRUE`.
6. **Every compensation path** — restaurant rejects; restaurant never answers (timeout);
   no rider within range. Each must reach `cancelled` with the payment `refunded` and **no
   rider left claimed**. That last assertion is the one Revision 1 would have failed.
7. **Authorisation** — every internal endpoint answers `401` without `X-Internal-Key`; a
   customer token cannot accept a ticket or dispatch a rider; a rider cannot mark another
   rider's order delivered.
8. **Idempotency** — replaying the create returns the same order id *and* leaves exactly one
   workflow execution. `POST /api/v1/payments` on an orchestrated order answers `409`.
9. **Concurrency** — fire N simultaneous orders at a fleet of one. Exactly one reaches
   `assigned`; the rest cancel. This is what proves `FOR UPDATE SKIP LOCKED`.
10. **Durability** — `docker compose restart order-worker` mid-saga. The workflow must resume
    and still reach `delivered`. If this passes, Temporal is doing its job; if it does not,
    nothing else here matters.

Because the saga is eventually consistent, `scripts/smoke-test.sh` needs a polling helper
alongside its existing `expect`/`assert`/`jfield`. Its current assertions are synchronous and
several become races the moment the workflow starts.
