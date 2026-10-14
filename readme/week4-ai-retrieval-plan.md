# Week 4 — AI retrieval layer: implementation plan

Implements §2 ("Week 4 Requirements: Data Preparation & Retrieval Layer") of
[smartfoodops-week4-5-requirements.md](docs/smartfoodops-week4-5-requirements.md): a vector
store, an ingestion pipeline, hybrid semantic search, and a multi-source RAG context assembler.

**Status:** All six phases are implemented and verified.

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
| pgvector client | The `pgvector` Python package, registered once per process with `register_vector(..., globally=True)` | Embeddings travel as float32 numpy arrays instead of hand-built strings. Global registration needs no change to the shared connection pool in `common`. (Phases 1 to 3 first used text literals; switched in Phase 4.) |

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
| 3 | Ingestion pipeline | Done |
| 4 | `POST /api/v1/ai/search` | Done |
| 5 | `POST /api/v1/ai/rag-context` | Done |
| 6 | Telemetry, tests, documentation | Done |

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

### Phase 3 — Ingestion pipeline (done): `services/ai/ingestion.py`

A menu publish becomes up-to-date vectors within seconds, idempotently.

- **Two layers.** `IngestionService` is synchronous and Kafka-free ("make this restaurant's vectors
  match its menu now"); `IngestionConsumer` is the Kafka side (read, validate, retry, dead-letter).
  Backfill, an event and a person at a shell all go through the same service.
- **Own container.** `ai-ingestion-worker` runs the AI image with `python -m ai.ingestion`
  and a metrics server on port 9111, so an embedding job never competes with a search request.
  `AI_PROCESS_NAME` gives each process its own name for logs, traces and the Prometheus job.
- **Per event:** fetch the menu and restaurant over the internal routes; build chunks
  ([ai/chunking.py](../services/ai/chunking.py)); embed the dishes and the restaurant in one batch;
  replace the restaurant's documents and record the event as processed in one transaction.
  Re-ingesting is skipped when the stored `source_version` and embedding model both match.
- **Chunk format:** `Dish: … at … ($…) | Category: … | Description: … | Options: … | Tags: … |
  Availability: In Stock`. Segments with nothing to say are omitted rather than filled with a
  placeholder, so nothing is asserted that the data does not hold. The label is `Tags:`, not the
  spec's `Dietary:`, because the list now holds more than diets.
- **Restaurant document:** name, address, menu sections, a few dish names, the union of its items'
  tags, and open/closed status.
- **Failure handling:** a transient failure retries with backoff (5 attempts) and then
  dead-letters. Retrying inside the handler is deliberate: the loop commits offsets, so letting an
  exception propagate would commit past the unhandled message and lose it. A schema-invalid
  event goes straight to the DLQ; an unknown event type is skipped.
- **Backfill.** `python -m ai.ingestion --backfill` ingests every published menu once and prints
  outcome counts. The worker also does this at startup (retrying while the menu service comes up),
  so a lost event or a menu published before this existed is a delay, not a hole. One restaurant
  failing is logged and counted, not fatal.
- **Metrics:** `sfo_ai_ingestion_events_total{outcome}`, `sfo_ai_documents_indexed{kind}`,
  `sfo_ai_embedding_latency_seconds`, `sfo_ai_ingestion_last_event_timestamp_seconds`.

Verified:
- A publish is indexed in about 0.25 s; republishing with an item removed and a price changed
  updates the rows and deletes the removed one.
- Replaying an event does nothing; a new event for an unchanged menu is skipped; a restaurant with
  no menu is removed; a backfill rerun skips all 33.
- With the menu service stopped, an event retries with backoff and completes once it is back; an
  invalid payload lands on the DLQ with its reason.
- The query "spicy beef burger with cheese" returns the burger first, and `EXPLAIN` shows
  `Index Scan using idx_menu_item_documents_embedding`.
- Prometheus has 15 healthy targets; `--fast` smoke passed 294/294.

### Phase 4 — Semantic search (done): `POST /api/v1/ai/search`

- **Request:** `query` (2 to 500 chars, whitespace-trimmed), `top_k` (1 to 20, default 5),
  `max_price`, `dietary_filters[]` (validated against the shared tag vocabulary), optional
  `restaurant_id`. **Response:** `matches[]` (`item_id`, `item_name`, `restaurant_id`,
  `restaurant_name`, `category_name`, `price`, `dietary_tags`, `similarity_score`) and
  `restaurants[]`, in the D35 envelope with `Retrieved N matching item(s)`.
- **One statement per result kind** in [repositories/vectors.py](../services/ai/repositories/vectors.py):
  a cosine ordering (`<=>`, served by the HNSW index) beside ordinary WHERE clauses, so `top_k`
  applies to rows that already satisfy the filters. Items must be available, within `max_price`,
  carry every requested tag, and belong to an active restaurant. Restaurants must be active and
  have at least one item passing the same constraints.
- **Filtered HNSW:** `SET LOCAL hnsw.iterative_scan = relaxed_order` and `hnsw.ef_search = 100`
  per request (pgvector 0.8.7), so a selective filter still returns every match up to `top_k`.
  `SET LOCAL` keeps the settings from leaking to another request on the same pooled connection.
- **Tag semantics:** a filter requires every tag listed (array containment).
- **RBAC:** new `ai:search` permission granted to `customer` (and `system_admin` through the
  existing cross join), a `route_permissions` row, and `require_permission("ai:search")` in the
  handler. Added to [db/user/init.sql](../db/user/init.sql), mirrored into bootstrap, and shipped
  as [db/user/add_ai_permissions.sql](../db/user/add_ai_permissions.sql) (idempotent; applied to
  the running database).
- An empty result is a 200 with empty lists, not a 404.

Verified on the running stack:
- A customer gets 200, a rider and a restaurant_admin get 403 at the gateway, no token 401.
- "Spicy dishes under $15" returns only available items at or under 15; a vegan filter returns
  only vegan items; a sold-out item is never returned.
- A selective tag filter returns exactly the rows that have it (kosher 2/2, pasta 1/1,
  light-meal 1/1), which is what the iterative scan is for.
- A deactivated restaurant disappears from both lists, scoped and global.
- Unknown tag, blank query, `top_k` of 0 and `top_k` of 50 are all 422.
- Semantic checks: "a cold sweet drink" returns Mango Lassi first; "italian vegetarian dinner"
  returns the three Italian dishes.
- `--fast` smoke passed.

Open point for Week 5: a semantic search always returns its nearest rows, however weak, so an
off-topic query still gets low-scoring matches. A minimum similarity threshold (or a
`min_score` parameter) would let the assistant say "nothing matches" instead of presenting a
poor match; it is not in the spec and not built.

### Phase 5 — RAG context (done): `POST /api/v1/ai/rag-context`

[services/ai/rag_context.py](../services/ai/rag_context.py) gathers three sources, each under its own
time budget (`config.RAG_*_TIMEOUT_SECONDS`):
1. **Vector:** top-K items and restaurants for `prompt_query`, constrained by optional
   `max_price` and `dietary_filters` (the same search as Phase 4, plus restaurant coordinates).
2. **Analytics:** the customer summary from Phase 2, with restaurant names resolved from the
   vector store (the projection keeps only ids).
3. **Courier:** available-rider count and nearest distance around each candidate restaurant, from
   the rider internal route (capped at 5 lookups).

Vector and analytics run concurrently. Courier depends on the vector result (it needs the
candidates' coordinates), so it runs after, with one lookup per restaurant in parallel.

- **Request:** `customer_id`, `prompt_query`, optional `latitude`/`longitude` (both or neither),
  `top_k`, `max_price`, `dietary_filters`. The location fields extend the spec's body: a
  `customer_id` alone has nothing to measure "near me" from.
- **Response:** `query`, `items[]`, `restaurants[]` (with `distance_km`), `customer_profile`,
  `courier_availability[]`, and `sources_failed[]`.
- **Partial failure degrades.** A source that errors or runs out of time is named in
  `sources_failed` and contributes nothing; the call still returns 200 with everything else. This
  is deliberate: a source silently missing would read to the model as "no riders", a claim the
  response cannot make. If the vector source fails there is nothing to anchor courier lookups on,
  so `courier` is reported failed too.
- **Ordering:** relevance picks the candidates; proximity only orders them. With a location,
  restaurants are sorted nearest-first among the semantic top-K, not re-ranked across the index.
- **Ownership:** `customer_id` must be the caller's own (`require_self_or_admin`), because the
  response carries that customer's order history. An admin may ask for anyone's.
- **RBAC:** new `ai:rag_context` permission granted to `customer`, a route row, and
  `require_permission` in the handler; added to `init.sql` and the same idempotent
  [add_ai_permissions.sql](../db/user/add_ai_permissions.sql), already applied. The smoke
  assertion on policy counts is now 12|23|17|8.
- **Courier privacy:** the rider route returns counts and a distance only, never a rider id or
  position, because the result ends up in an LLM prompt.

Verified on the running stack:
- A customer with history gets their order counts, a favourite restaurant resolved to its name
  (3 delivered orders ranks above 1), restaurants nearest-first, and 34 available riders around
  each candidate; a customer with no history gets zeros and an empty list.
- No token is 401; a rider and a restaurant_admin are 403; someone else's `customer_id` is 403;
  an admin may ask for another customer; a latitude without a longitude, or an unknown tag, is 422.
- With the analytics service stopped: 200, `sources_failed: ["analytics"]`, items and courier
  data intact. With the rider service stopped: 200, `sources_failed: ["courier"]`, profile intact.
- `--fast` smoke passed.

Limits worth knowing: favourite *dishes* are not derivable (the projection has no line items), and
the analytics history here was seeded directly into the projection for the test, since the earlier
smoke customers' accounts no longer exist. Phase 6's smoke section will drive a real delivered order.

### Phase 6 — Telemetry, tests, documentation (done)

- **Metrics** (`sfo_ai_*`, [ai/metrics.py](../services/ai/metrics.py)): search and RAG request
  counts by outcome and latency histograms, RAG source failures by source, embedding latency
  (observed in one place, `embed_timed`, so search, RAG and ingestion are all covered), documents
  indexed, ingestion events by outcome, and a last-event timestamp for spotting a wedged worker.
  Spans cover the embed, the vector query and each RAG source. A *SmartFoodOps — AI Retrieval*
  Grafana dashboard ([ai-retrieval.json](../grafana/provisioning/dashboards/ai-retrieval.json),
  12 panels) is file-provisioned like the others.
- **Smoke test:** a new "AI retrieval (Week 4)" section, runnable under `--fast` except the
  history-dependent RAG assertions, which need a real delivered order. It covers: the pgvector
  extension and both HNSW indexes; tag normalization and the unknown-tag 422; search by meaning,
  `max_price`, tag filters, availability, `top_k`, empty results and validation; 401/403 for each
  role; ingestion following a republish; backfill idempotency; dead-lettering of an invalid event;
  RAG context with distance ordering, ownership (own, someone else's, admin), a customer with no
  history, and a customer whose real delivered order shows up as their favourite vendor; and
  graceful degradation with the analytics and rider services stopped. It also waits for Prometheus
  to re-scrape the restarted services, and asserts the `sfo_ai_*` metrics reach Prometheus.
- **Postman:** a "10. AI Retrieval (Week 4)" folder (19 requests), and `dietary_tags` documented on
  the main publish-menu request. Cases that stop containers or inspect Kafka stay in the smoke test.
- **Docs:** D59 to D62 in [key-decisions.md](key-decisions.md); a README section, env vars, endpoint
  and service tables, ports and migrations; section 9 of the manual testing guide; the RBAC design
  and auth guide tables; the architecture diagram, the ERD (an `sfo_vector_core` partition and
  `menu_items.dietary_tags`), and a new
  [ai_search_sequence_diagram.md](diagrams/ai_search_sequence_diagram.md).

Verified: the full smoke run passed 549 of 552 (the 3 failures are the known `compensation_failed`
timing ones), `--fast` passed 379 of 379; the ERD matches all 23 tables with no mismatches; and
`scripts/init_bootstrap.sh` run in a scratch directory produced files identical to the repo for all
15 mirrored files (D20).

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
