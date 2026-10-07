# Order Creation via Temporal Update-with-Start — Design

Status: **implemented** ([D54](key-decisions.md#d54--order-creation-moves-inside-temporal-via-update-with-start),
superseding [D25](key-decisions.md#d25--temporal-orchestrates-the-order-lifecycle-and-the-workflow-id-is-the-order-id)).
This doc is the design for moving `POST /api/v1/orders`'s database write behind Temporal, per
the mentor's direction that order creation should go through Temporal rather than directly
through the Order Service, scoped to order creation only (not every write endpoint
platform-wide).

> **Partly superseded by [D58](key-decisions.md#d58--checkout-starts-orderworkflow-and-waits-for-its-result-post-payment-work-moves-to-fulfillmentworkflow).** At the mentors'
> direction the API no longer uses Update-with-Start: it starts `OrderWorkflow` with the cart
> as its start argument and waits for that workflow's result. The insert this document
> moves behind Temporal is now the first step of `OrderWorkflow.run()`, not a `create_order`
> Update handler, and the API answers after payment authorisation rather than after the
> insert. §3 (the derived order id) and §4 (racing duplicates) still hold; §2 and §5 describe
> the replaced mechanism.

## 1. The problem

Today (`services/order/apis/checkout.py::create_order`), placing an order is two separate
facts with no atomicity between them:

1. A synchronous Postgres transaction inserts the order row and its opening audit-trail
   entry (`services/order/repositories/orders.py::create`, [D24](key-decisions.md#d24--the-tracking-trail-moved-into-the-order-database-and-stopped-being-best-effort)).
2. *Then*, a fire-and-forget call starts the `OrderWorkflow` saga
   (`deps.orchestrator_service.start_saga`, [D25](key-decisions.md#d25--temporal-orchestrates-the-order-lifecycle-and-the-workflow-id-is-the-order-id)).

D25 documents this gap explicitly as an accepted cost: *"Temporal cannot enlist in a
Postgres transaction, so 'the order exists' and 'its saga started' are not one atomic
fact... A failed start leaves an order sitting at `created`, logged at error, repaired by a
retry with the same idempotency key."* It works, but it means a saga can fail to start and
nothing notices until the client happens to retry.

**Proposal:** move the order-creation write itself inside the Temporal workflow, using a
feature of the Temporal SDK not yet used anywhere in this codebase — **Update-with-Start**
— so that from the caller's perspective, "the order exists" and "the saga is running"
become one atomic fact, in one HTTP round trip.

## 2. The mechanism: Update-with-Start

Confirmed available and working against what's actually running here (`temporalio==1.31.0`
client, Temporal server 1.31.2 — the `temporalio/temporal:1.8.2` tag in `docker-compose.yml`
is only the CLI wrapper version, not the server version).

`client.execute_update_with_start_workflow(update, args, start_workflow_operation=...)`
atomically starts a workflow if it isn't already running, delivers a Temporal **Update** to
it (a synchronous, typed RPC into a running workflow — distinct from a signal, which is
fire-and-forget and returns nothing), and blocks until the Update handler returns a result.
If the workflow is already running (e.g. this is a retried request), the SDK routes the
Update to the existing execution instead of starting a duplicate — governed by the same
`WorkflowIDConflictPolicy.USE_EXISTING` already used for saga starts today.

This is the one new idea in this design: a synchronous HTTP handler can trigger a
long-running saga's *first* durable step, wait only for that step, and get its result —
without either blocking for the whole saga or losing the atomicity between the DB write and
the saga starting.

## 3. Order ID: known before Temporal is called

Using `order_id` to address the workflow (`order-<order_id>`, unchanged from D25) requires
knowing it *before* calling Temporal. Today it's Postgres-generated
(`db/order/init.sql:43`, `id UUID PRIMARY KEY DEFAULT uuid_generate_v4()`) — assigned at
INSERT time, which now happens *inside* the workflow, too late.

**Decision: derive it deterministically, app-side, before touching Temporal or Postgres:**

```python
order_id = uuid.uuid5(ORDER_ID_NAMESPACE, f"{current_user.user_id}:{x_idempotency_key}")
```

Keyed on `(customer_id, idempotency_key)`, not the idempotency key alone. This matters:
if two *different* customers happened to submit the same literal key string, keying on the
key alone would route both to the same workflow — whichever's create-order Update lands
first would have its order handed back to the second customer, with no ownership check left
in the new path to catch it (today's synchronous path runs `require_self_or_admin` before
ever returning a replayed order; that check has nowhere to live once creation moves inside
an Update triggered before any DB read). Keying on the pair means two different customers
never address the same workflow at all — the DB's existing global-unique constraint on
`idempotency_key` still independently catches literal-key reuse across customers as a
genuine `409`, exactly as it does today.

The orders table's `DEFAULT uuid_generate_v4()` stays in place as a harmless fallback; this
path always supplies `id` explicitly.

## 4. Concurrency: two Updates racing on the same key

Because creation now goes through a workflow Update instead of one synchronous call, two
near-simultaneous requests with the same `(customer_id, idempotency_key)` can both reach the
internal create-order endpoint before either's Postgres insert commits — both compute the
same `order_id`, both start executing the Update handler concurrently.

**Decision: converge same-customer races silently; keep the cross-customer 409.** On the
insert's `UniqueViolation`, re-select the row by `idempotency_key`: if its `customer_id`
matches the caller, this was a same-customer race — return the existing row as a normal
(non-error) result, preserving the `201`-first/`200`-after semantics
([D08](key-decisions.md#d08--idempotency-keys-are-mandatory-and-a-replay-answers-200)) end
to end. If the `customer_id` differs, raise the same `409 conflict()` this endpoint already
raises today for genuine cross-customer key collisions.

## 5. Request flow

```
Client                Order Service           Temporal / OrderWorkflow      (internal, via activity)
  |  POST /orders          |                          |                              |
  |  X-Idempotency-Key --->|                          |                              |
  |                        | order_id = uuid5(...)    |                              |
  |                        | execute_update_with_start_workflow(                     |
  |                        |   "create_order", [payload],                            |
  |                        |   start_workflow_operation=                             |
  |                        |     WithStartWorkflowOperation(                         |
  |                        |       "OrderWorkflow", id=f"order-{order_id}",          |
  |                        |       id_conflict_policy=USE_EXISTING))                 |
  |                        |------------------------>| (starts workflow if new)      |
  |                        |                          | create_order(payload) update  |
  |                        |                          |----------------------------->| POST /orders/internal/create
  |                        |                          |                              | (re-price, verify, insert —
  |                        |                          |                              |  X-Internal-Key)
  |                        |                          |<-----------------------------| {order, created, capacity, ...}
  |                        |                          | self._order = result         |
  |                        |                          | run() unblocks, saga proceeds |
  |                        |<-------------------------| returns update result         |
  |<---- 201/200 order ----|                          |                              |
```

`run()` no longer takes the order's data as a start argument — it opens with
`await workflow.wait_condition(lambda: self._order is not None)` and reads from
`self._order` once the Update has populated it, then continues exactly as today
(authorize payment → confirm/join kitchen rail → wait for kitchen decision → dispatch rider
→ pickup → delivery). This sidesteps any question of whether the Update or `run()`'s own
first line executes first — `wait_condition` is correct regardless of delivery order.

## 6. Where the write actually happens (D36 preserved)

`orchestrator-worker` (which runs `OrderWorkflow` and its activities) has no direct database
access — by design, since [D36](key-decisions.md#d36--the-order-sagas-workflow-and-worker-split-into-their-own-deployable):
every activity crosses HTTP back into the owning service, authenticated with
`X-Internal-Key`, never a database credential. This design keeps that invariant: a new
`create_order_activity` (`services/orchestrator/activities/order.py`) calls a new
`POST /api/v1/orders/internal/create` (`require_internal`-gated, same convention as the
existing `/orders/{id}/internal` and `/orders/logs` routes), which is where the re-pricing
([D06](key-decisions.md#d06--the-server-re-prices-every-cart)), customer/restaurant
verification, and the actual Postgres insert (D24) all move to — lifted essentially
unchanged from today's `checkout.py`.

## 7. What this costs: a new hard dependency on Temporal

Today, Temporal being unreachable degrades gracefully: the order still commits, the saga
start is logged and swallowed, and a client retry self-heals it (D25's accepted cost).
**Under this design, Temporal being unreachable means no order can be created at all** —
the write happens inside the workflow, so without Temporal there's no path to it.

This is accepted deliberately: the client sees a `503` and retries later, the same shape
already used elsewhere in this platform when a hard dependency is down
(e.g. [D51](key-decisions.md#d51--nginx-gains-a-fail-closed-auth_request-chokepoint-in-front-of-in-process-verification)'s
gateway-to-user-service dependency). It trades a documented atomicity gap for a documented
availability coupling — not a hidden regression, a named trade.

## 8. File-level changes

| File | Change |
|---|---|
| `services/order/apis/checkout.py` | `create_order` becomes a thin wrapper: derive `order_id`, build the Update payload, call Temporal, map `created` to `201`/`200`. Re-pricing/verification/insert removed from here. |
| `services/order/apis/internal_orders.py` (new) | `POST /api/v1/orders/internal/create`, `require_internal`-gated — the relocated creation logic. |
| `services/order/repositories/orders.py`, `repositories/sql.py` | `create()` takes an explicit `order_id`; `UniqueViolation` handling converges same-customer races (§4). |
| `services/order/clients/orchestrator.py`, `services/common/temporal.py` | New method wrapping `execute_update_with_start_workflow`; `start_saga` (creation use) removed. |
| `services/orchestrator/workflows/order.py` | `run(self, payload)` → `run(self)`; new `@workflow.update create_order` handler; `run()` waits on `self._order`. |
| `services/orchestrator/activities/order.py`, `clients/order/order_service.py` | New `create_order_activity` / `OrderServiceClient.create()`, HTTP + internal key, same shape as `transition_order_activity`. |
| `services/orchestrator/registry.py` | Register the new activity. |
| `readme/key-decisions.md` | New decision entry; D25 marked superseded (not edited away, per this file's own convention). |

**Breaking change to note:** `OrderWorkflow.run()`'s signature change means `order-service`
and `orchestrator-worker` must deploy together — the old caller (payload-passing) and the
new `run()` (no args) are incompatible mid-rollout.

## 9. Verification plan

- Full `scripts/smoke-test.sh` should pass with no assertion changes — the external contract
  (status codes, response shape, 200-on-replay) is unchanged by design.
- New cases worth adding: Temporal stopped → `POST /orders` returns `503`; two concurrent
  same-customer requests with the same idempotency key → both resolve to the same order;
  two different customers reusing the same literal key string → second gets `409`.
- Full saga re-verified end-to-end (checkout → payment → kitchen → rider → delivery),
  specifically diffing what `self._order` now supplies against what `payload` supplied
  before, since every downstream activity call reads from it.
