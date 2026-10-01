# Removing the Transactional Outbox in Favor of Temporal — Design

Status: **implemented** ([D53](key-decisions.md#d53--the-outbox-tables-are-removed-workflow-history-becomes-the-publish-ledger),
superseding [D39](key-decisions.md#d39--a-transactional-outbox-not-a-post-commit-publish)).
This doc is the design for the mentor's direction to remove `order_outbox`/`payment_outbox`
and their relays altogether, and use Temporal to handle event delivery instead. It composes
with (does not replace)
[readme/order-creation-temporal-update-design.md](order-creation-temporal-update-design.md),
which already moves one of the five outbox call sites (`order.created`) inside Temporal for an
unrelated reason (closing D25's atomicity gap); this doc covers the other four plus the two
call sites `order-creation-temporal-update-design.md` doesn't touch.

## 1. What the outbox actually guarantees, and why removing the table isn't free

[D39](key-decisions.md#d39--a-transactional-outbox-not-a-post-commit-publish) exists because
a Kafka publish and a Postgres commit are two independent systems with no shared transaction —
Temporal can't enlist in a Postgres transaction either, a fact
[order-creation-temporal-update-design.md §1](order-creation-temporal-update-design.md#1-the-problem)
already quotes from D25. Today's answer is to make the *fact that publishing must happen* part
of the same Postgres transaction as the business write, via `append_outbox()`, then let a
background relay (one per service, in-process, see `services/common/outbox.py`) retry the
actual Kafka `send` until it succeeds. The DB row is the durable ledger entry; the relay is
just a worker that keeps trying against it.

Deleting `order_outbox`/`payment_outbox` without a replacement ledger reopens exactly the hole
D39 closed: a crash between the business write and a bare `await producer.send(...)` loses the
event permanently, with no row anywhere recording that it was ever owed. **The mentor's ask
("use Temporal for handling the scenarios") is achievable, but only if Temporal's own execution
history takes over the ledger's job** — not by calling Temporal *after* the write commits (that
is the exact post-commit-publish shape D39 was written against), but by making the write and the
publish two sequential Temporal activities in the same workflow.

## 2. The replacement mechanism: workflow history as the ledger

A Temporal workflow's history is durable and append-only. If a workflow executes activity A
(the DB write) and it completes, that fact is recorded in history *before* the workflow ever
attempts activity B (the Kafka publish). If the worker crashes at any point — before A, between
A and B, or during B — replay reconstructs exactly where execution was: A's completion is never
re-run (activities aren't idempotent-by-magic, but Temporal's own replay guarantees A is not
re-attempted once its completion is in history), and B is retried per its `RetryPolicy`
(exponential backoff, unlimited by default) until it succeeds or is explicitly abandoned as
non-retryable. This is the same at-least-once, retry-until-success contract the outbox relay
already provides — the ledger just moved from a Postgres table to Temporal's history, and the
retry loop moved from a bespoke `asyncio.Task` poller to Temporal's own activity retry machinery
(which is also what already runs every other activity in this platform, so there is nothing new
to operate).

Two things carry over unchanged from D39 and don't get easier or harder:

- **Downstream consumers still dedup on `event_id`.** At-least-once is at-least-once regardless
  of which system provides it; Analytics's `(consumer_group, event_id)` dedup
  ([D44](key-decisions.md#d44--the-analytics-service-gets-its-own-database-and-dedups-by-consumer_group-event_id))
  needs no change.
- **[D38](key-decisions.md#d38--kafka-carries-facts-temporal-still-owns-decisions)'s
  producer-free-workflow constraint still holds.** `@workflow.defn` code may not do I/O — the
  actual Kafka `send` must live in an *activity*, never in `OrderWorkflow.run` itself. This
  design adds activities, never workflow-body I/O.

One thing gets **better**, not just replaced: ordering. Today, order and payment events for one
order are only guaranteed in-order because a single relay claims and publishes its batch in
strict `seq` order — a second relay racing the same table could reorder them (the reason
`services/common/outbox.py` explicitly runs one relay per table, never two). Under this design,
a single workflow execution is single-threaded and deterministic: activity B for
`order.confirmed` only starts after activity B for the *previous* lifecycle event has already
returned (i.e. its Kafka send was ack'd), because that's what "the activity completed" means.
Ordering falls out of the workflow's own program order for free, with no relay-uniqueness
invariant to maintain.

## 3. Where the Kafka publish activity actually runs

[D36](key-decisions.md#d36--the-order-sagas-workflow-and-worker-split-into-their-own-deployable)
still applies: `orchestrator-worker` has no direct database access and, per D38, no Kafka client
either — "the Orchestrator Service holds no Kafka client at all — it produces nothing." Neither
constraint is being relaxed. The new publish activity follows the exact shape every other
activity in this platform already uses: it's a thin HTTP-calling shim in `orchestrator-worker`
that crosses back into the *owning* service (order-service or payment-service) with
`X-Internal-Key`, and the owning service's own process — which already holds the Kafka producer
today, for the relay — does the actual `send_and_wait`. The only change from today's
`OutboxRelay` is that the producer call now lives behind a new internal HTTP endpoint invoked
once by an activity, instead of behind a polling loop reading a table.

Concretely, per event-producing write, the workflow now runs two activities instead of one:

```
await workflow.execute_activity(transition_order_activity, ...)   # DB write only, no outbox row
await workflow.execute_activity(publish_order_event_activity, ...) # POST .../internal/events -> Kafka send, retried by Temporal until ack'd
```

`transition_order_activity` / `OrderRepository.transition()` lose their `append_outbox()` call
entirely — they become pure DB writes, same CAS guard as today
([D31](key-decisions.md#d31--a-status-transition-is-a-compare-and-set-and-the-enum-supplies-the-ordering)).
The new `publish_order_event_activity` carries everything the event payload needs (event type,
aggregate id, the transition's own return value) so it never needs to re-read the DB.

## 4. The two writes that have no workflow to attach to today

Two of the five outbox call sites don't run behind any existing activity, so the DB write itself
has to move into Temporal first, not just gain a publish step after it. Confirmed with the user:

### 4a. `order.kitchen.decided` — becomes a Temporal Update

Today, `POST /orders/{id}/accept|reject` (`services/order/apis/kitchen.py`) writes the decision
directly via `OrderRepository.decide_kitchen()` (which also appends the outbox row today), then
best-effort signals `restaurant_decision` to the running workflow. The DB write always succeeds
even if the signal is lost — that's *why* `OrderWorkflow` also has a 120s timeout and a
`read_kitchen_decision_activity` fallback ([D32](key-decisions.md#d32--the-kitchen-queue-collapsed-into-the-orders-table)).

**Decided:** convert this into a Temporal **Update** against the already-running
`order-{order_id}` workflow — the workflow is guaranteed running by the time a restaurant can
act, so no Update-with-Start is needed, unlike order creation. The external contract is
unchanged (`POST /orders/{id}/accept|reject` still returns the same shape); internally, the
handler calls `client.execute_update(workflow_id=f"order-{order_id}", update="kitchen_decision",
args=[...])` instead of writing the DB directly. The Update handler in `OrderWorkflow` runs
`decide_kitchen_activity` (the DB write, unchanged from today's repository logic minus the
outbox insert) then `publish_order_event_activity("order.kitchen.decided", ...)`, then unblocks
the `wait_condition` that today's `restaurant_decision` signal unblocks.

**Cost, named explicitly:** this makes Temporal a hard dependency for kitchen accept/reject.
Today the DB write always commits regardless of Temporal's availability (only the *notification*
was best-effort); after this change, if Temporal is unreachable, a restaurant literally cannot
accept or reject an order — same shape of trade D51/D52 and
`order-creation-temporal-update-design.md §7` already document elsewhere in this platform: a
`503`, and a retry once Temporal is back.

### 4b. `payment.authorized` via the direct/manual endpoint — its own minimal workflow

`POST /api/v1/payments` (`services/payment/apis/payments.py::process_payment`) sits entirely
outside the saga — [D30](key-decisions.md#d30--the-saga-owns-payment-authorisation-so-post-apiv1payments-now-answers-409)
already restricts it to non-orchestrated use (it collides on `UNIQUE(order_id)` and returns
`409` for any order the saga already paid), leaving it as, in D30's own words, "left for direct
and manual use." There's no saga workflow to attach an activity to.

**Decided:** give it its own minimal single-activity workflow (e.g. `ManualPaymentWorkflow`),
started synchronously from the HTTP handler via `client.execute_workflow(...)` (blocking until
the workflow — one activity for the DB write via the existing `authorise()` helper, one activity
for the Kafka publish — completes and returns the payment). Workflow ID is derived
deterministically from the same natural key `process_payment` already uses to reject duplicates
(`order_id`), with `WorkflowIDConflictPolicy.USE_EXISTING` — consistent with every other
workflow-addressing convention already in this codebase (D25, D47), so a retried request routes
to the same execution rather than double-authorizing.

**Cost, named explicitly:** a path that has zero Temporal dependency today gains one — Temporal
down means this endpoint is down too, not degraded. Given the endpoint is already documented as
a manual/testing side door rather than the platform's real payment path, this is judged an
acceptable, narrow cost.

## 5. What gets deleted

- `order_outbox` DDL (`db/order/init.sql`) and `payment_outbox` DDL (`db/payment/init.sql`).
- `append_outbox()` and the `OutboxRelay` class (`services/common/outbox.py`) — the whole file's
  reason to exist goes away; whatever's left (if anything reusable) gets folded into the new
  publish-activity HTTP handlers directly.
- The two `OutboxRelay(...)` constructions and their `lifespan` composition in
  `services/order/deps.py` and `services/payment/deps.py`.
- The `sfo_outbox_backlog` / `sfo_outbox_lag_seconds` Prometheus gauges and the 7-day `_prune`
  job — there's no longer a table to prune or measure backlog on.
- Every `append_outbox(...)` call site: `OrderRepository.create()` (superseded by
  `order-creation-temporal-update-design.md`'s own removal of this method's outbox call),
  `OrderRepository.transition()`, `OrderRepository.decide_kitchen()`,
  `PaymentRepository.mark_authorized()`, `PaymentRepository.mark_refunded()`.

## 6. What this costs, beyond the two named hard-dependency additions

- **Lost:** the DB-queryable backlog (`SELECT * FROM order_outbox WHERE published_at IS NULL`)
  ops used for ad-hoc debugging. Replacement is Temporal Web's pending-activity view plus
  Temporal's own metrics (port 9233) — which D38 already argued is the platform's preferred
  observability channel over a second, potentially-disagreeing one ("the saga stays observable
  through traces and Temporal's own metrics, not through a second channel that could disagree
  with what Temporal itself records"). This design is that same argument, applied one layer
  further.
- **Gained:** more workflow volume — every kitchen decision and every manual payment is now a
  workflow execution (Update or a short-lived new workflow), where before it was a plain HTTP
  request. Temporal namespace retention needs to comfortably cover these in addition to the
  long-running `OrderWorkflow`s; worth an explicit check against whatever retention is configured
  today, since it was previously sized only for order sagas.
- **Unchanged:** at-least-once delivery and the resulting `event_id` dedup requirement on every
  consumer — this design relocates the durability mechanism, it does not strengthen or weaken
  the delivery guarantee itself.

## 7. File-level changes

| File | Change |
|---|---|
| `db/order/init.sql`, `db/payment/init.sql` | Remove `order_outbox` / `payment_outbox` DDL. |
| `services/common/outbox.py` | Remove `append_outbox()` and `OutboxRelay`; file likely deleted entirely. |
| `services/order/deps.py`, `services/payment/deps.py` | Remove `OutboxRelay` construction and its `lifespan` composition. |
| `services/order/repositories/orders.py` | `transition()`, `decide_kitchen()` drop their `append_outbox()` calls; `create()`'s removal already covered by `order-creation-temporal-update-design.md`. |
| `services/payment/repositories/payments.py` | `mark_authorized()`, `mark_refunded()` drop their `append_outbox()` calls. |
| `services/order/apis/kitchen.py` | `accept_order`/`reject_order` become thin wrappers issuing a Temporal Update instead of a direct DB write + best-effort signal. |
| `services/order/apis/internal_events.py` (new) | New `POST /orders/{id}/internal/events` (or per-event-type routes), `require_internal`-gated — does nothing but produce the given event to Kafka. Called once per publish activity. |
| `services/payment/apis/internal_events.py` (new) | Same shape, for `payment.authorized`/`payment.refunded`. |
| `services/orchestrator/workflows/order.py` | New `kitchen_decision` `@workflow.update` handler (DB-write activity + publish activity, unblocks the existing wait); every place that calls `transition_order_activity` now also calls a new `publish_order_event_activity` right after. |
| `services/orchestrator/activities/order.py`, `clients/order/order_service.py` | New `publish_order_event_activity`, `decide_kitchen_activity`; corresponding client methods. |
| `services/orchestrator/workflows/manual_payment.py` (new) | `ManualPaymentWorkflow` — one activity for the DB write (reusing `authorise()`), one for the publish. |
| `services/payment/apis/payments.py` | `process_payment` starts `ManualPaymentWorkflow` via `execute_workflow`, deterministic id from `order_id`, `USE_EXISTING` conflict policy. |
| `services/orchestrator/registry.py` | Register `ManualPaymentWorkflow` and the new activities. |
| `readme/key-decisions.md` | New decision entry superseding D39. |

## 8. Verification plan

- Full `scripts/smoke-test.sh` should pass with no assertion changes to status codes or response
  shapes — the external contract is unchanged by design.
- New cases worth adding: kitchen accept/reject with Temporal stopped → `503`, not a silent
  DB-only success; a manual payment retried with the same `order_id` → routes to the same
  workflow execution rather than double-authorizing; ordering check — force two lifecycle
  transitions to race and confirm the events still land on the topic in workflow program order.
- Re-verify `saga-resilience-test.sh` end-to-end, since every activity in the saga's happy path
  now has a second activity chained after it — specifically confirm a worker kill between the
  transition activity and its publish activity still results in exactly the publish being
  retried on restart, not the transition being redone.
