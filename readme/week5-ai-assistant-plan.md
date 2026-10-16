# Week 5 — AI assistant and streaming: implementation plan

Implements §3 ("Week 5 Requirements: AI Assistant & Streaming Engine") of
[smartfoodops-week4-5-requirements.md](docs/smartfoodops-week4-5-requirements.md): a conversational
food assistant, an order-explanation engine, restaurant-admin authoring tools, SSE streaming, and a
multi-agent framework with zero-hallucination guardrails — built on the Week 4 retrieval layer
([week4-ai-retrieval-plan.md](week4-ai-retrieval-plan.md)).

**Status:** All six phases are implemented. The full smoke run and `--fast` pass (see §5). **Not
verified here:** answers from a real Claude or Ollama model. No `ANTHROPIC_API_KEY` was available, so
every assertion about what the assistant *says* ran against the deterministic test double
(`LLM_PROVIDER=fake`). The provider wiring is exercised up to construction and the no-key path, not a
live completion.

Decisions D63 to D65 are written in
[key-decisions.md](key-decisions.md) under *Part B — semantic retrieval (the AI layer)*.

## 1. What the spec asks for, and what the platform lacked

The spec's four deliverables (§3.4) are a conversational agent, an order explanation engine, an SSE
endpoint, and a LangGraph/LangChain multi-agent system with zero-hallucination prompts.

Exploration found six gaps the spec assumes away:

| Gap | Consequence | Resolved in |
|---|---|---|
| No chat model, LangGraph, or SSE library anywhere in the stack | Nothing to generate with or stream through | Phase 1 |
| No reviews table | "Summarise customer review feedback" has no data | Phase 2 (`order_reviews`) |
| The trail and saga stage are Order Service facts with no read the AI Service may use | Explanations would need the Order database (D01) or Temporal | Phase 2 (internal `explain-context`) |
| The rider route returns nearby counts only | "A courier is N km away" is not answerable | Phase 2 (`order-courier`) |
| A semantic search always returns its nearest rows, however weak (the Week 4 open point) | An off-topic question still gets "matches" | Phase 2 (`min_score`) |
| The gateway buffers responses and times out at 60s | A token stream would arrive all at once | Phase 4 (dedicated nginx location) |

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Orchestration | LangGraph `StateGraph`: router, retrieval, generation, guardrail | The spec describes a graph. Retrieval is mandatory because the edges say so; a tool-calling agent could skip it |
| Model access | LangChain's `ChatAnthropic` / `ChatOllama`, picked by `LLM_PROVIDER` in `ai.llm` | Less code than hand-written provider classes for the same interface |
| Default model | `claude-sonnet-5-5`, Ollama selectable | Answer quality is what a user sees; the self-hosted option keeps D59's "nothing leaves the stack" available |
| Embedder | Unchanged: local, custom (D59) | Week 4 is verified; nothing in the graph needs a LangChain wrapper |
| Grounding | A check in code after generation (bold names, `$` prices, quantities), with a rule-written replacement | The prompt asks; the code is what makes it true. Ceiling stated in D63 |
| Conversation memory | Redis db 3, capped list per `{user}:{conversation}`, one-hour TTL | The stock LangGraph checkpointers need psycopg3 or Redis Stack; `MemorySaver` is unbounded and lost on restart |
| Order facts | One internal Order Service route (order + reduced trail + saga stage) | The Order Service already holds the Temporal client and the tables (D01) |
| Conversion telemetry | Log answers; attribute `order.created` events from Kafka | Changes nothing in checkout; the Order Service never learns the assistant exists (D65) |
| Test model | `LLM_PROVIDER=fake`, keyed on a `[ROLE:...]` tag in each prompt | Lets the smoke test drive every branch, including the guardrail, with no key |

Deviations from the spec's wording, deliberately:

- **No invented causes or ETAs.** The spec's example ("delayed by 6 minutes due to high dinner traffic ...
  1.2 km away") needs a cause and an expected time the platform does not record. An explanation states the
  trail, the saga's recorded `reason` code in words, elapsed time and the courier's distance — and says "no
  recorded reason" when there is none.
- **"Restaurant Support" is read as questions about a restaurant** (what it serves, where, how it is rated),
  answered from its document and its review aggregate. The partner tools (§3.2.C) are separate endpoints.
- **"Popular combo items from Zen Sushi"** cannot be answered as *popular*: no sales data reaches the vector
  store, and order projections hold no line items (D62). The assistant can list a restaurant's dishes; it is
  told to claim nothing the context does not say.
- **The stream is not wrapped in the D35 envelope**, because an event stream has no single body. Errors before
  the first event are ordinary enveloped HTTP errors.

Cross-cutting rules that apply to every phase (unchanged from Week 4):
- **D20:** every edit to compose, nginx, an `init.sql` is mirrored into `scripts/init_bootstrap.sh`.
- **D57:** every gated route needs a `route_permissions` row; an unmapped path is a 403.
- **Internal routes** use `require_internal`, have no permission row, and are unreachable through the gateway.

## 3. Phase overview

| Phase | Scope | Status |
|---|---|---|
| 1 | LLM foundation: dependencies, provider factory, D63 | Done |
| 2 | Source data: reviews, explain-context, courier distance, `min_score`, RBAC | Done |
| 3 | The graph: router, retrieval, generation, guardrail, memory | Done |
| 4 | The SSE endpoint, gateway streaming | Done |
| 5 | `explain-order` and the partner tools | Done |
| 6 | Telemetry, attribution, tests, documentation | Done |

## 4. Phases

### Phase 1 — LLM foundation (done)

- [llm.py](../services/ai/llm.py): `build_chat_model()` returns a LangChain `BaseChatModel` for the
  provider `LLM_PROVIDER` names (`anthropic`, `ollama`, `fake`); `call_model` and `stream_model` wrap every
  call with metrics, a span, and the failure contract — any provider error is a plain `503` to the caller,
  with the kind (`timeout`, `rate_limit`, `auth`, `unavailable`) on `sfo_ai_llm_failures_total` and the detail
  in the log only.
- Dependencies in [requirements.txt](../services/ai/requirements.txt): `langgraph`, `langchain-core`, the two
  provider packages, `sse-starlette`, `redis`. **`sse-starlette` is pinned to 2.2.1**: 3.x needs
  `starlette>=0.49`, while the shared chassis pins `fastapi==0.115.6` (`starlette<0.42`), and upgrading FastAPI
  would touch every service for the sake of one.
- `ANTHROPIC_API_KEY` is read with `required()` in one place and compose passes it to `ai-service` only. Lazy
  construction ([assistant_deps.py](../services/ai/assistant_deps.py)) keeps the ingestion worker's startup,
  and search and RAG, independent of it: without a key only the assistant endpoints answer `503`.

Verified: the image builds, `ChatAnthropic` constructs with a dummy key, and the service without a key
logs a warning and answers `503` on the assistant, `200` on search.

### Phase 2 — Source data (done)

- **Reviews.** `order_reviews` (Order DB): rating 1–5, optional comment ≤ 1000 chars, `UNIQUE (order_id)`.
  `POST /orders/{id}/review` (`order:review`): own order, `delivered` only, once. Internal read of a
  restaurant's count, average and commented reviews — no customer id.
  Migration: [add_order_reviews.sql](../db/order/add_order_reviews.sql).
- **Explain context.** `GET /orders/internal/{id}/explain-context`: the order, its trail reduced to
  `(status, service, reason, time)` plus, on the `assigned` row, the dispatch estimate and distance already recorded in `metadata`, and the moment the kitchen answered (`kitchen_decided_at`, which lets an explanation split the wait into the restaurant's part and the courier search) — never the log's `detail`, `raw_log` or `rider_id` — and the fulfilment and rider
  workflows' `stage` via Temporal Queries, best-effort. `GET /orders/internal/customers/{id}/latest-order`
  so "why is my order late?" needs no id.
- **Courier distance.** `POST /riders/internal/order-courier`: distance from a point to the rider holding an
  order, from the Redis geo index. Never an id, a position or an ETA (the platform's only estimate is a labelled
  mock with kitchen padding in it).
- **`min_score`** on `/search` and `/rag-context`: a cosine floor, applied in SQL beside the HNSW ordering.
- **RBAC.** `ai:assistant`, `ai:explain_order`, `order:review` (customer), `ai:partner` (restaurant_admin), six
  route rows. [add_week5_permissions.sql](../db/user/add_week5_permissions.sql), applied to the running
  database; the smoke assertion on policy counts is now 16|31|23|8.

Verified: every internal route is `401` without the key and `403` through the gateway for an admin's bearer;
the real cancelled order in the dev database explains as `kitchen_at_capacity`.

### Phase 3 — The graph (done): `services/ai/agents/`

- **Router** classifies into `food_discovery`, `order_tracking`, `restaurant_support`, `out_of_scope` and
  extracts a budget, dietary tags (intersected with the shared vocabulary) and an order id, as JSON parsed
  defensively; an unparsable reply falls back to food discovery with no constraints.
- **Retrieval** for food and restaurant questions is the Week 4 `RagContextAssembler`, called with the
  assistant's `min_score` and a follow-up-aware query. For orders it is `OrderExplainer.facts`. Nothing above the
  floor ends the run without calling the model.
- **Generation** streams the model's text through LangGraph's stream writer.
- **Guardrail** ([guardrails.py](../services/ai/guardrails.py)): the generation prompt makes every dish and
  restaurant name bold and exact, so the asserted names are the bold spans; each must be in the context, every
  `$` price must match one, and an order explanation's "N minutes" / "N km" must be a figure in the facts. A
  failing answer is replaced by one rendered from the context by rules.
- **Memory** ([memory.py](../services/ai/memory.py)): Redis, per user and conversation, one hour.

Verified: with the test double, a food question is routed, retrieved against, streamed and grounded; an
off-topic one is declined without a dish; an impossible filter is "nothing matches" with no model call; a model
made to name a dish that exists nowhere is caught and replaced; two turns are four messages in Redis with an
expiry, and a second user reusing the id gets their own history.

### Phase 4 — The SSE endpoint (done): `GET /api/v1/ai/assistant/stream`

- Chunks are `data: {"token", "done"}`; the final chunk (`done: true`, empty token) carries `conversation_id`,
  `intent`, `grounded` and, when the guardrail replaced the answer, `replacement`. Tokens already sent cannot
  be recalled, so replacement is a client contract.
- The first event is pulled before the response starts, so a failure that precedes any output (a bad
  question, someone else's order, the model unavailable) is a real HTTP status in the D35 envelope; a failure
  after streaming begins is a final `{"done": true, "error": ...}` chunk.
- [nginx.conf](../api-gateway/nginx.conf) gets `location = /api/v1/ai/assistant/stream` with
  `proxy_buffering off`, HTTP/1.1 and a 120s read timeout; still behind `auth_request`.

Verified: with the model emitting a token every 0.4 s, chunks reach the client 0.4 s apart through the
gateway — it does not buffer. Role, validation and ownership errors are `401`/`403`/`422` before any stream.

### Phase 5 — Explanations and partner tools (done)

- `POST /ai/explain-order`: facts from the Order, Restaurant and Rider services, ownership checked, the model
  only phrases them, quantities checked, a rule-written summary as the fallback. A claim about the customer's
  money is checked too: "refunded" and "not charged" are stated from the saga's own ordering (compensation
  refunds before it cancels; a declined payment cancels before charging), and `verify_money` rejects a refund or
  not-charged claim the facts do not support. See D64.
- `POST /ai/partner/{describe-dish,promo,review-summary}` (`ai:partner`), each scoped to a restaurant the caller
  owns (verified against the Restaurant Service as the caller). Copy containing a number the owner did not
  supply is replaced by a plain rendering and flagged; counts and averages never pass through the model.

Verified: own order `200` and grounded, someone else's `403`, unknown `404`; an owner is refused for another
owner's restaurant and a customer for the tools; a review summary reports the database's count and average.

### Phase 6 — Telemetry, attribution, tests, documentation (done)

- **Metrics** (`sfo_ai_*`, [metrics.py](../services/ai/metrics.py)): assistant requests by intent and outcome,
  end-to-end and first-token latency, chat-model calls and failures by kind, guardrail violations by kind, dishes
  named, explain and partner requests, recommending interactions, attributed orders and revenue. Spans cover each
  graph node and model call. A *SmartFoodOps — AI Assistant* dashboard
  ([ai-assistant.json](../grafana/provisioning/dashboards/ai-assistant.json), 19 panels).
- **Attribution** (D65): every answer is logged to `assistant_interactions` (ids and outcomes, never text);
  a second consumer in the ingestion worker matches `order.created` to a recent recommendation by the same
  customer for the same restaurant. [add_assistant_interactions.sql](../db/vector/add_assistant_interactions.sql).
- **Smoke test:** a new *AI assistant and explanations (Week 5)* section. Access control, validation and the
  review rules run whatever the provider; the wording checks run only under `LLM_PROVIDER=fake` and are skipped,
  visibly, otherwise.
- **Postman:** folder 11 (21 requests), provider-agnostic assertions.
- **Docs:** D63 to D65, README (a section, env vars, endpoint and service tables, migrations, layout), the RBAC
  design and auth guide tables, the architecture diagram, the ERD (`ORDER_REVIEWS`, `ASSISTANT_INTERACTIONS`; all
  all 25 tables match, column for column), a new [ai_assistant_sequence_diagram.md](diagrams/ai_assistant_sequence_diagram.md), and
  section 10 of the manual testing guide.

Verified: attribution end to end — the assistant named a restaurant, an order was placed there, the interaction
was stamped and `sfo_ai_attributed_orders_total` / `sfo_ai_attributed_revenue_total` moved.

## 5. Verification

Run on the dev stack with the AI service on `LLM_PROVIDER=fake` (no API key was available):

- `./scripts/smoke-test.sh` — **665 passed, 0 failed** (648 before the follow-ups below added assertions). (The three `compensation_failed` timing failures that
  appeared in earlier Week 4 runs did not occur.)
- `./scripts/smoke-test.sh --fast` — **472 passed, 0 failed.**
- A follow-up (explanations also quote the recorded dispatch estimate, and split the wait into the restaurant's time
  and the courier search) was verified on a real delivered order against its raw trail timestamps, on an in-flight
  order waiting on the kitchen, and by unit-checking six timelines (accepted, rejected, instant, waiting on the
  restaurant, searching for a courier, no decision time recorded).
- A second follow-up (explanations say what happened to the money, and `payment_failed` was added to the reason
  table) was verified against real orders of each outcome and their payment rows: a rejected and a timed-out order
  (payment `refunded`), a declined payment (no payment row), and a `compensation_failed` order (payment still
  `authorized`). The full smoke run cross-checks the rejected and declined cases against the Payment database.
- Both include the new *AI assistant and explanations (Week 5)* section and the Prometheus assertions that the
  `sfo_ai_assistant_*`, `sfo_ai_llm_*`, `sfo_ai_guardrail_*`, `sfo_ai_explain_*` and `sfo_ai_partner_*` series
  reach Prometheus. The route-template cardinality bound in the Observability section was raised from 60 to 100:
  Week 5 adds about ten route templates, and a real path leak would be hundreds.

Checked by hand, outside the smoke test: SSE chunks reach the client 0.4 s apart through the gateway (not
buffered); attribution end to end (an order placed at a restaurant the assistant named stamped its interaction
and moved `sfo_ai_attributed_orders_total` and `sfo_ai_attributed_revenue_total`); the default configuration
(`LLM_PROVIDER=anthropic`, no key) answers `503` on the assistant and `200` on search; `nginx -t` on the new
config; the ERD against the live databases (25 tables, two of them named `processed_events`, every column compared).

D20: `scripts/init_bootstrap.sh` run in a scratch directory produced files identical to the repo for all 15
mirrored files (compose, nginx, every `init.sql`, Prometheus, Grafana provisioning, Kafka, RabbitMQ).

## 6. Known limits

- **Real model output is unverified** here (no key). Wording, latency and the guardrail's false-alarm rate
  against Claude are unmeasured; `ASSISTANT_MIN_SCORE` (0.30) is calibrated on the seed menus with MiniLM only.
- **The guardrail has a ceiling** (D63): it catches an invented name, price or figure, not an invented ingredient
  in prose or a false claim made without a bold name.
- **No rate limit** on the LLM-backed endpoints (key-decisions, open questions).
- **Conversion is attribution, not lift,** and restaurant-level, not dish-level (D65).
- **Courier distance and the dispatch estimate are to the restaurant,** not the customer: orders carry no delivery
  coordinates, and no cause is recorded for an order that is merely slow (only for cancellations): an
  explanation says where the time went, not why. The estimate is
  `rider.eta`'s mock, recorded in the `assigned` trail row; explanations quote it as an estimate and never as a delay.
- **Saga stage is best-effort:** a finished workflow or an unreachable Temporal answers "unknown".
- **Conversation text sits in Redis for an hour and a question travels in the URL** (a `GET`, per the spec).
- **After rebuilding `ai-service`, reload the gateway** (`docker exec sfo-api-gateway nginx -s reload`): nginx
  resolves upstream addresses at load, so a recreated container otherwise answers `502`.
