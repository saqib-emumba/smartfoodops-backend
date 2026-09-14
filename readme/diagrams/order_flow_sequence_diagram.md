# SmartFoodOps — Order Flow Sequence Diagram

Creation through delivery, the failure/compensation path, and the Kafka eventing that runs
alongside — the one diagram for the order flow. Verified against
`services/order/apis/checkout.py`, `services/orchestrator/workflows/order.py`,
`services/orchestrator/activities/order.py`, `services/order/apis/kitchen.py`,
`services/order/apis/rider_reports.py`, `services/order/apis/transitions.py`,
`services/rider/apis/delivery.py`, and `services/common/outbox.py`. See
[readme/order-saga-orchestration-guide.md](../order-saga-orchestration-guide.md) for the
full reasoning behind each step.

```mermaid
sequenceDiagram
    autonumber
    actor Customer
    actor Owner
    actor Rider
    participant Gateway
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
        Note over Customer, Kafka: Order placed
        Customer->>Gateway: POST /orders [X-Idempotency-Key]
        Gateway->>OrderSvc: forward
        break key already used
            OrderSvc-->>Customer: 200 OK, same order replayed
            Note right of OrderSvc: saga restarted too, if it never began the first time
        end
        OrderSvc->>OrderSvc: re-price against live menu, verify customer & restaurant exist
        break item unavailable, price mismatch, or unknown restaurant
            OrderSvc-->>Customer: 422 Unprocessable Entity
            Note right of OrderSvc: nothing committed -- no order exists to cancel
        end
        OrderSvc->>OrderSvc: commit order + outbox row (one transaction)
        OrderSvc-)Temporal: start OrderWorkflow [SagaClient]
        OrderSvc-)Kafka: outbox relay publishes order.created
        Kafka->>Analytics: consume order.created -> create projection row
        Note over Kafka, NotifConsumer: Notification Consumer only reacts to confirmed / delivered / cancelled -- skips the rest
        OrderSvc-->>Customer: 201 Created
    end

    rect rgb(240, 240, 240)
        Note over Temporal, Owner: Payment authorization, then the kitchen's decision
        Temporal->>Worker: dispatch workflow task
        Worker->>PaymentSvc: authorize payment
        break payment declined
            PaymentSvc-->>Worker: declined
            Worker->>OrderSvc: transition -> cancelled
            OrderSvc-)Kafka: outbox relay publishes order.cancelled
            Kafka->>Analytics: consume order.cancelled -> mark projection cancelled
            Kafka->>NotifConsumer: consume order.cancelled -> notify customer (SMS)
            Note right of Worker: order cancelled, not refunded -- nothing was charged yet
        end
        PaymentSvc-->>Worker: authorized
        PaymentSvc-)Kafka: outbox relay publishes payment.authorized
        Kafka->>Analytics: consume payment.authorized -> dedup only, no projection field for it
        Worker->>OrderSvc: transition -> confirmed
        OrderSvc-)Kafka: outbox relay publishes order.confirmed
        Kafka->>Analytics: consume order.confirmed -> mark projection confirmed
        Kafka->>NotifConsumer: consume order.confirmed -> notify customer (SMS + email)
        Worker->>Worker: wait up to 120s for restaurant_decision
        Owner->>OrderSvc: POST /orders/{id}/accept
        OrderSvc->>OrderSvc: record kitchen decision
        OrderSvc-)Kafka: outbox relay publishes order.kitchen.decided
        Kafka->>Analytics: consume order.kitchen.decided -> dedup only, no projection field for it
        OrderSvc-)Temporal: signal restaurant_decision [accepted]
        Temporal->>Worker: deliver signal
    end

    rect rgb(255, 245, 230)
        Note over Worker, Rider: Rider dispatch, pickup, delivery
        Worker->>RiderSvc: dispatch (retries up to 6x, 10s apart)
        RiderSvc-->>Worker: rider assigned
        Worker->>OrderSvc: transition -> assigned
        OrderSvc-)Kafka: outbox relay publishes order.assigned
        Kafka->>Analytics: consume order.assigned -> mark projection assigned
        Rider->>RiderSvc: POST .../picked-up
        RiderSvc->>OrderSvc: record rider_reported_stage
        Note right of OrderSvc: no outbox row here, D46 -- a durable column, not an event
        RiderSvc-)Temporal: signal rider_pickup
        Temporal->>Worker: deliver signal
        Worker->>OrderSvc: transition -> picked_up
        OrderSvc-)Kafka: outbox relay publishes order.picked_up
        Kafka->>Analytics: consume order.picked_up -> mark projection picked_up
        Rider->>RiderSvc: POST .../delivered
        RiderSvc->>OrderSvc: record rider_reported_stage
        Note right of OrderSvc: no outbox row here either, same reason
        RiderSvc-)Temporal: signal rider_delivery
        Temporal->>Worker: deliver signal
        Worker->>OrderSvc: transition -> delivered
        OrderSvc-)Kafka: outbox relay publishes order.delivered
        Kafka->>Analytics: consume order.delivered -> mark delivered, compute delivery_seconds
        Kafka->>NotifConsumer: consume order.delivered -> notify customer (SMS)
        Worker->>RiderSvc: release rider
    end

    rect rgb(255, 230, 230)
        Note over Worker, OrderSvc: If it fails instead: kitchen rejects/stays silent past 120s,<br/>or no rider found after 6 attempts -- compensate
        Worker->>PaymentSvc: refund
        PaymentSvc-->>Worker: refunded
        PaymentSvc-)Kafka: outbox relay publishes payment.refunded
        Kafka->>Analytics: consume payment.refunded -> dedup only, no projection field for it
        Worker->>RiderSvc: release (only if one had been assigned)
        Worker->>OrderSvc: transition -> cancelled
        OrderSvc-)Kafka: outbox relay publishes order.cancelled
        Kafka->>Analytics: consume order.cancelled -> mark projection cancelled
        Kafka->>NotifConsumer: consume order.cancelled -> notify customer (SMS)
        Note right of Worker: order cancelled and refunded, in that order --<br/>the refund's the customer's money, so it goes first
    end
```
