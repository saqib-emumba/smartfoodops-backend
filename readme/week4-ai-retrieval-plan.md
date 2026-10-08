# Week 4 — AI retrieval layer: implementation plan

Implements §2 ("Week 4 Requirements: Data Preparation & Retrieval Layer") of
[smartfoodops-week4-5-requirements.md](docs/smartfoodops-week4-5-requirements.md): a vector
store, an ingestion pipeline, hybrid semantic search, and a multi-source RAG context assembler.

**Status:** Phases 1 and 2 are implemented and verified. Phases 3 to 6 are planned.

Decision numbers D59 to D62 are cited in code comments already; the entries themselves are
written in Phase 6 under a new `## Week 4 — semantic retrieval (AI layer)` section of
[key-decisions.md](key-decisions.md).

## 1. What the spec asks for, and what the platform lacked

The spec's four deliverables are a containerized vector DB, an ingestion worker, `POST
/api/v1/ai/search`, and `POST /api/v1/ai/rag-context`. It must respect D35 (response envelope),
D57 (gateway RBAC) and D01 (database per service).

Exploration found five gaps the spec assumes away:

| Gap | Consequence | Resolved in |
|---|---|---|
| Menu publish is a full replace whose only side effect is deleting a Redis key; the menu service had no Kafka | Nothing tells the AI side a menu changed | Phase 2 (`menu.published`) |
| No dietary or tag column anywhere | The spec's `dietary_tags` filter has no data | Phase 2 (owner-declared column) |
| No bulk or internal read routes on menu, restaurant, rider or analytics | The AI service cannot read them without touching their databases (D01) | Phase 2 |
| `rag-context` carries only `customer_id` and a prompt | "Near me" has no location | Phase 5 (optional `latitude`/`longitude`) |
| Order projections hold no line items | "Favourite dishes" is not derivable; "favourite restaurants" is | Out of scope (see §6) |

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Embedding model | Local `all-MiniLM-L6-v2`, 384-dim, behind an `Embedder` interface; OpenAI selectable by `EMBEDDING_PROVIDER` | No API key, no per-call cost, no menu data leaving the stack. The spec allows either model. |
| Embedding runtime | `fastembed` (onnxruntime), not `sentence-transformers` | Same model and output; image is ~758MB rather than 1.5GB+ with PyTorch |
| Change feed | Kafka `menu.published` event, re-read over internal HTTP | Matches D38 (Kafka carries facts); the event is a hint to re-read, not the data |
| Tags | One owner-declared `menu_items.dietary_tags` list from a fixed vocabulary covering diets, allergens, cuisines, formats, dish types and meal styles | Filters are exact; nothing is inferred, which matters for the Week 5 zero-hallucination goal. Mixing groups was a deliberate choice (the alternative was separate cuisine and meal-style fields); a filter requires every tag asked for, so clients send one cuisine per search. |
| Vector search scope | Items and restaurants first; order history comes from analytics in the RAG step | Projections have no line items |
| pgvector client | No Python package; vectors are passed as text and cast with `::vector` | Avoids registering a type adapter on every pooled connection |

Cross-cutting rules that apply to every phase:
- **D20:** every edit to compose, nginx, Prometheus, a topic list or an `init.sql` is mirrored
  into `scripts/init_bootstrap.sh`.
- **D57:** every gated route needs a `route_permissions` row; an unmapped path is a 403.
- **Internal routes** use `require_internal`, have no permission row, and are therefore
  unreachable through the gateway.

## 3. Phase overview

| Phase | Scope | Status |
|---|---|---|
| 1 | Infrastructure and service skeleton | Done |
| 2 | Source-data changes in menu, restaurant, rider, analytics | Done |
| 3 | Ingestion pipeline | Planned |
| 4 | `POST /api/v1/ai/search` | Planned |
| 5 | `POST /api/v1/ai/rag-context` | Planned |
| 6 | Telemetry, tests, documentation | Planned |

After each phase, stop and review before starting the next.

## 4. Phases

### Phase 1 — Infrastructure and service skeleton (done)

An `ai-service` that is up, healthy, routed and scraped, with an empty, indexed vector database.

- `db-vector-postgres`: the `pgvector/pgvector:pg15` image, container `sfo-vector-db`, DB
  `sfo_vector_core`, host port 5439, volume `vector_postgres_data`. (`postgres:15-alpine` has no
  pgvector.)
- [db/vector/init.sql](../db/vector/init.sql): `menu_item_documents` and `restaurant_documents`
  with `vector(384)` columns; HNSW `vector_cosine_ops` indexes; a GIN index on `dietary_tags`;
  btree filter indexes; a `processed_events` dedup table.
- `services/ai/` (port 8009): `main.py`, `deps.py`, `config.py`, `embeddings.py`,
  `apis/health.py`, Dockerfile. The model is baked into the image at build time, so the
  container needs no network to start.
- Gateway: public `GET /api/v1/ai/health`, plus a gated `/api/v1/ai` prefix (60s read timeout).
- Prometheus job, smoke-test health checks (readiness now waits for 8 services), bootstrap mirror.

Verified: health returns 200 through the gateway with model and dimension; the `vector`
extension and both HNSW indexes exist.

### Phase 2 — Source-data changes (done)

Everything the AI service needs is owner-declared, event-signalled and reachable over internal
HTTP.

- **Dietary tags.** `menu_items.dietary_tags TEXT[]`, the vocabulary in
  [common/dietary.py](../services/common/dietary.py), validated on publish (unknown tag is a
  422), returned on read. Migration: [db/menu/add_dietary_tags.sql](../db/menu/add_dietary_tags.sql).
- **`menu.published`.** New topic `sfo.menu.events.v1` (+ DLQ), model in
  [common/events/menu.py](../services/common/events/menu.py). Published after the commit and
  cache invalidation; best-effort, so a Kafka failure is logged and never fails the publish.
  `KafkaGateway.publish` gained an optional `event_key`, because the default event id
  (`aggregate_id`, `event_type`) assumes an event fires once per aggregate, which a republished
  menu does not.
- **Internal routes** (internal key only):
  - `GET /api/v1/menus/{id}/internal` and `GET /api/v1/menus/internal/restaurant-ids`
  - `GET /api/v1/restaurants/{id}/internal`
  - `POST /api/v1/riders/internal/nearby`: returns a count and the nearest distance only,
    never rider ids or positions, because the consumer is an LLM prompt
  - `GET /api/v1/analytics/internal/customers/{id}/summary`: order counts and favourite
    restaurants (analytics now holds the internal key and JWT public key because it imports
    `common.auth`)

Verified: tags round-trip and normalize; one event lands on the topic per publish; every
internal route is 401 without the key and 403 through the gateway with a bearer token;
`--fast` smoke passed 294/294.

### Phase 3 — Ingestion pipeline (planned): `services/ai/ingestion.py`

A menu publish becomes up-to-date vectors within seconds, idempotently.

- A separate container `ai-ingestion-worker` from the same image (`python -m ai.ingestion`) with
  its own metrics server on port 9111, so a slow embedding job never stalls the API.
- Consumes `sfo.menu.events.v1` as group `ai-ingestion`: manual commit, `read_committed`, DLQ,
  schema-registry validation. The pattern is copied from
  [analytics/consumer.py](../services/analytics/consumer.py). Dedup uses `processed_events`.
- Per event: fetch the menu and restaurant over internal HTTP; build chunks
  (`ai/chunking.py`); embed in batch; replace that restaurant's documents in one transaction;
  skip if `source_version` is unchanged.
- Item chunk format: `Dish: {name} at {restaurant} (${price}) | Category: {category} |
  Description: … | Options: {group: option(+price)} | Dietary: … | Availability: In Stock`.
- `python -m ai.ingestion --backfill` walks `menus/internal/restaurant-ids`; it also runs once at
  worker startup, so menus published before this existed and lost events are reconciled.

Exit check: a publish produces one row per item with a 384-dim embedding and correct metadata;
removing an item deletes its row; replaying an event does no work.

### Phase 4 — Semantic search (planned): `POST /api/v1/ai/search`

- Request: `query`, `top_k` (1 to 20, default 5), `max_price`, `dietary_filters[]`, optional
  `restaurant_id`. Response: `matches[]` (item, restaurant, price, tags, `similarity_score`) and
  `restaurants[]`.
- One SQL statement: `WHERE is_available AND base_price <= … AND dietary_tags @> … ORDER BY
  embedding <=> … LIMIT k`; `similarity_score = 1 - distance`. Inactive restaurants are excluded.
- **Filtered HNSW pitfall:** an HNSW scan stops after `ef_search` candidates, so a heavy filter
  can return fewer than `k` rows. Set `hnsw.iterative_scan = relaxed_order` and
  `hnsw.ef_search = 100` per query (`SET LOCAL`), and assert the count in tests.
- RBAC: new `ai:search` permission granted to `customer` (and `system_admin` via the existing
  cross join), a `route_permissions` row, and `require_permission("ai:search")` in the handler.
  Shipped as `db/user/add_ai_permissions.sql` plus the `init.sql` and bootstrap mirrors.

Exit check: "spicy dishes under $15" returns only available, tagged items at or under 15; a
customer gets 200, a rider 403 at the gateway, no token 401.

### Phase 5 — RAG context (planned): `POST /api/v1/ai/rag-context`

`services/ai/rag_context.py` runs three sources concurrently, each with its own timeout:
1. **Vector:** top-K items and restaurants for `prompt_query` (reuses Phase 4).
2. **Analytics:** the customer summary (favourite vendors).
3. **Riders:** available-rider count and nearest distance around each candidate restaurant, or
   around the caller's `latitude`/`longitude` when supplied.

Returns `{query, items[], restaurants[], customer_profile{}, courier_availability[],
sources_failed[]}`.

- **Partial failure degrades:** a source that times out is listed in `sources_failed` instead of
  failing the call, so Week 5 can say "courier data unavailable" rather than invent it.
- **Ownership:** `customer_id` must be the caller unless the caller is an admin
  (`require_self_or_admin`).
- **Location:** optional `latitude`/`longitude` extend the spec's request body, which has no
  location. Record this in D62.
- RBAC: new `ai:rag_context` permission granted to `customer`.

Exit check: a customer with delivered orders sees their top restaurant; a rider near a
restaurant shows `available_riders >= 1`; stopping analytics returns 200 with
`sources_failed: ["analytics"]`; another customer's id returns 403.

### Phase 6 — Telemetry, tests, documentation (planned)

- **Metrics** (`sfo_ai_*`): search requests and latency, embedding latency, ingestion events and
  documents indexed, RAG source failures. Spans for embed, vector query and each RAG source. A
  small Grafana "AI retrieval" dashboard.
- **Smoke test:** an "AI retrieval (Week 4)" section: health and index checks; publish a tagged
  menu and poll search until it appears; price, dietary and availability filters; item removal;
  RBAC 401 and 403; RAG context with history, riders and a degraded source; event replay.
  Menu-tag cases belong here too.
- **Postman:** a new "AI retrieval" folder; document `dietary_tags` on menu publish.
- **Docs:** D59 to D62 in key-decisions; a README AI section and env vars (including
  `VECTOR_POSTGRES_PASSWORD`, `EMBEDDING_PROVIDER`, `OPENAI_API_KEY`); architecture diagram, ERD
  (`sfo_vector_core` partition and `menu_items.dietary_tags`), and an AI search sequence diagram.

## 5. Verification (end to end, after Phase 6)

1. Run `scripts/init_bootstrap.sh` in a scratch copy and diff it against the repo (D20 mirror).
2. `docker compose up -d --build`, restart the gateway, run `./scripts/smoke-test.sh --wait`.
   Existing checks still pass (apart from the 3 known `compensation_failed` timing failures) and
   the new AI section is green.
3. Manually publish a menu with `"dietary_tags":["spicy","gluten-free"]`, then
   `POST /api/v1/ai/search {"query":"something hot and filling","max_price":15}`; `rag-context`
   returns all three sources.
4. `EXPLAIN ANALYZE` on the search SQL shows an `hnsw` index scan.

## 6. Out of scope for Week 4

- LLM generation, SSE streaming and LangGraph (Week 5).
- Line items in order projections, which "favourite dishes" would need.
- Restaurant change events: a restaurant's vector document refreshes when its menu is
  republished, not when the restaurant record changes.
- Re-embedding after a model switch: truncate the document tables and run `--backfill`.
