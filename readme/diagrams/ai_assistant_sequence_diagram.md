# SmartFoodOps — AI Assistant Sequence Diagram

How a question becomes a streamed, grounded answer; how an order is explained; how reviews and the
partner tools work; and how an order is later attributed to a recommendation. Verified against
`services/ai/apis/{assistant,explain,partner}.py`, `services/ai/agents/{graph,router,context,prompts}.py`,
`services/ai/{guardrails,order_explainer,partner,memory,llm,attribution}.py`,
`services/ai/repositories/interactions.py`, `services/order/apis/{reviews,internal_explain}.py`,
`services/order/clients/orchestrator.py`, `services/rider/apis/dispatch.py` and
`api-gateway/nginx.conf`. The reasoning is in [D63–D65](../key-decisions.md) and the phased build in
[week5-ai-assistant-plan.md](../week5-ai-assistant-plan.md). Retrieval itself (vector, analytics,
courier) is drawn in [ai_search_sequence_diagram.md](ai_search_sequence_diagram.md).

As in the Week 4 diagram, the gateway's `auth_request` round trip (D51/D57) is drawn once per section.
Calls marked `[X-Internal-Key]` use the shared service-to-service key (D15); none of those routes has a
`route_permissions` row, so the gateway refuses them even for an admin's bearer.

```mermaid
sequenceDiagram
    autonumber
    actor Customer
    actor Owner
    participant Gateway
    participant UserSvc as User Service
    participant AISvc as AI Service
    participant Redis as Redis db 3
    participant LLM as Chat model
    participant VectorDB as sfo_vector_core
    participant OrderSvc as Order Service
    participant RestSvc as Restaurant Service
    participant RiderSvc as Rider Service
    participant Temporal
    participant Kafka
    participant Worker as AI Ingestion Worker

    rect rgb(235, 245, 255)
        Note over Customer, VectorDB: A food question, streamed
        Customer->>Gateway: GET /ai/assistant/stream?q=... (Accept: text/event-stream)
        Gateway->>UserSvc: auth_request verify + authorize (ai:assistant)
        UserSvc-->>Gateway: 200 + X-User-Id/Roles/Permissions
        Note over Gateway: dedicated location: proxy_buffering off, HTTP/1.1, 120s read timeout
        Gateway->>AISvc: forward
        AISvc->>Redis: LRANGE ai:conv:{user}:{conversation} (empty on a miss or outage)
        AISvc->>LLM: router prompt -> JSON {intent, max_price, dietary_filters, order_id}
        LLM-->>AISvc: parsed defensively (unknown tags dropped, bad JSON -> food_discovery)
        alt out_of_scope
            AISvc-->>Customer: fixed reply, streamed word by word (no model call)
        else food_discovery / restaurant_support
            AISvc->>VectorDB: top-K above ASSISTANT_MIN_SCORE (+ analytics and courier, as in D62)
            alt nothing cleared the floor
                AISvc-->>Customer: "I couldn't find anything..." (no model call)
            else matches found
                AISvc->>LLM: generate from CONTEXT only (streaming)
                loop tokens as they are written
                    LLM-->>AISvc: fragment
                    AISvc-->>Customer: data: {"token": "...", "done": false}
                end
                Note over AISvc: guardrail: every **bold** name and every $ price must exist in CONTEXT
                alt grounded
                    AISvc-->>Customer: data: {"token": "", "done": true, "grounded": true, ...}
                else a name or price was invented
                    AISvc-->>Customer: data: {"done": true, "grounded": false, "replacement": "<rule-written answer>"}
                end
            end
        end
        AISvc->>Redis: RPUSH + LTRIM + EXPIRE (question, answer)
        AISvc->>VectorDB: INSERT assistant_interactions (ids and outcome only)
    end

    rect rgb(255, 247, 235)
        Note over Customer, Temporal: An order question — "why is my order late?" (also POST /ai/explain-order)
        Customer->>Gateway: GET /ai/assistant/stream?q=why is my order late  |  POST /ai/explain-order
        Gateway->>AISvc: forward (ai:assistant | ai:explain_order)
        opt no order id given
            AISvc->>OrderSvc: GET /orders/internal/customers/{id}/latest-order [X-Internal-Key]
        end
        AISvc->>OrderSvc: GET /orders/internal/{id}/explain-context [X-Internal-Key]
        OrderSvc->>Temporal: Query "stage" on the fulfilment and rider workflows (best-effort)
        Temporal-->>OrderSvc: stage, or nothing if finished / unreachable
        OrderSvc-->>AISvc: order, kitchen_decided_at, trail (status, service, reason code, time, dispatch estimate on `assigned`), saga stage, rider_assigned
        Note over AISvc: require_self_or_admin on the order's customer (403 otherwise)
        AISvc->>RestSvc: GET /restaurants/{id}/internal (name, coordinates) [X-Internal-Key]
        opt a rider holds a not-yet-delivered order
            AISvc->>RiderSvc: POST /riders/internal/order-courier [X-Internal-Key]
            RiderSvc-->>AISvc: distance in km (never an id, position or ETA)
        end
        Note over AISvc: facts split the wait: restaurant time = kitchen_decided_at - confirmed, courier search = assigned - kitchen_decided_at
        AISvc->>LLM: phrase FACTS only
        Note over AISvc: guardrail: every "N minutes" / "N km" must be a figure in FACTS, else the rule-written summary is returned
        Note over AISvc: money is stated from the facts: a compensation reason -> "refunded" (refund precedes cancel), payment_failed -> "not charged", and verify_money rejects any other refund or charge claim
        AISvc-->>Customer: explanation (grounded: true | false)
    end

    rect rgb(240, 253, 244)
        Note over Customer, Owner: Reviews and the partner tools
        Customer->>Gateway: POST /orders/{id}/review {rating, comment} (order:review)
        Gateway->>OrderSvc: forward
        Note over OrderSvc: own order only, delivered only, once (UNIQUE order_id -> 409)
        OrderSvc-->>Customer: 201
        Owner->>Gateway: POST /ai/partner/review-summary {restaurant_id} (ai:partner)
        Gateway->>AISvc: forward
        AISvc->>RestSvc: GET /restaurants/{id} as the owner (verify_owner, 403 if not theirs)
        AISvc->>OrderSvc: GET /orders/internal/restaurants/{id}/reviews [X-Internal-Key]
        OrderSvc-->>AISvc: count, average, recent commented reviews (no customer id)
        AISvc->>LLM: summarise REVIEWS only (skipped if none are written)
        AISvc-->>Owner: review_count + average_rating from the database, summary from the model
        Note over AISvc: describe-dish and promo are the same shape: verify_owner, one model call, any number not in the owner's input -> plain rendering, grounded: false
    end

    rect rgb(253, 242, 248)
        Note over Customer, Worker: Later: attributing an order to a recommendation
        Customer->>Gateway: POST /orders (the usual checkout, unchanged)
        Gateway->>OrderSvc: forward
        OrderSvc->>Kafka: order.created (customer_id, restaurant_id, total_amount, ...)
        Kafka-->>Worker: consumer group ai-attribution
        Worker->>VectorDB: UPDATE the newest unconverted interaction naming this restaurant within 24h (idempotent)
        Note over Worker: sfo_ai_attributed_orders_total and sfo_ai_attributed_revenue_total
    end
```
