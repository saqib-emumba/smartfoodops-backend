# SmartFoodOps — Observability & Eventing: How Temporal, Kafka, OpenTelemetry, Jaeger, Prometheus and Grafana Actually Work

This document explains **the code as it exists today** — what `services/common/telemetry.py`,
`services/common/kafka.py`, `services/common/outbox.py`, `services/common/temporal.py` and the
two Week 3 consumers (`services/analytics/consumer.py`, `services/notification/consumer.py`)
actually do, and how Temporal, Kafka, Jaeger, Prometheus and Grafana fit around them.

- **What was planned, and why the original blueprint wasn't safe to follow as written** →
  [week3-event-driven-observability-blueprint.md](week3-event-driven-observability-blueprint.md)
- **How the saga itself works, end to end** →
  [order-saga-orchestration-guide.md](order-saga-orchestration-guide.md)
- **Why each choice was made, against what alternative** →
  [key-decisions.md](key-decisions.md) (D25–D32, D36, D38–D45)
- **This document** → how the six pieces work together, end to end, including what happens
  when each one is down, and how to log into each one's UI

One rule governs all of it, and everything below is a consequence of it:

> **Nothing on the checkout or saga path may depend on Kafka, Jaeger, Prometheus, or Grafana.**
> All four are read-only observers of state that already exists, or an append-only sink for
> facts a service already committed. Stop any of them and `scripts/smoke-test.sh` still passes.
> Temporal is the one exception, by design, not by omission: it is not observability
> tooling here, it *is* the saga's durable state — stopping `temporal-server` stops new sagas
> starting, exactly as stopping Postgres would. What's covered below is Temporal's
> **observability surface** (its Web UI, its own Prometheus metrics, and the trace spans
> `TracingInterceptor` adds around it) — not the orchestration engine itself, which
> [order-saga-orchestration-guide.md](order-saga-orchestration-guide.md) already covers in full.

---

## 1. The six pieces, and what each one owns

| Piece | Role | Talks to |
|---|---|---|
| **Temporal** | Durable saga state — every order's workflow history, timers, and retries. Not optional infrastructure like the other five; the saga *is* Temporal | `orchestrator-service`/`worker` (gRPC `:7233`); exposes its own Web UI (`:8233`) and Prometheus metrics (`:9233`) |
| **OpenTelemetry** | The instrumentation layer *inside* every process — creates spans, propagates trace context across HTTP, Kafka, and Temporal boundaries | Nothing external directly; hands finished spans to an exporter |
| **Jaeger** | Trace storage + UI. `jaegertracing/jaeger:2.x` is itself an OTel Collector distribution, so it receives OTLP natively — no separate Collector container | Receives OTLP from every service over HTTP (`:4318`) |
| **Prometheus** | Metrics storage + query engine. Pulls, never pushed to | Scrapes every service's `/metrics`, plus Temporal's own `:9233`, on a 10s interval |
| **Grafana** | Dashboards over Prometheus's data | Queries Prometheus (`:9090`) only |
| **Kafka** | The durable, ordered fact log for what happened to an order | Written by the outbox relays in `order-service`/`payment-service`; read by `analytics-service` and `notification-consumer` |

Temporal sits apart from the other five: it's load-bearing, not observability. The other five
split into two concerns about *that* saga (and everything else) from the outside: two are about
**understanding one request** (OpenTelemetry + Jaeger — a trace), two are about **understanding
the system over time** (Prometheus + Grafana — a metric), and one is about **telling other
services what happened** (Kafka — an event). All three of those concerns are wired into the
same chassis modules — `common/telemetry.py` for tracing and metrics, `common/outbox.py` +
`common/kafka.py` for eventing — so a new service gets all of it for the cost of one
`bootstrap()` call and one `instrument_app()` call. Temporal's own tracing hook,
`TracingInterceptor`, is wired separately, in `common/temporal.py` — see §4.1.

---

## 2. The whole picture

```
                                POST /api/v1/orders           (a customer places an order)
                                        │
                                        ▼
                              ┌────────────────────┐
                              │   order-service      │──── 1. INSERT order ────▶ ┌────────────────┐
                              │   :8004               │     INSERT order_outbox  │ sfo_order_core   │
                              └──────────┬────────────┘     (same transaction)   └────────┬─────────┘
                                         │ starts the saga                                 │
                                         │ (HTTP, X-Internal-Key)             2. background │ relay polls
                                         ▼                                    FOR UPDATE SKIP LOCKED
                        ┌────────────────────────────┐                                    ▼
                        │ orchestrator-service/worker  │             ┌──────────────────────────┐
                        │ starts/runs the saga          │◀── gRPC ──▶│  temporal-server           │
                        └──────────┬─────────────────────┘  :7233   │  durable workflow state     │
                                   │                                 │  :8233  Web UI               │
                                   │ HTTP: authorize, dispatch,      │  :9233  Prometheus metrics    │
                                   │       transition, refund        └──────────────────────────────┘
                                   ▼                                              │
                    payment-service / rider-service           ┌─────────────────▼──────────┐
                                                                │  OutboxRelay                │
                                                                │  (in-process, common/       │
                                                                │   outbox.py) — 3. validate,  │
                                                                │   publish, mark published   │
                                                                └─────────────────┬────────────┘
                                                              validate against    │
                                                              schema-registry     ▼
   ══════════════════════════════════ every hop above ════════════════════════════════════════════
   also emits an OTel span, parented under the request's trace, carrying:
     • server span   (FastAPIInstrumentor)      • DB span     (Psycopg2Instrumentor)
     • client span    (HTTPXClientInstrumentor)  • Temporal span (TracingInterceptor, client-side)
   ...and every FastAPI app increments sfo_http_requests_total / _duration_seconds on the way.
   ═════════════════════════════════════════════════════════════════════════════════════════════

                                                                  ┌───────────────────────┐
                                                                  │  Kafka                  │
                                                                  │  sfo.order.events.v1     │  (3 partitions,
                                                                  │  keyed: order_id          │  keyed for
                                                                  └────┬──────────────┬─────┘  per-order order)
                                                     consumer group    │              │  (implicit
                                                     "analytics"       ▼              ▼   consumer group)
                                                          ┌──────────────────┐ ┌────────────────────────┐
                                                          │ analytics-service │ │ notification-consumer   │
                                                          │ (own Postgres,    │ │  → celery send_task →   │
                                                          │  dedup + counts)  │ │    RabbitMQ → worker     │
                                                          └──────────────────┘ └────────────────────────┘

    traces ──OTLP/HTTP──▶ Jaeger :16686      metrics ──scrape──▶ Prometheus :9090 ──▶ Grafana :3000
    (temporal-server's OWN metrics on :9233 are scraped by Prometheus the same way as any service)
```

Everything below unpacks one lane of this diagram at a time.

---

## 3. Temporal — the saga's durable state, and its own observability surface

Temporal isn't one of the observability add-ons — it's the load-bearing piece Week 2 built
(D25–D32, D36; full detail in
[order-saga-orchestration-guide.md](order-saga-orchestration-guide.md)). It's covered here
because it exposes an observability surface of its own that the other five pieces plug into,
and because `TracingInterceptor` is the one trace-propagation hook that lives in
`common/temporal.py` rather than `common/telemetry.py`.

**What it owns.** Every order's workflow history — every activity call, every signal, every
timer — is durable inside Temporal's own storage (backed by `temporal-postgresql`), not inside
`orchestrator-worker`'s process memory. That's what a worker restart mid-saga survives: the
next worker to poll the `order-tasks` task queue picks the workflow up exactly where it left
off, because nothing about the saga's state depended on that specific worker process staying
alive.

**Its own three ports**, all on `temporal-server`:

| Port | What | Notes |
|---|---|---|
| `:7233` | gRPC workflow API | Used by `orchestrator-worker`, and since D47 by `order-service` and `rider-service`, which start and signal workflows directly. `orchestrator-service` holds a client only for its health probe |
| `:8233` | Web UI | Browse every workflow by id (`order-<ORDER_UUID>`), its full event history, and replay it step by step. **Deliberately not behind the gateway — it has no auth of its own** (see §8) |
| `:9233` | Prometheus metrics | Pinned specifically for Week 3 — scraped by `prometheus.yml`'s `temporal-server` job like any other target |

**How it joins a trace.** `TracingInterceptor` is registered on the **Client** only — never the
Worker (confirmed against Temporal's own docs; a Worker-side interceptor would wrap workflow
*replay*, which must stay a pure, deterministic function of history, not something that emits
spans with side-effecting timestamps) — and in **both** places a client is constructed:
`common/temporal.py`'s `Client.connect()` and `orchestrator/worker.py`, which routes through
the same `TemporalGateway` rather than a second, uninstrumented `Client.connect()` call. Skip
either one and a trace snaps at `POST /api/v1/orders` — none of the saga's activities show up
in Jaeger, which is the single biggest source of "missing" trace data if this is ever touched.
`SandboxRestrictions.default.with_passthrough_modules("opentelemetry")` lets the OTel API
through the workflow sandbox, the same class of allowance `imports_passed_through()` already
grants a handful of other modules.

**If `temporal-server` is down**, this is the one case where the answer to "does checkout
break" is genuinely *yes* — no new saga can start, and no signal can reach a running one. That
isn't a Week 3 regression; it's true today, independent of anything in this document, because
the saga's state lives there and nowhere else. What Week 3 adds is *visibility into that*: its
own Prometheus target goes red in `/targets`, and its Web UI (when it's the thing that's down)
simply becomes unreachable rather than silently degrading.

---

## 4. Tracing — OpenTelemetry + Jaeger

### 4.1 Where a trace is born and where it dies

Every process calls `configure_telemetry(service_name)` from inside `bootstrap()`
([services/common/bootstrap.py](../services/common/bootstrap.py)) — the same call that names
the logger and the health payload, so a trace's `service.name` **cannot** disagree with what
`/health` reports for that process:

```python
resource = Resource.create({SERVICE_NAME: service_name})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=".../v1/traces")))
```

`BatchSpanProcessor`, never `Simple` — spans queue in-process and are exported in batches, so a
Jaeger outage adds **zero** latency to a live request; spans simply queue and are dropped if the
buffer fills, never retried against the request itself.

`instrument_app(app, service_name)` — called once per FastAPI app, the line after
`install_error_handlers(app)` — wires the parts that need the `app` object:

- `FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics")` — one **server span** per
  inbound request. The exclusion matters: without it, Prometheus's own scrape (every 10s,
  forever) manufactures a trace per service per scrape, drowning out the traces that matter.
- The Prometheus middleware (§5).
- The `/metrics` route itself.

Two more instrumentors are installed once, process-wide, inside `configure_telemetry` (they
monkeypatch a *class*, not one `app`, so they belong at that scope):

- `Psycopg2Instrumentor` — one **DB span** per query, with the actual SQL and duration. This
  only works because `postgres.py` passes `cursor_factory=RealDictCursor` at
  `ThreadedConnectionPool()` **construction** time — the instrumentor only wraps the cursor
  factory it sees at connect time, not one passed per-call to `conn.cursor(...)`. Passing it
  per-call (the original shape of this code) produced zero DB spans with no error at all.
- `HTTPXClientInstrumentor` — one **client span** per outbound call, and it injects the
  `traceparent` header automatically into every `httpx.Client`/`AsyncClient` request. This is
  what makes `common/service_client.py`'s four request helpers (`get`/`aget`/`post`/`apost`)
  trace-propagating with **zero code changes to that file** — the instrumentor patches the
  class those helpers already build a fresh client from.

Two boundaries HTTPX auto-instrumentation can't reach, handled explicitly:

- **Temporal.** `TracingInterceptor` is registered on the **Client** only — never the Worker,
  confirmed against Temporal's own docs — in both places a client is constructed:
  `common/temporal.py`'s `Client.connect()` — which every client now goes through, including
  `order-service`'s and `rider-service`'s since D47 — and `orchestrator/worker.py`, which
  routes through the same `TemporalGateway` rather than calling `Client.connect()` a second,
  uninstrumented way. One connect path means a service that gains a Temporal client gains
  trace propagation with it, rather than having to remember. Without this, a trace snaps at
  `POST /api/v1/orders` and none of the saga's activities appear in it.
- **Kafka.** There is no HTTP call to instrument automatically, so `common/kafka.py` carries
  the propagation by hand:
  - `inject_trace_headers(traceparent, tracestate)` — called by the relay at publish time,
    using the trace context **captured when the outbox row was written**
    (`append_outbox` calls `TraceContextTextMapPropagator().inject(...)` inside the original
    request's span), not the relay's own ambient context. The relay runs asynchronously,
    potentially minutes later — injecting its own context would start a new, disconnected
    trace instead of continuing the one the write belongs to.
  - `extract_trace_context(headers)` — called by both consumers before they open their own
    `CONSUMER`-kind span, so that span parents correctly under the original request instead of
    starting an orphaned trace.

### 4.2 What one trace looks like

A single `POST /api/v1/orders` that reaches the kitchen, gets accepted, and is picked up by a
rider produces one trace with (at least) these spans, all under one `trace_id`:

```
order-service       POST /api/v1/orders                         (server span)
 ├─ psycopg2         INSERT INTO orders / order_outbox           (DB span)
 ├─ temporal         StartWorkflow                              (Temporal span, TracingInterceptor)
 │   └─ orchestrator-worker  OrderWorkflow.run                    (workflow task)
 │       ├─ httpx     POST payment-service/authorize              (client span)
 │       │   └─ payment-service  ...                              (server + DB span)
 │       └─ httpx     POST rider-service/dispatch                  (client span)
 │                   └─ rider-service  ...                         (server + DB span)
 └─ (later, async, same trace_id via Kafka headers)
     analytics.consume         order.confirmed                    (consumer span)
     notification.consume      order.confirmed → celery.send_task (consumer span)
```

The Kafka-carried spans are the interesting part: they appear **minutes** after the HTTP spans
above them (the outbox relay polls once a second, and the consumer may be further behind), but
Jaeger still stitches them into the same trace because the `traceparent` travelled with the
message the whole way from `append_outbox` to `extract_trace_context`.

### 4.3 Logs join the same trace

`common/logging_config.py` installs a `LogRecordFactory` (not a `logging.Filter` — a filter
only decorates records reaching the one handler it's attached to, and a record formatted
elsewhere raises `Formatting field not found` and is lost) that stamps every log line with
`trace=%(otel_trace_id)s span=%(otel_span_id)s`, zero-padded to 32/16 hex characters. Paste a
`trace_id` from Jaeger into `docker compose logs | grep <trace_id>` and every log line from
every service touched by that one request comes back, in order.

### 4.4 If Jaeger is down

Spans queue in the `BatchSpanProcessor`'s in-memory buffer and are dropped once it's full.
Requests are unaffected — no exception, no added latency, just missing traces until Jaeger
comes back. Nothing retries against Jaeger being unavailable; there is nothing durable to lose,
by design (traces are a debugging aid, not a system of record).

---

## 5. Metrics — Prometheus + Grafana

### 5.1 What gets measured, and where

Every FastAPI service's `instrument_app()` installs one middleware
([services/common/telemetry.py](../services/common/telemetry.py)) that maintains two metrics,
declared **once at module import**, never inside a request handler:

```python
REQUEST_COUNT   = Counter("sfo_http_requests_total", ..., ["method", "route", "status"])
REQUEST_LATENCY = Histogram("sfo_http_request_duration_seconds", ..., ["method", "route"])
```

Three correctness rules make this middleware safe rather than a future incident:

1. **Label by the matched route *template*, never the raw path.** `request.scope["route"]` is
   only populated by the router *during* dispatch, so it's read **after** `call_next`, never
   before. A raw `request.url.path` contains order UUIDs — one series per order, forever. The
   route path (`/api/v1/orders/{order_id}`) is fixed no matter how many orders exist.
2. **A 404 gets a fixed label**, `"__unmatched__"` — not the path that didn't match anything.
   Otherwise a 404 scanner mints unbounded cardinality just as fast as UUIDs would.
3. **`status_code` defaults to `500` before the `try`, in a `finally`.** `install_error_handlers`'
   catch-all sits on Starlette's `ServerErrorMiddleware`, *outside* this middleware, so an
   unhandled exception propagates *through* this middleware's `finally` as an exception, not a
   response — without the pre-set default, every 500 would silently vanish from the counter
   instead of being recorded.

Buckets are tuned to the platform's own real timeouts (`common/config.py`):
`MOCK_GATEWAY_LATENCY_SECONDS=5.0`, `PAYMENT_HTTP_TIMEOUT=15.0` — the histogram tops out at 30s
so neither of those calls lands in an overflow bucket that hides the actual distribution.

`/metrics` itself is **not** wrapped in the platform's usual JSON envelope (D35) — Prometheus's
exposition format is a machine-defined content type no envelope can wrap without breaking every
scraper (D41). It's `include_in_schema=False` (off the OpenAPI docs) and has no nginx location,
so it's unreachable through the gateway by construction — every real route is
`/api/v1/<entity>/…`, and nginx routes by that prefix alone.

Two processes have no FastAPI app to hang a middleware on, so they run a bare
`prometheus_client.start_http_server(...)` instead:

- `orchestrator-worker` — port `9108`, right before `Worker(...)` starts polling. Without it
  the worker (which does all the saga's actual work) would be completely unscrapeable.
- `notification-consumer` — port `9110`, for the same reason.

### 5.2 The other two families of metric

- **Outbox relay metrics** (`common/outbox.py`), one set per table (`order_outbox`,
  `payment_outbox`), labeled `outbox_table`:
  `sfo_outbox_backlog`, `sfo_outbox_lag_seconds` (age of the oldest unpublished row),
  `sfo_outbox_published_total`, `sfo_outbox_publish_failures_total`. Backlog and lag are the
  two signals that say the relay is alive at all — a flat, non-zero backlog with lag climbing
  means the relay is stuck (Kafka down, or every publish failing schema validation).
- **Business metrics** (`services/analytics/consumer.py`): `sfo_business_orders_placed_total`,
  `_delivered_total`, `_cancelled_total` (all `Counter`, never `Gauge` — a `Gauge` here would
  make `rate()`/`increase()` meaningless), `sfo_business_average_delivery_seconds` (computed
  from the stored `order_projections` table, not an in-process dict, so a restart doesn't
  silently reset it), and `sfo_consumer_last_message_timestamp_seconds` — a flat line on that
  one means a wedged consumer, even though the service still reports healthy.
  **Restart durability**: on startup, `_seed_metrics()` calls the private
  `Counter._value.set(n)` to seed each counter from the database's own totals *before* serving
  any traffic — otherwise a restart would silently reset business counters to zero even though
  nothing was actually lost.

### 5.3 Prometheus itself

[prometheus/prometheus.yml](../prometheus/prometheus.yml) scrapes every target by its Compose
service name, over `smartfoodops-network` — no app service publishes its port to the host for
this, and none needs to:

```yaml
scrape_interval: 10s
scrape_configs:
  - job_name: "temporal-server"       # :9233, pinned since Week 2 specifically for this
  - job_name: "user-service"          # :8001
  - job_name: "restaurant-service"    # :8002
  - job_name: "menu-service"          # :8003
  - job_name: "order-service"         # :8004
  - job_name: "payment-service"       # :8005
  - job_name: "rider-service"         # :8006
  - job_name: "orchestrator-service"  # :8007 — health and /metrics only since D47
  - job_name: "orchestrator-worker"   # :9108 — bare prometheus_client server, no FastAPI app
  - job_name: "analytics-service"     # :8008
  - job_name: "notification-consumer" # :9110 — same reasoning as orchestrator-worker
  - job_name: "rabbitmq"              # :15692, via the rabbitmq_prometheus plugin
```

The file is mounted read-only into the container
(`./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro`) and is one of the files
`init_bootstrap.sh` regenerates byte-for-byte (D20, D42) — new services get scraped by editing
one file in one place, not by touching every service.

**Prometheus does not hot-reload this file.** Adding a target requires
`docker kill --signal=SIGHUP sfo-prometheus`, or recreating the container.

### 5.4 Grafana

Both the datasource and the dashboard *provider* are file-provisioned
(`grafana/provisioning/datasources/prometheus.yml`,
`grafana/provisioning/dashboards/dashboards.yml`), not clicked together — so `docker compose
down -v` never discards them, and `GF_AUTH_ANONYMOUS_ENABLED: "false"` behind a password from
`.env` (`GRAFANA_ADMIN_PASSWORD`) means the UI isn't wide open on `:3000`.

Four dashboards ship as JSON in that directory, every query verified against this stack's own
running Prometheus before being checked in (not guessed from source):

| Dashboard | Covers |
|---|---|
| **Service RED Metrics** | Request rate / error rate / P95 latency per service, top routes by traffic, scrape-target health, resident memory |
| **Eventing & Outbox** | `sfo_outbox_backlog`/`sfo_outbox_lag_seconds` per table, publish rate and failures, consumer staleness (`time() - sfo_consumer_last_message_timestamp_seconds`), notification enqueue rate, RabbitMQ queue depth |
| **Business Metrics** | The analytics read-model's own counters — orders placed/delivered/cancelled, average delivery time, cancellation rate |
| **Temporal Saga** | `temporal-server:9233`'s own metrics — workflow completions, activity success/fail by type, task-queue poll health, schedule-to-start and schedule-to-close latency, all scoped to `order_tasks` / `OrderWorkflow` |

Kafka's own broker has no Prometheus exporter in this stack, so the Eventing dashboard observes
it through its consequences (outbox lag, consumer staleness) rather than broker-internal
metrics — a real gap if a future need justifies adding a JMX exporter, not one closed here.

### 5.5 If Prometheus or Grafana is down

Every service still serves traffic — the middleware increments in-process counters regardless
of whether anything ever scrapes them; a `Counter` with nobody reading it just grows quietly in
memory until the next scrape catches up. Grafana being down affects nothing but the dashboards
themselves; Prometheus keeps scraping and storing independently of whether anyone's looking.

---

## 6. Eventing — Kafka, the outbox, and the two consumers

### 6.1 The rule that avoids the dual-write bug

The obvious way to publish "an order was placed" is to commit the order, then call Kafka. That
is a **dual write**: if the Kafka call fails after the commit, the order exists but the fact
was never told to anyone downstream — and D09/D24 already fought this exact class of bug for
the audit log, years before Kafka existed in this codebase. Reintroducing it here would put
back a bug this platform already fixed once.

The fix is the **transactional outbox** (D39): the event row is written in the **same Postgres
transaction** as the business row, so it's as durable as the write it describes and impossible
to lose independently of it.

```python
# services/order/repositories/orders.py — inside the same cursor(commit=True) block:
cur.execute(TRANSITION_ORDER, {...})          # the business write
if changed:                                    # only when the CAS actually took effect —
    append_log(cur, ...)                       # inherits "exactly one row per real change"
    append_outbox(cur, table="order_outbox", ...)   # for free, from the same guard
```

Gating the outbox write on the same `changed` branch that already guards the audit-trail write
means a retried Temporal activity (D31) that no-ops on its second attempt emits **zero** extra
events, with no new logic — it inherits the guarantee `transition()` already had.

### 6.2 The relay: table → Kafka

`OutboxRelay` ([services/common/outbox.py](../services/common/outbox.py)) runs **in-process**
inside `order-service` and `payment-service` — not a separate container. A sidecar relay would
need write credentials to that service's database in a second place, which is exactly the kind
of boundary regression D01/D19 exist to prevent, for no real benefit at this platform's size.
One relay per table also means one relay can never race another over the same table's `seq`
ordering.

Its loop, once a second:

```
claim  := UPDATE ... FOR UPDATE SKIP LOCKED WHERE published_at IS NULL ORDER BY seq LIMIT 50
for each claimed row, in seq order:
    validate row.payload against the JSON Schema for (row.event_type, row.event_version)
    producer.send_and_wait(topic, key=order_id, value=envelope, headers=trace headers)
producer.flush()
mark those rows published_at = now()
```

- **`FOR UPDATE SKIP LOCKED`**, already idiomatic in this codebase
  ([services/rider/repositories/riders.py](../services/rider/repositories/riders.py)), rather
  than a high-water-mark cursor — a `BIGSERIAL` hands out `seq` values *before* commit, so a
  lower `seq` can commit after a higher one; a cursor would skip it forever. The
  `published_at IS NULL` predicate has no such race.
- **Startup failure is non-fatal.** If Kafka is unreachable, the relay logs a warning and rows
  simply accumulate — `order-service` and `payment-service` keep serving checkout traffic with
  zero dependency on Kafka being up. This is the property that lets the smoke test pass with
  Kafka stopped entirely (§6.5).
- **Crash after the broker ack but before `mark_published`** re-publishes the same rows on
  restart — **at-least-once**, deduplicated downstream by `event_id` (§6.3). A persistent
  publish failure is never silently dropped; it climbs `attempts` and `last_error` on the row
  and is visible in `sfo_outbox_publish_failures_total`.

### 6.3 The envelope, the topic, and the schema registry

One topic for the whole order aggregate, `sfo.order.events.v1`
([services/common/events/topics.py](../services/common/events/topics.py)), 3 partitions,
**keyed by `order_id`** — payment events ride the same topic. Kafka only guarantees ordering
*within* one topic-partition, and every consumer here depends on seeing, say,
`order.confirmed` before `order.picked_up` for the same order — splitting events across topics
by type would make that guarantee unavailable.

```json
{
  "event_id": "…",        // UUID — what consumers dedup on
  "event_type": "order.confirmed",
  "event_version": 1,
  "aggregate_type": "order",
  "aggregate_id": "…",    // = the Kafka message key
  "seq": 42,               // this outbox row's own sequence — lets a consumer detect gaps
  "occurred_at": "…",      // business time, from the DB row — NOT Kafka's own transport time
  "producer": "order-service",
  "data": { ... }
}
```

The producer (`common/kafka.py::build_producer`) sets `acks="all"` and
`enable_idempotence=True` — without both, a broker-side leader failover between retries can
reorder or duplicate a message *before* it's ever acked, and no amount of consumer-side dedup
can undo an ordering guarantee that never held in the first place.

**Schema Registry validation is on the hot path, not just at boot.** Both the relay (before
`send`) and every consumer (before `model_validate_json`) call
`SchemaRegistryValidator.validate(event_type, event_version, data)`, which compiles each
`(event_type, event_version)` pair's JSON Schema once with `fastjsonschema` and caches it —
keyed by the **exact version the message declares**, never "whatever the registry currently
has." That distinction is what makes a rolling deploy safe: an old pod still emitting
`event_version: 1` and a new one emitting `version: 2` are each checked against their own
declared version, never against whichever version a shared cache happened to fetch most
recently.

**Consumer rule:** an *unrecognised* `event_type` is logged and the offset is committed — it's
a producer shipping a feature this consumer doesn't know about yet, not a poison message. A
*known* type whose payload fails its own schema, or a message that can't even be decoded, goes
to the `.dlq` topic instead. Conflating the two would shovel an entire topic into the DLQ over
a forward-compatible field addition.

### 6.4 The two consumers

**`analytics-service`** ([services/analytics/consumer.py](../services/analytics/consumer.py))
— its own Postgres on `:5438`, `sfo_analytics_core`, reachable by nothing else in the platform.
`enable_auto_commit=False`; the loop commits the **database write before the Kafka offset**,
never the reverse:

```
handle message → write projection + processed_events dedup row, ONE local transaction
              → THEN consumer.commit()
```

At-least-once delivery plus that ordering equals effectively-once: a crash between the DB
commit and the offset commit redelivers the message, and `INSERT ... ON CONFLICT DO NOTHING`
on `processed_events (consumer_group, event_id)` makes the redelivery a no-op. Committing the
offset first would risk losing a message the process crashed on before ever applying it.

**`notification-consumer` / `notification-worker`** — two containers built from one image
(D45). The consumer is the only one of the two with a Kafka client; it calls
`celery_app.send_task(...)` and commits its Kafka offset **only after** the task is
successfully enqueued onto RabbitMQ — so Kafka, not RabbitMQ, is the durable ledger. The worker
never touches Kafka at all; it just drains the queue with `task_acks_late=True` and
`worker_prefetch_multiplier=1`, resolving contact details from the User Service's internal
endpoint before dispatching a simulated SMS/email.

### 6.5 If Kafka (or the Schema Registry, or RabbitMQ) is down

- **Kafka down** → outbox rows accumulate in Postgres; `order-service`/`payment-service`
  continue authorizing payments, confirming orders and running the saga exactly as before.
  This is verified directly: `scripts/smoke-test.sh` passes at full count with the `kafka`
  container stopped.
- **Schema Registry down at startup** → `register_all()` retries a few times, then gives up
  quietly; each side still validates against its own locally-derivable schema
  (`common/events/*.py`), so **validation still happens** — only cross-version compatibility
  checking at the registry is unavailable until it comes back.
- **RabbitMQ down** → `notification-consumer` cannot enqueue, so it doesn't commit its Kafka
  offset — the affected messages are simply redelivered once RabbitMQ is back. Nothing
  upstream of Kafka is affected; a notification is late, not lost or duplicated.

---

## 7. Where each system's failure boundary actually sits

```
                     ┌─────────────────────────────────────────────────────────┐
                     │            can checkout/saga fail because of…            │
                     ├────────────────────────┬────────────────────────────────┤
                     │ Temporal down           │  YES — no saga can start        │
                     │                         │  or be signalled. Not a Week 3  │
                     │                         │  regression: the saga's state   │
                     │                         │  lives there and nowhere else.  │
                     ├────────────────────────┼────────────────────────────────┤
                     │ Jaeger down             │  No — spans drop silently       │
                     │ Prometheus down         │  No — counters grow unread       │
                     │ Grafana down            │  No — nothing reads Prometheus   │
                     │ Kafka down              │  No — outbox rows queue          │
                     │ Schema Registry down    │  No — local schema still used    │
                     │ RabbitMQ down           │  No — Kafka offset withheld       │
                     └────────────────────────┴────────────────────────────────┘
```

Everything below Temporal in that table is the machine-checkable claim behind D38 ("Kafka
carries facts, Temporal owns decisions") and it's exercised for real, not just asserted: the
smoke suite is run once with the full stack up and once with `kafka` stopped, and both runs are
required to pass at the same count before a phase is considered done. Temporal itself is the
one row that was never a candidate for that test — it's the platform's saga engine, not
observability tooling, and `scripts/saga-resilience-test.sh` exists specifically to prove the
saga survives failures *of the worker*, not of Temporal itself.

---

## 8. Quick reference — who to look at for what

### 8.1 Logging into each UI

Every UI here runs on `localhost` at a fixed port, published straight from
`docker-compose.yml` — none of them sit behind the Nginx gateway (they're not part of the
`/api/v1/…` surface the gateway proxies), and none of them are internet-facing on a real
deployment without a reverse proxy of their own in front. That's a known, named gap — see
[key-decisions.md](key-decisions.md)'s residuals list — acceptable for a local stack, not for
production as-is.

| UI | URL | Username | Password | Notes |
|---|---|---|---|---|
| **Temporal Web UI** | <http://localhost:8233> | — | — | **No auth at all.** Browse every workflow by id (`order-<ORDER_UUID>`), inspect its full event history, and see exactly which activity it's retrying or waiting on |
| **Jaeger UI** | <http://localhost:16686> | — | — | **No auth at all.** Pick a `service` (e.g. `order-service`) from the dropdown, hit "Find Traces" |
| **Prometheus UI** | <http://localhost:9090> | — | — | **No auth at all.** `/targets` shows scrape health per job; `/graph` runs ad-hoc PromQL |
| **Grafana** | <http://localhost:3000> | `admin` (Grafana's built-in default — not overridden here) | Whatever you set for `GRAFANA_ADMIN_PASSWORD` in your own `.env` | `GF_AUTH_ANONYMOUS_ENABLED=false`, so this is the one UI in the list that's actually behind a login. There is no fixed password checked in anywhere — see [Environment file](../README.md#environment-file) |
| **RabbitMQ Management UI** | <http://localhost:15672> | Whatever you set for `RABBITMQ_USER` | Whatever you set for `RABBITMQ_PASSWORD` | Same story as Grafana — both keys are `${VAR:?set … in the root .env}` anchors (D19), never a source-code default |
| **Schema Registry** | <http://localhost:8081> | — | — | REST API only, no web UI. `curl http://localhost:8081/subjects` lists what's registered |
| **Kafka itself** | `localhost:9092` (broker) | — | — | No web UI ships with this stack — inspect it with `docker compose exec kafka kafka-topics …` (see below), not a browser |

**Where the passwords actually come from:** `.env` is gitignored and never contains a fixed
value — you choose `GRAFANA_ADMIN_PASSWORD`, `RABBITMQ_USER` and `RABBITMQ_PASSWORD` yourself
when you create it (see the [README's Environment file section](../README.md#environment-file)
and `scripts/init_bootstrap.sh`, which generates the file with placeholders for you to fill
in). There is no "the password is X" answer this document can give — if you don't have your
own `.env` yet, run `scripts/init_bootstrap.sh` and open the file it creates.

### 8.2 Everything else

| You want to… | Look at |
|---|---|
| See one request's full call graph across services | Jaeger UI, <http://localhost:16686> |
| See an order's saga state, or replay it step by step | Temporal Web UI, <http://localhost:8233>, workflow id `order-<ORDER_UUID>` |
| Check whether a service is up and how fast it's answering | Prometheus, <http://localhost:9090/targets> and `/graph` |
| Watch business counters (orders placed/delivered/cancelled) | Grafana, <http://localhost:3000> — or query `sfo_business_*` in Prometheus directly |
| Check whether the outbox relay is keeping up | `sfo_outbox_backlog` / `sfo_outbox_lag_seconds` in Prometheus |
| Check whether the analytics or notification consumer is alive | `sfo_consumer_last_message_timestamp_seconds` — a flat line means wedged |
| Inspect a specific order's event history | `SELECT * FROM order_outbox WHERE aggregate_id = '<uuid>' ORDER BY seq` — see [README.md](../README.md#database-access) |
| List Kafka topics and partitions | `docker compose exec kafka kafka-topics --bootstrap-server localhost:9092 --describe` |
| See what schemas are registered | `curl http://localhost:8081/subjects` |
| Watch RabbitMQ's queue depth for the notification pool | RabbitMQ Management UI, <http://localhost:15672> → Queues |
