# SmartFoodOps — Order Creation Sequence Diagram

`POST /api/v1/orders`, success path and edge cases, verified against
[services/order/apis/checkout.py](../../services/order/apis/checkout.py), its HTTP clients in
`services/order/clients/`, and `services/order/repositories/orders.py` as of Week 3 — see
[readme/key-decisions.md](../key-decisions.md) for the D-numbers cited inline below.

This diagram stops at the hand-off to the saga on purpose — everything from payment
authorization onward is a separate asynchronous flow; see
[readme/order-saga-orchestration-guide.md](../order-saga-orchestration-guide.md) for that half.

```mermaid
sequenceDiagram
    autonumber
    actor Customer
    participant Gateway as Nginx API Gateway
    participant OrderSvc as Order Service (FastAPI)
    participant UserSvc as User Service (FastAPI)
    participant RestaurantSvc as Restaurant Service (FastAPI)
    participant MenuSvc as Menu Service (FastAPI)
    participant OrderDB as PostgreSQL (sfo_order_core)
    participant Orchestrator as Orchestrator Service (Temporal saga)

    %% 1. SUCCESS PATH
    Note over Customer, Orchestrator: Flow: POST /api/v1/orders — success path
    Customer->>Gateway: POST /api/v1/orders (payload + X-Idempotency-Key + Bearer token)
    Gateway->>OrderSvc: Forward request

    rect rgb(235, 245, 255)
        Note over OrderSvc, OrderDB: 1. Idempotency check (replay protection)
        OrderSvc->>OrderDB: SELECT * FROM orders WHERE idempotency_key = :key
        OrderDB-->>OrderSvc: NULL (no existing order for this key)
    end

    rect rgb(240, 240, 240)
        Note over OrderSvc, MenuSvc: 2. Server-side re-pricing against the live menu
        OrderSvc->>MenuSvc: GET /api/v1/menus/{restaurant_id} (Bearer forwarded)
        MenuSvc-->>OrderSvc: Published menu (categories -> items -> customizations)
        OrderSvc->>OrderSvc: Recompute unit prices & total from the live menu
        Note over OrderSvc: build_order_snapshot rejects unavailable items,<br/>unknown options, or a total mismatch
    end

    rect rgb(255, 250, 235)
        Note over OrderSvc, RestaurantSvc: 3. Existence checks HTTP does in place of a foreign key
        OrderSvc->>UserSvc: GET /api/v1/users/{customer_id} (Bearer forwarded)
        UserSvc-->>OrderSvc: Customer record (confirms the account still exists)
        OrderSvc->>RestaurantSvc: GET /api/v1/restaurants/{restaurant_id} (Bearer forwarded)
        RestaurantSvc-->>OrderSvc: Restaurant record (capacity, latitude, longitude — captured now for the saga)
    end

    rect rgb(255, 245, 230)
        Note over OrderSvc, OrderDB: 4. One local transaction — order + audit trail + outbox row
        OrderSvc->>OrderDB: Write orders, order_tracking_logs & order_outbox in one transaction
        OrderDB-->>OrderSvc: Committed order row (status = 'created')
        Note over OrderSvc, OrderDB: No cross-service log call, no MongoDB — order_tracking_logs<br/>lives in this same database and commits in the SAME transaction<br/>as the order itself (D24), and order_outbox rides along for Kafka (D39)
    end

    rect rgb(240, 248, 240)
        Note over OrderSvc, Orchestrator: 5. Hand the committed order to the saga (fire-and-forget)
        OrderSvc-)Orchestrator: POST /api/v1/orchestrator/sagas [X-Internal-Key]
        Note over OrderSvc, Orchestrator: Payload: order_id, restaurant_id, amount,<br/>capacity, restaurant latitude/longitude
        Note over OrderSvc, Orchestrator: Deliberately non-fatal (D09): the order is already committed,<br/>so a failure here is logged, never returned to the client.<br/>Payment authorization, kitchen decision, rider dispatch and delivery<br/>all happen asynchronously from here — see order-saga-orchestration-guide.md
    end

    OrderSvc-->>Gateway: 201 Created — Envelope[OrderResponse] (status: "created")
    Gateway-->>Customer: 201 Created

    %% 2. ERROR & EDGE CASES
    Note over Customer, Orchestrator: Flow: POST /api/v1/orders — error & edge cases
    Customer->>Gateway: POST /api/v1/orders
    Gateway->>OrderSvc: Forward request

    alt Edge Case A: Missing mandatory X-Idempotency-Key header
        Note over OrderSvc: FastAPI's own header validation rejects this before any<br/>handler code runs — no DB or menu call ever happens
        OrderSvc-->>Gateway: 422 Unprocessable Entity
        Gateway-->>Customer: 422 (Detail: "field required — X-Idempotency-Key")
    else Edge Case B: Duplicate order request (idempotent replay)
        OrderSvc->>OrderDB: SELECT * FROM orders WHERE idempotency_key = :key
        OrderDB-->>OrderSvc: Existing order row (order_id, total_amount, ...)
        OrderSvc->>OrderSvc: require_self_or_admin(caller, existing.customer_id)
        OrderSvc-)Orchestrator: POST /api/v1/orchestrator/sagas [idempotent retry]
        Note over OrderSvc, Orchestrator: Self-healing: a no-op if that saga is already running
        OrderSvc-->>Gateway: 200 OK — the SAME order, unchanged (message: "Replayed")
        Gateway-->>Customer: 200 OK
    else Edge Case C: Item sold out / unknown option / total mismatch
        OrderSvc->>MenuSvc: GET /api/v1/menus/{restaurant_id}
        MenuSvc-->>OrderSvc: Menu (item "soldout" has is_available = false)
        OrderSvc->>OrderSvc: Re-validate against the menu
        Note over OrderSvc: build_order_snapshot raises -- availability, option,<br/>or total-mismatch validation failed
        OrderSvc-->>Gateway: 422 Unprocessable Entity
        Gateway-->>Customer: 422 (Detail: e.g. "Item 'soldout' is not available")
    else Edge Case D: Order database pool exhausted
        OrderSvc->>OrderDB: SELECT / INSERT ...
        Note over OrderSvc, OrderDB: Connection pool has no capacity left
        OrderDB--xOrderSvc: Pool timeout
        OrderSvc-->>Gateway: 500 Internal Server Error
        Gateway-->>Customer: 500 (Detail: "database connection timeout")
    end
```
