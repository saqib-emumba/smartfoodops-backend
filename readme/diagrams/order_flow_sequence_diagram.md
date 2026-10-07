# SmartFoodOps — Order Flow Sequence Diagram

Creation through delivery, the failure/compensation path, and the Kafka eventing that runs
alongside — the one diagram for the order flow. Verified against
`services/order/apis/checkout.py`, `services/order/clients/orchestrator.py`,
`services/common/temporal.py`, `services/order/apis/internal_orders.py`,
`services/order/apis/internal_kitchen.py`, `services/order/apis/internal_events.py`,
`services/orchestrator/workflows/order/workflow.py`,
`services/orchestrator/workflows/fulfillment/workflow.py`,
`services/orchestrator/workflows/payment/workflow.py`,
`services/orchestrator/workflows/rider/workflow.py`,
`services/orchestrator/workflows/compensation/workflow.py`,
`services/orchestrator/workflows/order/activities.py`,
`services/orchestrator/workflows/payment/activities.py`,
`services/orchestrator/workflows/rider/activities.py`,
`services/orchestrator/utils/transitions.py`, `services/order/apis/kitchen.py`,
`services/order/apis/rider_reports.py`, `services/order/apis/transitions.py`,
`services/rider/apis/delivery.py`, `services/rider/fleet.py`, and
`services/common/kafka.py`'s `KafkaGateway`. See
[readme/order-saga-orchestration-guide.md](../order-saga-orchestration-guide.md) for the
full reasoning behind each step.

The gateway's `auth_request` verify round trip (D51) is drawn once below, at the only
gateway-fronted call in this flow — every later `Gateway->>...: forward` in this platform
runs the same check. `RiderSvc->>OrderSvc: record rider_reported_stage` stays on the
internal key (D26), unaffected by D51/D52: a rider reporting a pickup or delivery is a fact
about what it observed, not a call made *as* the customer.

> **"Order created" below reflects [D54](../key-decisions.md#d54--order-creation-moves-inside-temporal-via-update-with-start), implemented, as reshaped by [D58](../key-decisions.md#d58--checkout-starts-orderworkflow-and-waits-for-its-result-post-payment-work-moves-to-fulfillmentworkflow).** See
> [readme/order-creation-temporal-update-design.md](../order-creation-temporal-update-design.md)
> for the original design. `OrderSvc` no longer inserts the order directly and starts the saga
> as a separate, fire-and-forget step afterward (D25, superseded); the insert happens *inside*
> the workflow, at the cost of a new hard dependency on Temporal for checkout itself.

> **Every `-)Kafka:` publish below reflects [D53](../key-decisions.md#d53--the-outbox-tables-are-removed-workflow-history-becomes-the-publish-ledger), implemented.**
> See [readme/outbox-removal-temporal-design.md](../outbox-removal-temporal-design.md).
> `order_outbox`/`payment_outbox` and their relays (D39, superseded) are gone: the business
> write and the Kafka publish are two sequential Temporal activities, so Temporal's own
> workflow history — not a database row — is what guarantees the publish is retried until it
> succeeds. The kitchen decision (`POST /orders/{id}/accept|reject`) likewise moves from a
> plain DB write plus a best-effort signal into a Temporal Update, since D53 needs an activity
> to attach the publish to and none existed for that write before.

> **Payment, rider dispatch/delivery, and compensation run as child workflows, per
> [D55](../key-decisions.md#d55--payment-rider-and-compensation-become-child-workflows-of-orderworkflow), implemented.**
> They are marked with a `Note` at the start of the relevant section below, rather than drawn
> as separate lifelines, since every HTTP call they make still flows through the same
> `Orchestrator Worker` process either way. Two behavioural changes worth noticing while
> reading: `RiderSvc` signals pickup/delivery **directly into `RiderWorkflow`**
> (`rider-<order_id>`), not into the order's workflow as before; and
> `RiderWorkflow` releases whatever rider it claimed on *every one of its own* exit paths —
> delivered, or a timed-out pickup/delivery with nothing recovered — so `CompensationWorkflow`
> never touches the fleet at all, unlike the single-workflow version this replaces.

> **A refund that permanently fails reaches `compensation_failed`, per
> [D56](../key-decisions.md#d56--a-refund-that-permanently-fails-becomes-compensation_failed-not-a-silently-failed-workflow), implemented.**
> `CompensationWorkflow` catches a refund activity that exhausts every retry instead of
> letting the exception fail the workflow unhandled — drawn as a `break` in the
> compensation section below, mirroring every other `break` in this diagram. The order
> never reaches `cancelled` on this path; nothing was given back, so claiming otherwise
> would be worse than an honest "needs a human."

> **Checkout answers after payment, and the workflow tree is split, per [D58](../key-decisions.md#d58--checkout-starts-orderworkflow-and-waits-for-its-result-post-payment-work-moves-to-fulfillmentworkflow), implemented.**
> `OrderSvc` *starts* `OrderWorkflow` with the cart -- items only, **no price**: the server prices
> it from the live menu and the workflow pays the total it computed -- and waits for its result — no Update, no
> polling. `OrderWorkflow` (`order-<id>`) is only checkout: create the order, run
> `PaymentWorkflow` as a child, mark the order `confirmed` (payment recorded, capacity slot
> claimed), start `FulfillmentWorkflow` (`fulfillment-<id>`) **abandoned**, and complete — that
> completion is the `201`. `FulfillmentWorkflow` then owns the kitchen's decision (the
> `kitchen_decision` Update now goes to *it*), starts `RiderWorkflow` and
> `CompensationWorkflow`, and keeps running after `order-<id>` has closed. A rejected cart is
> `422`/`404`/`409`, a declined payment `402`, and a full kitchen `409` — either turned away at
> creation (nothing written) or, if it filled up while the order was being paid for, refunded
> first.

```mermaid
sequenceDiagram
    autonumber
    actor Customer
    actor Owner
    actor Rider
    participant Gateway
    participant UserSvc as User Service
    participant OrderSvc as Order Service
    participant PaymentSvc as Payment Service
    participant RiderSvc as Rider Service
    participant Temporal as Temporal Server
    participant Worker as Orchestrator Worker
    participant Kafka
    participant DLQ as Unreadable/invalid events
    participant Analytics as Analytics Consumer
    participant NotifConsumer as Notification Consumer

    rect rgb(235, 245, 255)
        Note over Customer, Kafka: Order created
        Customer->>Gateway: POST /orders [X-Idempotency-Key] -- cart only, no price (D58)
        Gateway->>UserSvc: auth_request verify + authorize (D51/D57)
        UserSvc-->>Gateway: 200 + X-User-Id/X-User-Roles/X-User-Permissions
        Note right of Gateway: every Gateway forward elsewhere in this platform runs<br/>this same check first -- omitted after this diagram for readability
        Gateway->>OrderSvc: forward [X-User-Id, X-User-Roles, X-User-Permissions]
        OrderSvc->>OrderSvc: order_id = uuid5(customer_id, idempotency_key)
        OrderSvc->>Temporal: start_workflow(OrderWorkflow, cart) as order-{id}<br/>[conflict policy FAIL -- tells a first request from a retry]
        break Temporal unreachable
            Temporal-->>OrderSvc: connection error
            OrderSvc-->>Customer: 503 Service Unavailable
            Note right of OrderSvc: no order created at all -- Temporal is a hard dependency<br/>for checkout (see design doc). Client retries later.
        end
        Note right of OrderSvc: nothing is sent into the workflow. OrderSvc now waits for<br/>its result (up to 85s -- under the gateway's 90s read timeout)
        Temporal->>Worker: dispatch OrderWorkflow task
        Worker->>OrderSvc: POST /orders/internal/create [X-Internal-Key]
        OrderSvc->>OrderSvc: price the cart from the live menu (server is the only source of the total),<br/>verify customer & restaurant exist
        break item unavailable, unknown option, or unknown restaurant
            OrderSvc-->>Worker: 422 Unprocessable Entity
            Worker-->>Temporal: OrderWorkflow fails (non-retryable, type OrderCreateRejected)
            Temporal-->>OrderSvc: WorkflowFailureError
            OrderSvc-->>Customer: 422 Unprocessable Entity
            Note right of Worker: nothing committed -- no order exists to cancel
        end
        break same idempotency key, different customer
            OrderSvc-->>Worker: 409 Conflict
            Worker-->>Temporal: OrderWorkflow fails (non-retryable, type OrderCreateConflict)
            Temporal-->>OrderSvc: WorkflowFailureError
            OrderSvc-->>Customer: 409 Conflict
        end
        OrderSvc->>OrderSvc: insert order, then early capacity check (D58, advisory)
        break kitchen already at capacity at creation
            OrderSvc->>OrderSvc: roll back the insert -- no order row, nothing charged
            OrderSvc-->>Worker: 409 Conflict
            Worker-->>Temporal: OrderWorkflow fails (non-retryable)
            Temporal-->>OrderSvc: WorkflowFailureError
            OrderSvc-->>Customer: 409 Conflict (kitchen at capacity)
            Note right of OrderSvc: advisory only -- it counts confirmed orders, so concurrent<br/>checkouts still paying are invisible to it. The check at "confirmed"<br/>below is the one that guarantees the limit.
        end
        OrderSvc->>OrderSvc: commit order (no outbox row -- D53)
        Note right of OrderSvc: same customer, same key, already committed --<br/>converges here instead of erroring (concurrent replay).<br/>A retry after the first run finished lands here too: created=false,<br/>so OrderWorkflow returns the order at once -- no second payment
        OrderSvc-->>Worker: order, created, capacity, restaurant lat/long
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.created
        OrderSvc-->>Worker: 200 published
        Note right of Worker: Temporal retries this activity until the publish<br/>is ack'd -- workflow history is the ledger now, not a table (D53)
        Kafka->>Analytics: consume order.created -> create projection row
        Note over Kafka, NotifConsumer: Notification Consumer only reacts to confirmed / delivered / cancelled -- skips the rest
    end

    rect rgb(240, 240, 240)
        Note over Temporal, Customer: Payment authorization, confirmation, then the checkout answer
        Note right of Worker: OrderWorkflow starts PaymentWorkflow as a child (mode=saga, D55)<br/>and awaits it -- everything down to "authorized" runs inside it
        Worker->>PaymentSvc: authorize payment
        break payment declined
            PaymentSvc-->>Worker: declined
            Note right of Worker: PaymentWorkflow fails non-retryably and OrderWorkflow<br/>cancels directly -- no CompensationWorkflow here, since nothing was ever charged
            Worker->>OrderSvc: transition -> cancelled
            Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
            OrderSvc-)Kafka: produce order.cancelled
            OrderSvc-->>Worker: 200 published
            Kafka->>Analytics: consume order.cancelled -> mark projection cancelled
            Kafka->>NotifConsumer: consume order.cancelled -> notify customer (SMS)
            Worker-->>Temporal: OrderWorkflow fails (non-retryable, type PaymentDeclined)
            Temporal-->>OrderSvc: WorkflowFailureError
            OrderSvc-->>Customer: 402 Payment Required
            Note right of Worker: order cancelled, not refunded -- nothing was charged yet
        end
        PaymentSvc-->>Worker: authorized
        Worker->>PaymentSvc: POST /payments/internal/events [X-Internal-Key]
        PaymentSvc-)Kafka: produce payment.authorized
        PaymentSvc-->>Worker: 200 published
        Kafka->>Analytics: consume payment.authorized -> dedup only, no projection field for it
        Note right of Worker: PaymentWorkflow completes -- OrderWorkflow resumes
        Worker->>OrderSvc: transition -> confirmed [capacity_limit]
        Note right of OrderSvc: entering confirmed records the payment on the order and claims a<br/>capacity slot in one local transaction (the authoritative capacity check)
        break kitchen filled up since the early check (race loser)
            OrderSvc-->>Worker: 409 at capacity
            Note right of Worker: customer is already charged -- OrderWorkflow starts<br/>CompensationWorkflow as a child (refund, then cancel -- last section below)
            Worker-->>Temporal: OrderWorkflow fails (non-retryable, type KitchenAtCapacity)
            Temporal-->>OrderSvc: WorkflowFailureError
            OrderSvc-->>Customer: 409 Conflict (kitchen at capacity, payment refunded)
        end
        OrderSvc-->>Worker: confirmed
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.confirmed
        OrderSvc-->>Worker: 200 published
        Kafka->>Analytics: consume order.confirmed -> mark projection confirmed
        Kafka->>NotifConsumer: consume order.confirmed -> notify customer (SMS + email)
        Worker->>Temporal: start FulfillmentWorkflow as fulfillment-{id}<br/>[parent close policy ABANDON]
        Worker-->>Temporal: OrderWorkflow completes with the order
        Temporal-->>OrderSvc: workflow result (order)
        OrderSvc-->>Customer: 201 Created (200 if this was a replay)
        Note right of Worker: the API's answer is OrderWorkflow's completion. order-{id} is<br/>checkout only; the rest of the order's life is fulfillment-{id} and below
    end

    rect rgb(240, 240, 240)
        Note over Temporal, Owner: The kitchen's decision (FulfillmentWorkflow, D58)
        Temporal->>Worker: dispatch FulfillmentWorkflow task
        Worker->>Worker: wait up to 300s for the kitchen_decision update<br/>(on FulfillmentWorkflow -- the order is already confirmed)
        Owner->>OrderSvc: POST /orders/{id}/accept
        OrderSvc->>Temporal: execute_update(fulfillment-{id}, kitchen_decision)
        break Temporal unreachable
            Temporal-->>OrderSvc: connection error
            OrderSvc-->>Owner: 503 Service Unavailable
            Note right of OrderSvc: the decision isn't recorded at all if Temporal<br/>is down -- hard dependency (D53). Owner retries later.
        end
        Temporal->>Worker: deliver kitchen_decision update
        Worker->>OrderSvc: POST /orders/{id}/internal/kitchen-decision [X-Internal-Key]
        OrderSvc->>OrderSvc: record kitchen decision
        OrderSvc-->>Worker: decision recorded
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.kitchen.decided
        OrderSvc-->>Worker: 200 published
        Kafka->>Analytics: consume order.kitchen.decided -> dedup only, no projection field for it
        Worker-->>Temporal: update result
        Temporal-->>OrderSvc: decision recorded
        OrderSvc-->>Owner: 200 OK
        Note right of Worker: the update handler both records the decision and unblocks<br/>run()'s wait -- no separate restaurant_decision signal needed anymore
    end

    rect rgb(255, 245, 230)
        Note over Worker, Rider: Rider dispatch, pickup, delivery
        Note right of Worker: FulfillmentWorkflow starts RiderWorkflow as a child (D55/D58) and awaits it --<br/>everything in this section runs inside it, including its own cleanup on failure
        Worker->>RiderSvc: dispatch (retries up to 6x, 10s apart)
        break no rider found after 6 attempts
            RiderSvc-->>Worker: not assigned
            Note right of Worker: RiderWorkflow returns failure -- nothing was ever claimed,<br/>so nothing to release. FulfillmentWorkflow starts CompensationWorkflow next.
        end
        RiderSvc-->>Worker: rider assigned
        Worker->>OrderSvc: transition -> assigned
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.assigned
        OrderSvc-->>Worker: 200 published
        Kafka->>Analytics: consume order.assigned -> mark projection assigned
        Rider->>RiderSvc: POST .../picked-up
        RiderSvc->>OrderSvc: record rider_reported_stage
        Note right of OrderSvc: no outbox row here, D46 -- a durable column, not an event
        RiderSvc-)Temporal: signal rider_pickup (-> RiderWorkflow directly -- D55)
        Temporal->>Worker: deliver signal
        break rider never reports picked-up, and recovery finds nothing either
            Worker->>RiderSvc: release rider
            Note right of Worker: RiderWorkflow releases the rider itself before returning<br/>failure -- CompensationWorkflow never needs to know one was assigned
        end
        Worker->>OrderSvc: transition -> picked_up
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.picked_up
        OrderSvc-->>Worker: 200 published
        Kafka->>Analytics: consume order.picked_up -> mark projection picked_up
        Rider->>RiderSvc: POST .../delivered
        RiderSvc->>OrderSvc: record rider_reported_stage
        Note right of OrderSvc: no outbox row here either, same reason
        RiderSvc-)Temporal: signal rider_delivery (-> RiderWorkflow directly -- D55)
        Temporal->>Worker: deliver signal
        break rider never reports delivered, and recovery finds nothing either
            Worker->>RiderSvc: release rider
            Note right of Worker: same self-cleanup as the pickup timeout above
        end
        Worker->>OrderSvc: transition -> delivered
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.delivered
        OrderSvc-->>Worker: 200 published
        Kafka->>Analytics: consume order.delivered -> mark projection delivered
        Kafka->>NotifConsumer: consume order.delivered -> notify customer (SMS)
        Worker->>RiderSvc: release rider
        Note right of Worker: RiderWorkflow completes -- FulfillmentWorkflow resumes and finishes (delivered)
    end

    rect rgb(255, 230, 230)
        Note over Worker, OrderSvc: If it fails instead: kitchen rejects/stays silent past 300s,<br/>or RiderWorkflow reports it could not complete -- compensate.<br/>(Also the kitchen-full race at "confirmed" above, started by OrderWorkflow.)
        Note right of Worker: FulfillmentWorkflow starts CompensationWorkflow as a child (D55/D58) and awaits it.<br/>No rider involvement at all here -- RiderWorkflow already released whatever<br/>it claimed (or never claimed one), so this is purely money and order state.
        Worker->>PaymentSvc: refund (retries per COMPENSATION_POLICY)
        break every retry exhausted -- the refund permanently fails (D56)
            PaymentSvc-->>Worker: still failing
            Note right of Worker: CompensationWorkflow catches this instead of failing<br/>unhandled -- a business outcome, not a technical fluke
            Worker->>OrderSvc: transition -> compensation_failed [reason, detail]
            Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
            OrderSvc-)Kafka: produce order.compensation_failed
            OrderSvc-->>Worker: 200 published
            Kafka->>Analytics: consume order.compensation_failed -> mark projection compensation_failed
            Note right of Worker: visible via order_tracking_logs and<br/>GET /api/v1/orders?status=compensation_failed (system_admin) --<br/>never reaches 'cancelled' on this path
        end
        PaymentSvc-->>Worker: refunded
        Worker->>PaymentSvc: POST /payments/internal/events [X-Internal-Key]
        PaymentSvc-)Kafka: produce payment.refunded
        PaymentSvc-->>Worker: 200 published
        Kafka->>Analytics: consume payment.refunded -> dedup only, no projection field for it
        Worker->>OrderSvc: transition -> cancelled
        Worker->>OrderSvc: POST /orders/{id}/internal/events [X-Internal-Key]
        OrderSvc-)Kafka: produce order.cancelled
        OrderSvc-->>Worker: 200 published
        Kafka->>Analytics: consume order.cancelled -> mark projection cancelled
        Kafka->>NotifConsumer: consume order.cancelled -> notify customer (SMS)
        Note right of Worker: order cancelled and refunded, in that order --<br/>the refund's the customer's money, so it goes first
    end
```
