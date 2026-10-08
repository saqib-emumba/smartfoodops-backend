# SmartFoodOps — AI Retrieval Sequence Diagram

From an owner publishing a tagged menu, through its ingestion into the vector store, to a customer
searching it and asking for RAG context. Verified against
`services/menu/apis/menus.py`, `services/common/kafka.py`, `services/common/events/menu.py`,
`services/ai/ingestion.py`, `services/ai/chunking.py`, `services/ai/embeddings.py`,
`services/ai/repositories/{vectors,documents}.py`, `services/ai/apis/{search,rag}.py`,
`services/ai/rag_context.py`, `services/rider/apis/dispatch.py` and
`services/analytics/apis/internal.py`. The reasoning is in
[D59–D62](../key-decisions.md) and the phased build in
[week4-ai-retrieval-plan.md](../week4-ai-retrieval-plan.md).

The gateway's `auth_request` verify round trip (D51/D57) is drawn once, at the first gateway-fronted
call of each section; every `Gateway->>...: forward` runs the same check. Calls marked `[X-Internal-Key]`
use the shared service-to-service key (D15): the ingestion worker and the RAG assembler hold no user
token, and none of those routes has a `route_permissions` row, so the gateway refuses them even for an
admin's bearer.

```mermaid
sequenceDiagram
    autonumber
    actor Owner
    actor Customer
    participant Gateway
    participant UserSvc as User Service
    participant MenuSvc as Menu Service
    participant RestSvc as Restaurant Service
    participant Kafka
    participant DLQ as menu events DLQ
    participant Worker as AI Ingestion Worker
    participant VectorDB as sfo_vector_core (pgvector)
    participant AISvc as AI Service
    participant Analytics as Analytics Service
    participant RiderSvc as Rider Service

    rect rgb(235, 245, 255)
        Note over Owner, VectorDB: Publish a menu, then ingest it
        Owner->>Gateway: POST /menus [dietary_tags per item]
        Gateway->>UserSvc: auth_request verify + authorize (menu:write)
        UserSvc-->>Gateway: 200 + X-User-Id/Roles/Permissions
        Gateway->>MenuSvc: forward
        MenuSvc->>MenuSvc: normalize tags (lower-case, dedupe) against the fixed vocabulary
        break an unknown tag
            MenuSvc-->>Owner: 422 Unprocessable Entity
        end
        MenuSvc->>RestSvc: verify restaurant is active and owned by the caller
        MenuSvc->>MenuSvc: replace the menu tree in one transaction (menus.updated_at advances)
        MenuSvc->>MenuSvc: invalidate the Redis cache entry
        MenuSvc-)Kafka: produce menu.published [key = restaurant_id, event_key = updated_at]
        Note right of MenuSvc: best-effort: a Kafka failure is logged and never fails the publish.<br/>The event carries only restaurant_id, published_at and items_count --<br/>a hint to re-read, not the menu itself
        MenuSvc-->>Owner: 200 Menu published
        Kafka->>Worker: consume menu.published
        Worker->>Worker: validate against the registered schema
        break schema-invalid payload
            Worker-)DLQ: dead-letter with x-dlq-reason
            Note right of Worker: an unknown event type is skipped, never dead-lettered (D40)
        end
        Worker->>VectorDB: already processed this event_id?
        Note right of Worker: yes -> "duplicate", nothing to do
        Worker->>MenuSvc: GET /menus/{id}/internal [X-Internal-Key]
        break the menu no longer exists (404)
            MenuSvc-->>Worker: 404
            Worker->>VectorDB: delete the restaurant's documents, mark event processed
        end
        MenuSvc-->>Worker: menu tree + updated_at (read from Postgres, never the cache)
        Worker->>VectorDB: current source_version and embedding_model for this restaurant
        break the same version and model are already indexed
            Worker->>VectorDB: mark event processed
            Note right of Worker: "skipped" -- a backfill over current menus re-embeds nothing
        end
        Worker->>RestSvc: GET /restaurants/{id}/internal [X-Internal-Key]
        RestSvc-->>Worker: name, address, coordinates, is_active
        Worker->>Worker: build chunks -- "Dish: ... | Category: ... | Options: ... | Tags: ... | Availability: ..."
        Worker->>Worker: embed every dish and the restaurant in one batch (local all-MiniLM-L6-v2, 384-dim)
        Worker->>VectorDB: ONE transaction: delete the restaurant's old documents,<br/>insert the new ones, upsert the restaurant document, mark the event processed
        break a transient failure (a sibling down, the embedder erroring)
            Worker->>Worker: retry with backoff, 5 attempts
            Worker-)DLQ: dead-letter after the last attempt
            Note right of Worker: retried inside the handler on purpose: the loop commits offsets, so a<br/>propagating exception would commit past this message and lose it.<br/>The startup backfill recovers a dead-lettered menu anyway
        end
        Worker->>Worker: commit the Kafka offset (after the database write)
        Note over Worker, VectorDB: At startup the worker also walks GET /menus/internal/restaurant-ids and ingests<br/>every menu not already current -- a lost event is a delay, not a hole
    end

    rect rgb(240, 240, 240)
        Note over Customer, VectorDB: Hybrid semantic search
        Customer->>Gateway: POST /ai/search {query, top_k, max_price, dietary_filters}
        Gateway->>UserSvc: auth_request verify + authorize (ai:search)
        break the caller's roles lack ai:search (a rider, a restaurant_admin)
            UserSvc-->>Gateway: 403
            Gateway-->>Customer: 403 Forbidden -- never reaches the AI Service
        end
        Gateway->>AISvc: forward
        AISvc->>AISvc: validate: trim the query, check every filter tag against the vocabulary
        break an unknown tag, a blank query, or top_k outside 1-20
            AISvc-->>Customer: 422 Unprocessable Entity
        end
        AISvc->>AISvc: embed the query with the same model the documents used
        AISvc->>VectorDB: SET LOCAL hnsw.iterative_scan, hnsw.ef_search
        AISvc->>VectorDB: dishes: available AND price <= max AND tags @> filters AND restaurant active<br/>ORDER BY embedding <=> query LIMIT top_k
        AISvc->>VectorDB: restaurants: active AND has a dish passing the same constraints<br/>ORDER BY embedding <=> query LIMIT top_k
        VectorDB-->>AISvc: rows (served by the HNSW index)
        AISvc-->>Customer: 200 {matches[], restaurants[]} -- empty lists, never a 404, if nothing matches
    end

    rect rgb(255, 245, 230)
        Note over Customer, RiderSvc: RAG context for the Week 5 assistant
        Customer->>Gateway: POST /ai/rag-context {customer_id, prompt_query, latitude?, longitude?}
        Gateway->>UserSvc: auth_request verify + authorize (ai:rag_context)
        Gateway->>AISvc: forward
        AISvc->>AISvc: customer_id must be the caller's own (an admin may ask for anyone's)
        break someone else's customer_id
            AISvc-->>Customer: 403 Forbidden
        end
        par vector source (budget 5s)
            AISvc->>AISvc: embed the prompt
            AISvc->>VectorDB: the same hybrid search, plus restaurant coordinates
            VectorDB-->>AISvc: dishes and restaurants
        and analytics source (budget 3s)
            AISvc->>Analytics: GET /analytics/internal/customers/{id}/summary [X-Internal-Key]
            Analytics-->>AISvc: order counts + favourite restaurants (zeros for a customer with no history)
            AISvc->>VectorDB: resolve favourite restaurant ids to names
        end
        Note right of AISvc: courier availability needs the candidates' coordinates,<br/>so it runs after the vector source -- one lookup per restaurant, in parallel, capped at 5
        loop each candidate restaurant
            AISvc->>RiderSvc: POST /riders/internal/nearby {restaurant lat/long} [X-Internal-Key]
            RiderSvc-->>AISvc: available_riders, nearest_available_km, radius_km (counts only -- never a rider id or position)
        end
        AISvc->>AISvc: with a location: restaurants nearest-first among the semantic top-K
        AISvc-->>Customer: 200 {items, restaurants, customer_profile, courier_availability, sources_failed}
        Note right of AISvc: a source that errored or missed its budget is named in sources_failed and contributes<br/>nothing, and the call still returns 200. If the vector source fails, courier is reported failed too:<br/>an empty list would read to a model as "no riders", a claim this response cannot make
    end
```
