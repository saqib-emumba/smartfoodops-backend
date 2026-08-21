# SmartFoodOps — Order Saga: How Temporal Orchestration Actually Works

This document explains **the code as it exists today** — not the plan, not the blueprint's
intent, but what `services/order/workflows.py`, `activities.py`, `worker.py` and `main.py`
actually do, and how the three sibling services (Payment, Restaurant, Rider) participate.

- **What to build, and why it differs from the original plan** →
  [week2-temporal-orchestration-blueprint.md](week2-temporal-orchestration-blueprint.md)
- **Why each choice was made, against what alternative** →
  [key-decisions.md](key-decisions.md) (D25–D31)
- **This document** → how it runs, end to end, including every way it can fail

---

## 1. The actors, and who talks to whom

Only **two processes** in the whole platform hold a Temporal client. Every other service
stays completely unaware that an orchestrator exists — they see HTTP requests carrying
`X-Internal-Key`, exactly like any other internal call.

```
                     ┌───────────────────────┐
  bearer token       │      Nginx Gateway     │
 ───────────────────▶│   (path-based routing) │
                     └────────────┬───────────┘
   ┌──────────┬──────────┬────────┴─────┬───────────┬───────────┐
   ▼          ▼          ▼              ▼           ▼           ▼
 User      Restaurant   Menu         Order        Payment      Rider
 :8001       :8002     :8003         :8004         :8005       :8006
                                    │▲     gRPC
                              gRPC  ││
                                    ▼│
                            ┌───────────────┐
                            │ temporal-server│  :7233 gRPC
                            │               │  :8233 Web UI
                            └───────┬───────┘
                                    │ polls task queue "order-tasks"
                                    ▼
                            ┌───────────────┐
                            │ order-worker   │  runs every workflow + activity
                            └───────────────┘
```

`order-worker`'s activities and the Restaurant/Rider signal relays share the same credential
but travel in opposite directions:

```
 order-worker ──X-Internal-Key──▶ payment-service      (authorize / refund)
 order-worker ──X-Internal-Key──▶ restaurant-service   (send ticket / expire ticket)
 order-worker ──X-Internal-Key──▶ rider-service         (dispatch / release)

 restaurant-service ──X-Internal-Key──▶ order-service  POST /orders/{id}/signals
 rider-service      ──X-Internal-Key──▶ order-service  POST /orders/{id}/signals
```

- **`order-service`** starts a workflow when an order is created, and exposes the one door
  a signal can enter through: `POST /api/v1/orders/{id}/signals`.
- **`order-worker`** is a separate container running the same image, polling the
  `order-tasks` task queue. It hosts the workflow's *logic* and every *activity* — the actual
  HTTP calls out to Payment, Restaurant and Rider.
- **Payment, Restaurant, Rider** never import `temporalio`. They answer plain HTTP requests
  and, for Restaurant/Rider, occasionally call back into `order-service`'s signal endpoint to
  report something a human just did (a kitchen decision, a pickup, a delivery).

This split is what a worker restart can prove safe: the workflow's *state* lives inside
Temporal's own storage, not inside `order-worker`'s process memory. Kill the container, and
the next worker to poll the same task queue picks the workflow up exactly where it left off.

### The three things a workflow can do

Everything in `workflows.py` reduces to three primitives, and keeping them separate is what
makes the workflow replayable:

| Primitive | Direction | Used for | Example in this saga |
|---|---|---|---|
| **Activity** | workflow → outside world | any side effect (HTTP call, DB write) | `authorize_payment_activity` |
| **Signal** | outside world → workflow | reporting an event that already happened | `restaurant_decision`, `rider_pickup`, `rider_delivery` |
| **Query** | outside world → workflow (read-only) | asking the workflow's current state, with no side effect | `stage()` |

Signal handlers in this codebase only ever set a field on `self` — they never call an
activity directly (`workflows.py:80-100`). Only the `run()` coroutine, on its own turn,
reacts to what a signal set. That is what keeps the ordering of side effects deterministic
regardless of exactly when a signal happens to arrive.

`WorkflowSignalRequest.signal` (`schemas.py`) is a Pydantic `Literal["restaurant_decision",
"rider_pickup", "rider_delivery"]`, so a typo in a signal name is a `422` at the relay
endpoint rather than a signal Temporal accepts and nothing ever reads.

### How the Order Service wires into Temporal

`order-service`'s FastAPI app has two dependencies that each need setup and teardown — the
Postgres pool and the Temporal connection — composed with one `AsyncExitStack`
(`main.py:63-74`):

```
 lifespan()
   ├─ enter db.lifespan(app)         → PostgresPool opens its connection pool
   └─ enter temporal.lifespan(app)   → TemporalGateway connects to temporal-server
                                        (failure here is logged, not fatal — orders
                                         must stay creatable even with Temporal down)
```

`GET /api/v1/orders/health` reports `temporal_reachable` independently of
`database_reachable` — the two failure modes mean different things (§6.3).

---

## 2. The state machine

```
 created
    │  authorize_payment_activity
    ├── declined ─────────────────────────────────────────▶ cancelled  (no refund needed)
    │
    ▼ authorized
 confirmed
    │  send_ticket_activity
    ▼
 awaiting_kitchen
    │  restaurant_decision signal, or 120s silence
    │
    │  on timeout: read the ticket back before concluding anything —
    │  a decision is committed before the signal carrying it is sent,
    │  so silence may just mean the relay was lost (§6.5)
    │
    ├── rejected, or genuinely no decision ───────────────▶ COMPENSATE ──▶ cancelled
    │
    ▼ accepted (signalled, or recovered from the ticket)
 searching_for_rider
    │  dispatch_rider_activity, up to 6× / 10s apart
    ├── no rider found ───────────────────────────────────▶ COMPENSATE ──▶ cancelled
    │
    ▼ assigned                         (rider_id recorded on the order)
 assigned
    │  rider_pickup signal, timeout 1h
    ├── no pickup ────────────────────────────────────────▶ COMPENSATE ──▶ cancelled
    │
    ▼ picked_up
 picked_up
    │  rider_delivery signal, timeout 1h
    ├── no delivery ──────────────────────────────────────▶ COMPENSATE ──▶ cancelled
    │
    ▼ delivered
 delivered   (terminal — rider released here, after the transition)
```

`created` and `confirmed` are the only two states with no compensation on the way out — no
money has moved yet, so a decline just cancels. Every arrow into `COMPENSATE` from
`awaiting_kitchen` onward runs the full rollback sequence in §6, because by that point a card
has been charged and, sometimes, a rider has been claimed.

`orders.status` is a Postgres enum, `('created','confirmed','assigned','picked_up',
'delivered','cancelled')`, and every write to it goes through one guarded SQL statement —
see §5. (`searching_for_rider` above is a saga-internal stage, not a stored status — the
column only ever holds `confirmed` until a rider is actually found.)

---

## 3. One full run, step by step

```
CUSTOMER
   │ POST /api/v1/orders  (X-Idempotency-Key)
   ▼
ORDER-SERVICE
   │ re-price against the live menu, verify customer + restaurant over HTTP
   │ INSERT order + opening 'created' trail entry — one transaction
   │ start_workflow(id="order-<uuid>", id_conflict_policy=USE_EXISTING)
   ▼                                                  ┌─▶ 201 OrderResponse to CUSTOMER
TEMPORAL  ── hands the run to ──▶  ORDER-WORKER
                                        │
                                        ▼
                          authorize_payment_activity ────────▶ PAYMENT SERVICE
                                        │  (declined → cancel, no refund needed)
                                        ▼ authorized
                              transition → confirmed
                                        │
                                        ▼
                            send_ticket_activity ───────────▶ RESTAURANT SERVICE
                                        │
                          wait_condition(restaurant_decision, timeout=120s)
                                        │
                                        │◀── signal: restaurant_decision ─── RESTAURANT SVC
                                        │        (relayed from an admin's POST .../accept)
                                        ▼ accepted
                          dispatch_rider_activity  (×1..6, 10s apart) ─────▶ RIDER SERVICE
                                        │
                                        ▼ assigned
                              transition → assigned  (rider_id set)
                                        │
                          wait_condition(picked_up, timeout=1h)
                                        │◀── signal: rider_pickup ────────── RIDER SERVICE
                                        │        (relayed from a rider's POST .../picked-up)
                                        ▼
                              transition → picked_up
                                        │
                          wait_condition(delivered, timeout=1h)
                                        │◀── signal: rider_delivery ──────── RIDER SERVICE
                                        │        (relayed from a rider's POST .../delivered)
                                        ▼
                              transition → delivered
                                        │
                            release_rider_activity ───────────▶ RIDER SERVICE
                                        ▼
                                     [ end ]
```

Two things worth naming that this diagram can't show:

- **Every `wait_condition` is a durable timer**, not a blocked thread or an open connection.
  While the workflow is "waiting", `order-worker` holds nothing for it — no memory, no
  socket. It could be killed and restarted a dozen times during that 120-second window and
  the wait resumes exactly where it was.
- **Signals never call activities directly** (§1). The `run()` coroutine is what reacts to
  them, on its own turn — never the signal handler itself.

---

## 4. The two kinds of "no"

The single most important discipline in `activities.py` is this rule, applied consistently:

> **A business outcome is not a transport failure.**

| What happened | Raised as | Temporal's behaviour |
|---|---|---|
| Card declined, kitchen rejects/at capacity | `ApplicationError(non_retryable=True)` | Never retried — the workflow's `try/except ActivityError` catches it immediately |
| Payment/Restaurant/Rider service unreachable, 5xx, timeout | a normal exception (`ServiceClient` raises `503`/`502` as `HTTPException`, surfaced as `ActivityError`) | Retried under the activity's `RetryPolicy` |

This distinction is why `dispatch_rider_activity` returns `{"assigned": false}` instead of
raising when no rider is free — an empty fleet is not a service failure, it's an answer the
*workflow* decides how to act on (wait 10s, try again). And it's why a kitchen's rejection
compensates on the **first** attempt rather than after three retries — the bug the original
blueprint shipped (see §0.3 of the blueprint doc).

The workflow unwraps the real cause when logging or recording a reason
(`str(exc.cause or exc)` in `workflows.py`), so a cancellation's audit-trail entry names the
actual failure rather than Temporal's wrapper exception.

Three retry policies, each sized to what it protects:

```
 TRANSIENT      2s ▸ 4s ▸ 8s ▸ 16s ▸ 30s(cap)         max 3 attempts   forward progress
 STATE          1s ▸ 2s ▸ 4s ▸ 8s ▸ 16s ▸ 20s(cap)     max 5 attempts   local DB write
 COMPENSATION   2s ▸ 4s ▸ 8s ▸ 16s ▸ 32s ▸ 60s(cap)     max 10 attempts refund / release
```

Compensation retries hardest, deliberately: a failed refund leaves a customer charged for
food that will never arrive, which is the worst state this system can reach. Giving up on a
rollback is worse than giving up on forward progress.

---

## 5. Idempotency: every write survives being repeated

Temporal's execution guarantee is **at-least-once**, not exactly-once. A worker can die
after an activity's side effect completed but before Temporal recorded that it did — and the
next worker will run that activity again. Every single write in this saga is built to
survive that.

| Write | Guard | Where |
|---|---|---|
| Order status transition | Compare-and-set: `status <> new AND status NOT IN ('delivered','cancelled') AND (new='cancelled' OR new > status)` | `order/repository.py::OrderRepository.transition` |
| Payment authorization | Idempotency key **derived** from the order id (`wf-pay-{order_id}`), enforced by `UNIQUE(idempotency_key)` | `activities.py::payment_key`, `payment/repository.py` |
| Payment refund | Idempotent by **status**, not by key — a `refunded` row is returned unchanged | `payment/repository.py::mark_refunded` |
| Kitchen ticket creation | `order_id UNIQUE`, `ON CONFLICT DO NOTHING` | `restaurant/repository.py::enqueue` |
| Ticket accept / reject / expire | `WHERE status = 'pending'` — only the first decision sticks | `restaurant/repository.py::decide` / `expire` |
| Rider dispatch | Checks `current_order_id` **before** claiming; a retry that skipped this would strand the first rider | `rider/repository.py::dispatch` |
| Rider release | Zero rows affected = already released = success | `rider/repository.py::release` |
| Saga start | `WorkflowIDConflictPolicy.USE_EXISTING` + workflow id = `order-{order_id}` | `order/main.py::_start_saga` |

That is seven of the eight activities registered on the worker
(`worker.py::Worker(activities=[...])`), plus the one non-activity idempotency guard (saga
start) that lives in the HTTP handler instead. The eighth activity, `read_ticket_activity`,
is absent from this table because it is the only pure **read** in the set — it has no side
effect to make idempotent (§6.5).

The transition guard deserves a closer look, because it does three jobs in one `WHERE`
clause:

```sql
UPDATE orders
   SET status = %(new_status)s::order_status, rider_id = COALESCE(%(rider_id)s, rider_id), ...
 WHERE id = %(order_id)s
   AND status <> %(new_status)s::order_status                  -- ❶ a replay is a no-op
   AND status NOT IN ('delivered', 'cancelled')                 -- ❷ terminal states are final
   AND (%(new_status)s = 'cancelled' OR %(new_status)s > status) -- ❸ only forward, or cancel
RETURNING ...
```

❸ works because Postgres compares an enum by **declaration order**, and `order_status` was
declared in lifecycle order — so `'delivered' > 'assigned'` is simply `true`. No lookup
table needed, but it's a property worth knowing before anyone reorders that `CREATE TYPE`.

When the guard matches nothing, **no trail entry is written either** — the status write and
its audit-log entry share one transaction (`transition_order_activity` →
`OrderRepository.transition`), so a five-times-retried activity produces exactly one row in
`order_tracking_logs`, not five.

---

## 6. Failure scenarios and how each recovers

Every compensation path — regardless of which state it started from — runs the same three
steps, in this order:

```
   refund_payment_activity    always: money already moved, give it back first
        │
        ▼
   release_rider_activity     only if a rider had actually been claimed
        │
        ▼
   expire_ticket_activity     always: frees the kitchen's pending-ticket capacity slot
        │
        ▼
   transition → cancelled
```

Refund goes first because it is the customer's money. The rider release comes next, and only
conditionally — the first revision of the blueprint refunded and stopped, which is exactly
how every dispatched rider ended up leaking `is_available = FALSE` forever.

### 6.1 Business-outcome failures (each is a **first-attempt**, non-retried decision)

Every trigger below is caught on its **first** occurrence — none of these are retried, per
§4 — and each either skips straight to `cancelled` or funnels into the three-step
compensation shown above:

```
 card declined ─────────────────────────────────────▶ cancel only
                                                        (nothing was ever charged)

 kitchen rejects        ┐
 kitchen silent (120s)  │
 kitchen at capacity *  ├──────────────────────────▶ COMPENSATE ──▶ cancelled
 no rider (6× / 10s)    │                             refund → release † → expire
 pickup timeout (1h)    │
 delivery timeout (1h)  ┘

   * capacity refusal has no ticket to expire yet — see the note below
   † release only runs if `self._rider_id` was actually set (i.e. one had been claimed)
```

| Scenario | Detected by | Compensation run |
|---|---|---|
| **Card declined** | `authorize_payment_activity` sees `payment.status != "authorized"` | None — nothing was charged. Straight to `cancelled`. |
| **Kitchen rejects** | `restaurant_decision` signal carries `"rejected"` | Refund → release rider (none claimed yet) → expire ticket → `cancelled` |
| **Kitchen never answers** | `wait_condition(..., timeout=120s)` raises `asyncio.TimeoutError`, **then** `read_ticket_activity` confirms the ticket is still `pending`/`expired`/absent | Same as above — only genuine silence is treated as a refusal (§6.5) |
| **Kitchen at capacity** | `send_ticket_activity` sees `{"queued": false}` | Refund → cancel (no ticket was ever created — see note below) |
| **No rider within 10km after 6 tries (60s)** | `dispatch_rider_activity` returns `{"assigned": false}` every time | Refund → release rider (none claimed) → expire ticket → `cancelled` |
| **Rider never picks up (1h)** / **never delivers (1h)** | `wait_condition` timeout | Full compensation, including releasing the rider who *was* claimed |

> Capacity refusal is the one compensation path that reaches `_compensate()` with no ticket
> to expire. `expire_ticket_activity` handles this gracefully — the Restaurant Service
> answers `422 "no ticket to expire"`, which the activity catches and logs rather than
> propagating, since a lost capacity-slot cleanup must never block the refund that actually
> matters to the customer (`activities.py::expire_ticket_activity`).

### 6.2 Transport failures (retried automatically, invisible to the customer)

The dangerous case isn't the failure itself — it's a failure that happens **after** the side
effect already succeeded, where only the *response* was lost. This is exactly why §5's
idempotency guards exist, and here is the concrete path through one of them:

```
 time ──▶

 attempt 1   order-worker: authorize_payment_activity ──▶ Payment Service
             Payment Service: creates the row, authorizes it ── SUCCEEDS
             order-worker: ✗ times out before the response arrives
                    │
                    │  TRANSIENT backoff: wait 2s
                    ▼
 attempt 2   order-worker: authorize_payment_activity ──▶ Payment Service
             (same idempotency key: "wf-pay-<order_id>")
                    │
             Payment Service: UNIQUE(idempotency_key) already exists
             ──▶ returns the SAME row, untouched — no second charge
                    ▼
             workflow proceeds exactly as if attempt 1 had reported success
```

| Scenario | What happens |
|---|---|
| Payment/Restaurant/Rider service briefly unreachable | The activity's `TRANSIENT` policy retries 3× (2s → 4s → 8s) before the workflow ever sees a failure |
| A retried activity's side effect already landed (e.g. payment was authorized, but the response was lost) | The retry hits the idempotency guard (§5) and returns the same result — no double charge, no second rider |
| Signal relay (Restaurant/Rider → order-service) transiently fails | The **caller** gets a `502`/`503` from `ServiceClient` and can retry the HTTP call itself; nothing in the saga is lost because nothing was recorded as delivered until Temporal acknowledges the signal |

### 6.3 Process failures — the property this whole design exists to prove

**Case A — the worker is killed while parked on a durable timer.** Nothing is lost, because
nothing about the wait ever lived in the worker's own memory:

```
 t0  order-worker-A   send_ticket_activity completes ──▶ recorded in Temporal's history
 t1  order-worker-A   parks on wait_condition(restaurant_decision, timeout=120s)
 t2  order-worker-A   ✗ container killed  (crash, or `docker compose stop`)
              │
              │   the workflow's state lives in Temporal's history, not in
              │   order-worker-A's process — so nothing here is actually at risk
              ▼
 t3  order-worker-B   starts, polls task queue "order-tasks"
 t4  order-worker-B   Temporal replays the history deterministically —
                       reconstructs "ticket already sent, now waiting on the timer"
 t5  restaurant admin accepts ──signal──▶ order-worker-B resumes run() right here
 t6  order-worker-B   dispatch_rider_activity, ... continues exactly as normal
```

**Case B — the worker is killed while an activity call is actually in flight.** The
side effect may have already landed on the far end; the idempotency guard (§5) is what makes
re-running it safe rather than a double charge:

```
 t0  order-worker-A   authorize_payment_activity starts, calls Payment Service
 t1  Payment Service  creates the payment row, authorizes it — SUCCEEDS
 t2  order-worker-A   ✗ killed before it can report the result back to Temporal
              │
              ▼
 t3  order-worker-B   Temporal never received a completion → re-executes the SAME activity
 t4  order-worker-B   → Payment Service: authorize, idempotency key "wf-pay-<order_id>"
 t5  Payment Service  UNIQUE(idempotency_key) already exists → returns the SAME row
 t6  order-worker-B   sees status="authorized" → proceeds — no double charge
```

| Scenario | What happens |
|---|---|
| **`order-worker` killed while a workflow is mid-run** | Temporal already has the workflow's full event history. Any worker that next polls `order-tasks` replays it deterministically and resumes at the last completed step. Nothing lost, nothing repeated. |
| **`order-worker` killed while an activity is in flight** | The activity re-executes once a worker resumes (at-least-once) — exactly why every activity in §5 is idempotent |
| **`order-worker` killed while parked in a `wait_condition`** | No effect — the wait is state inside Temporal's own persistence, not the worker's memory |
| **`order-worker` receives SIGTERM (`docker compose stop`)** | `worker.py` traps `SIGINT`/`SIGTERM` and stops accepting new work; `graceful_shutdown_timeout=30s` gives in-flight activities time to finish before Temporal cancels them — after which they simply get retried by the next worker |
| **`order-service` down when a signal needs to be delivered** | The Restaurant/Rider Service's `ServiceClient.post` call fails with `503`; the *caller* (an admin's browser, a rider's app) sees the failure and can retry. The saga itself is untouched and still parked on its timer. |
| **Temporal unreachable when `POST /api/v1/orders` runs** | `_start_saga` catches the exception, logs at `error`, and returns normally — the order is still `201 created`. It sits at `created` until a client retries the same idempotency key, which re-attempts `_start_saga` (`order/main.py:158-205`). |

`scripts/saga-resilience-test.sh` asserts the two properties above directly against a live
stack: it restarts `order-worker` **twice** mid-saga (once while waiting on the kitchen, once
mid-delivery) and asserts the order still reaches `delivered` with exactly one trail entry
per transition — proving the compare-and-set in §5 absorbed the replayed activities without
duplicating anything.

### 6.4 The concurrency case

`rider/repository.py::dispatch` claims a rider with a single statement:

```sql
UPDATE riders SET is_available = FALSE, current_order_id = %(order_id)s
 WHERE id = (
   SELECT id FROM riders
    WHERE is_available AND current_order_id IS NULL AND haversine_km(...) <= 10
    ORDER BY haversine_km(...) LIMIT 1
    FOR UPDATE SKIP LOCKED
 ) AND is_available
```

```
 Txn A (order 1):  SELECT ... FOR UPDATE SKIP LOCKED  → locks rider R1 → claims R1
 Txn B (order 2):  SELECT ... FOR UPDATE SKIP LOCKED  → R1 is locked, skips it
                                                         → locks R2 → claims R2
                    (no wait, no error, no retry — both succeed on the first attempt)
```

Two workflows dispatching for two different orders **at the same instant** never contend:
whichever transaction gets there first locks its candidate row, and `SKIP LOCKED` makes the
second transaction simply skip past it to the next-nearest rider. `saga-resilience-test.sh`
proves this by firing four orders at a fleet of one rider simultaneously: exactly one gets
assigned, and the other three run the full compensation path.

### 6.5 A lost kitchen decision — and why a timeout is not trusted on its own

The signal relay has an unavoidable ordering problem: the Restaurant Service must commit the
kitchen's decision **before** it can relay it, because a human pressed a button and must not
see an error for something that worked. So a relay lost in flight leaves a ticket that says
`accepted` and a workflow still sitting on its timer.

Treating the timer expiring as "the kitchen refused" would then refund a customer whose order
had actually been taken. So the timeout is a prompt to **go and look**, not a conclusion:

```
 t0   send_ticket_activity ──▶ ticket created, status='pending'
 t1   workflow parks on wait_condition(restaurant_decision, timeout=120s)

 t2   admin: POST /restaurants/tickets/<id>/accept
 t3   Restaurant Service: UPDATE order_tickets SET status='accepted'   ── COMMITTED
 t4   Restaurant Service ──relay──▶ order-service /signals   ✗ LOST
              │
              │   the decision is on record; the workflow has no idea
              ▼
 t5   120s elapses — wait_condition raises asyncio.TimeoutError
 t6   _recover_kitchen_decision() → read_ticket_activity
                                     GET /restaurants/tickets/<order_id>
 t7   ticket says 'accepted'  ──▶ resume as if the signal had arrived
                                   (dispatch a rider; NO refund)
```

The three possible answers, and what each means:

```
 ticket says 'accepted'          ──▶ the signal was lost; continue to rider dispatch
 ticket says 'rejected'          ──▶ the signal was lost; compensate (refund + cancel)
 'pending' / 'expired' / absent  ──▶ genuine silence; compensate (refund + cancel)

 Restaurant Service unreachable
 after TRANSIENT retries         ──▶ treated as no decision → compensate
                                      (deliberate bias: refunding an accepted order is
                                       recoverable by a human, but leaving a charged
                                       customer on a saga that never finishes is not)
```

`scripts/saga-resilience-test.sh` §4 exercises this by writing the decision straight into
`sfo_restaurant_core` — which is exactly what an accept whose relay died looks like from the
outside — and asserts that a lost *acceptance* completes the order without a refund, while a
lost *rejection* still cancels and refunds.

---

## 7. What is *not* handled yet

Recorded honestly, because a diagram that hides its gaps is worse than no diagram:

- **A lost rider signal does not self-correct.** §6.5 closes this for the kitchen, because
  there is a ticket to read back. There is no equivalent for `rider_pickup` or
  `rider_delivery`: `riders.current_order_id` records *who* is carrying an order, not how far
  along they are, so a lost pickup or delivery signal is only caught by the 1-hour timeout,
  which then compensates an order that may well have arrived. An outbox table in the Rider
  Service — or a `rider_order_progress` column to read back the same way — is the fix.
  (`key-decisions.md`, D24/D27.)
- **The rider search window and the kitchen decision window are both fixed constants**
  (`common/config.py`), not tuned per restaurant or per city density.
- **`system_admin` bypasses every ownership check** in every service that has one — a
  standing, unaudited convenience noted in `key-decisions.md`'s open questions.
