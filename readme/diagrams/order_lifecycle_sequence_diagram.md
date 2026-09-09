# SmartFoodOps — Order Lifecycle Sequence Diagram (Creation to Completion)

Happy path only, `created` through `delivered`, verified against
`services/order/apis/checkout.py`, `services/orchestrator/workflows/order.py`,
`services/orchestrator/activities/order.py`, `services/order/apis/kitchen.py`,
`services/order/apis/rider_reports.py`, `services/order/apis/transitions.py`, and
`services/rider/apis/delivery.py`. See
[readme/order-saga-orchestration-guide.md](../order-saga-orchestration-guide.md) for the
full reasoning behind each step, and
[order_sequence_diagram.md](order_sequence_diagram.md) for order creation's own edge cases.

```mermaid
sequenceDiagram
    autonumber
    actor Customer
    actor Owner
    actor Rider
    participant Gateway as Nginx Gateway
    participant OrderSvc as Order Service
    participant PaymentSvc as Payment Service
    participant RiderSvc as Rider Service
    participant Temporal as Temporal Server
    participant Worker as Orchestrator Worker

    rect rgb(235, 245, 255)
        Note over Customer, Worker: Order placed
        Customer->>Gateway: POST /api/v1/orders
        Gateway->>OrderSvc: forward
        OrderSvc->>OrderSvc: price, verify customer & restaurant, commit order
        OrderSvc-)Temporal: start OrderWorkflow [SagaClient]
        Temporal->>Worker: dispatch workflow task
        OrderSvc-->>Gateway: 201 Created
        Gateway-->>Customer: 201 Created
    end

    rect rgb(240, 240, 240)
        Note over Worker, PaymentSvc: Payment authorization
        Worker->>PaymentSvc: POST /api/v1/payments/authorize
        PaymentSvc-->>Worker: authorized
        Worker->>OrderSvc: POST /api/v1/orders/{order_id}/transitions [confirmed]
        OrderSvc-->>Worker: status confirmed
    end

    rect rgb(255, 250, 235)
        Note over Worker, Owner: Kitchen decision
        Worker->>Worker: wait for restaurant_decision
        Owner->>Gateway: POST /api/v1/orders/{order_id}/accept
        Gateway->>OrderSvc: forward
        OrderSvc->>OrderSvc: record kitchen_decision accepted
        OrderSvc-)Temporal: signal restaurant_decision [SagaClient]
        Temporal->>Worker: deliver restaurant_decision
        OrderSvc-->>Gateway: 200 OK
        Gateway-->>Owner: 200 OK
    end

    rect rgb(255, 245, 230)
        Note over Worker, RiderSvc: Rider dispatch
        Worker->>RiderSvc: POST /api/v1/riders/dispatch
        RiderSvc-->>Worker: rider assigned
        Worker->>OrderSvc: POST /api/v1/orders/{order_id}/transitions [assigned]
        OrderSvc-->>Worker: status assigned
    end

    rect rgb(240, 248, 240)
        Note over Worker, Rider: Pickup
        Worker->>Worker: wait for rider_pickup
        Rider->>Gateway: POST /api/v1/riders/me/orders/{order_id}/picked-up
        Gateway->>RiderSvc: forward
        RiderSvc->>OrderSvc: POST /api/v1/orders/{order_id}/rider-report [picked_up]
        OrderSvc->>OrderSvc: record rider_reported_stage picked_up
        OrderSvc-->>RiderSvc: 202 Accepted
        RiderSvc-)Temporal: signal rider_pickup [SagaClient]
        Temporal->>Worker: deliver rider_pickup
        RiderSvc-->>Gateway: 200 OK
        Gateway-->>Rider: 200 OK
        Worker->>OrderSvc: POST /api/v1/orders/{order_id}/transitions [picked_up]
        OrderSvc-->>Worker: status picked_up
    end

    rect rgb(250, 235, 245)
        Note over Worker, Rider: Delivery
        Worker->>Worker: wait for rider_delivery
        Rider->>Gateway: POST /api/v1/riders/me/orders/{order_id}/delivered
        Gateway->>RiderSvc: forward
        RiderSvc->>OrderSvc: POST /api/v1/orders/{order_id}/rider-report [delivered]
        OrderSvc->>OrderSvc: record rider_reported_stage delivered
        OrderSvc-->>RiderSvc: 202 Accepted
        RiderSvc-)Temporal: signal rider_delivery [SagaClient]
        Temporal->>Worker: deliver rider_delivery
        RiderSvc-->>Gateway: 200 OK
        Gateway-->>Rider: 200 OK
        Worker->>OrderSvc: POST /api/v1/orders/{order_id}/transitions [delivered]
        OrderSvc-->>Worker: status delivered
        Worker->>RiderSvc: POST /api/v1/riders/release
        RiderSvc-->>Worker: rider released
    end

    Worker->>Worker: workflow completes, status delivered
```
