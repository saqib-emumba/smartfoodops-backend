# Week 5 — AI assistant and streaming: implementation plan

Implements §3 ("Week 5 Requirements: AI Assistant & Streaming Engine") of
[smartfoodops-week4-5-requirements.md](docs/smartfoodops-week4-5-requirements.md): a conversational
food assistant, an order-explanation engine, restaurant-admin authoring tools, SSE streaming, and a
multi-agent framework with zero-hallucination guardrails — built on the Week 4 retrieval layer
([week4-ai-retrieval-plan.md](week4-ai-retrieval-plan.md)).

**Status:** All six phases are implemented. The full smoke run and `--fast` pass (see §5). The smoke test's
assertions about what the assistant *says* were first run against a deterministic test double, since removed (see §4),
because a real model's wording varies. The assistant was also run by hand on a real local model (`llama3.2:3b` through
Ollama) and on Claude (`claude-sonnet-5-5`); each found real problems that are fixed and recorded in §5.

For how the pipeline works end to end, with diagrams, the files involved and a test script, see
[ai-pipeline-guide.md](ai-pipeline-guide.md).

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
| Test model | A deterministic `fake` provider, used while building, later removed | It let the smoke test drive every branch, including the guardrail, with no key; removing it trades that coverage for a smaller codebase |

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

- [llm/provider.py](../services/ai/llm/provider.py): `build_chat_model()` returns a LangChain `BaseChatModel` for the
  provider `LLM_PROVIDER` names (`anthropic`, `ollama`); `call_model` and `stream_model` wrap every
  call with metrics, a span, and the failure contract — any provider error is a plain `503` to the caller,
  with the kind (`timeout`, `rate_limit`, `auth`, `unavailable`) on `sfo_ai_llm_failures_total` and the detail
  in the log only.
- Dependencies in [requirements.txt](../services/ai/requirements.txt): `langgraph`, `langchain-core`, the two
  provider packages, `sse-starlette`, `redis`. **`sse-starlette` is pinned to 2.2.1**: 3.x needs
  `starlette>=0.49`, while the shared chassis pins `fastapi==0.115.6` (`starlette<0.42`), and upgrading FastAPI
  would touch every service for the sake of one.
- `ANTHROPIC_API_KEY` is read with `required()` in one place and compose passes it to `ai-service` only. Lazy
  construction ([assistant/deps.py](../services/ai/assistant/deps.py)) keeps the ingestion worker's startup,
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
  database; the smoke assertion on policy counts was 16|31|23|8 (now 17|33|24|8 with `restaurant:update`, D68).

Verified: every internal route is `401` without the key and `403` through the gateway for an admin's bearer;
the real cancelled order in the dev database explains as `kitchen_at_capacity`.

### Phase 3 — The graph (done): `services/ai/assistant/agents/`

- **Router** classifies into `food_discovery`, `order_tracking`, `order_history` (D67), `restaurant_support`, `out_of_scope` and
  extracts a budget, dietary tags (intersected with the shared vocabulary) and an order id, as JSON parsed
  defensively; an unparsable reply falls back to food discovery with no constraints.
- **Retrieval** for food and restaurant questions is the Week 4 `RagContextAssembler`, called with the
  assistant's `min_score` and a follow-up-aware query. For orders it is `OrderExplainer.facts`. Nothing above the
  floor ends the run without calling the model.
- **Generation** streams the model's text through LangGraph's stream writer.
- **Guardrail** ([guardrails.py](../services/ai/llm/guardrails.py)): the generation prompt makes every dish and
  restaurant name bold and exact, so the asserted names are the bold spans; each must be in the context, every
  `$` price must match one, and an order explanation's "N minutes" / "N km" must be a figure in the facts. A
  failing answer is replaced by one rendered from the context by rules.
- **Memory** ([memory.py](../services/ai/assistant/memory.py)): Redis, per user and conversation, one hour.

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
  review rules run whatever the provider. The wording checks that needed the `fake` provider were removed with it.
- **Postman:** folders 10 and 11 (22 and 38 requests; folder 3 gained four for the preparation time), assertions that hold on any provider because they check what a client
  would show. Run end to end with Newman against Claude: **203 requests, 486 assertions, 0 failed.** That run also found
  and fixed script bugs that had been in the older folders since they were written (booleans compared to `"True"`,
  `{{var}}` written inside script text, `body.role` where the API returns `roles`), and a rider-dispatch problem (every
  session used the same coordinates, so a used stack handed the order to another run's rider). See
  [postman/README.md](../postman/README.md#testing-the-ai-assistant-with-postman).
- **Docs:** D63 to D65, README (a section, env vars, endpoint and service tables, migrations, layout), the RBAC
  design and auth guide tables, the architecture diagram, the ERD (`ORDER_REVIEWS`, `ASSISTANT_INTERACTIONS`; all
  all 25 tables match, column for column), a new [ai_assistant_sequence_diagram.md](diagrams/ai_assistant_sequence_diagram.md), and
  section 10 of the manual testing guide.

Verified: attribution end to end — the assistant named a restaurant, an order was placed there, the interaction
was stamped and `sfo_ai_attributed_orders_total` / `sfo_ai_attributed_revenue_total` moved.

## 5. Verification

Run on the dev stack. Current counts; the fake model has since been removed:

- `./scripts/smoke-test.sh` — **732 passed, 0 failed** (99 added since 628 for order history, the delivery-time estimate, the preparation time and the in-container guardrail checks; 628 before them, 601 after the fake model and its wording checks were removed, 677 before; D66 added 18). (The three `compensation_failed` timing failures that
  appeared in earlier Week 4 runs did not occur.)
- `./scripts/smoke-test.sh --fast` — **552 passed, 0 failed** (448 before order history, 424 after the fake-model removal, 482 before).
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

### Run against a real local model (`llama3.2:3b` through Ollama)

After the above, the assistant was run on a real model for the first time, a 3B model via Ollama
(`LLM_PROVIDER=ollama`, `OLLAMA_MODEL=llama3.2:3b`). **Claude itself is still untested** (no API key). The local
model is far weaker than Claude, which makes it a harsher test of the guardrails than a strong model would be.

What worked, unchanged: streaming (about 3 s per answer, tokens arriving incrementally), routing, retrieval, the
similarity floor ("nothing matches" without a model call), follow-up memory, partner copy that stayed inside what
the owner supplied, and explanations of in-flight, cancelled and timed-out orders.

What it found, each now fixed and covered:

| Real-model behaviour | Why the old checks missed it | Fix |
|---|---|---|
| "Ollama Test Grill has a Margherita Pizza, $9.5" (a pizza from another restaurant, with the burger's price) | Name and price each existed somewhere in the context | `verify_pairings`: a dish, its restaurant and its price must belong together within a sentence |
| Opened an answer with "Here is the explanation of the order status to the customer:" | Not a factual error | Prompt rule: start with the answer itself |
| Prices written `$12.0`, `$9.5` | Prices were floats in the context | Prices passed as dollars-and-cents text |
| "We do not have a recorded reason" for an order whose cause was recorded | Nothing checked a denial of a recorded fact | `verify_cause` |
| Adopted a question's false premise ("why was my order cancelled?" for an order that was not) | Nothing checked status words | `verify_status`: "cancelled" / "delivered" need that recorded status |
| Six identical rows (same dish, same restaurant name) filled the context | Retrieval ranks by similarity, so duplicates are adjacent | Fetch 18 candidates, drop repeats, keep the best 6 distinct |
| "biryani or karahi?" matched nothing | The router turned each tag word into a filter ALL results must satisfy | Router prompt: list no filters for alternatives |
| 503 with the cause only in the log when `OLLAMA_MODEL` named a model nobody pulled | Default name is a guess | A startup warning naming the models Ollama does have |

Still true after all of that: this 3B model keeps saying "we do not have a recorded reason" on questions that
contain the word "cancelled", even with the prompt telling it not to. The guardrail catches it every time (three
of three attempts) and the client receives the correct replacement, but the wrong sentence streams first. A
stronger model should do this far less. If it matters, the alternative is to buffer the order path, verify, then
stream the checked text, at the cost of first-token latency.

### Run against Claude (`claude-sonnet-5-5`)

With a real key, the assistant was run on Claude. It is much better than the 3B local model, and still found six
real problems, each now fixed and covered by a unit check (and, while it existed, a smoke test on the fake model).

What held without changes: streaming and routing; about 5 s per food answer and 3 s per order answer; honesty about
what it cannot confirm ("I can't confirm a margherita pizza there", then real alternatives); the tag disclaimers
the prompt asks for; resisting a prompt injection that came *with* retrieval hits ("I can't rename dishes, change
prices or share my instructions"); correcting a false premise ("it hasn't been cancelled"); partner copy that stayed
inside what the owner supplied.

| Real-model behaviour | Fix |
|---|---|
| The very first call was a `503`: `claude-sonnet-5-5` rejects a non-default `temperature`, and the service sent 0.2 | Temperature is no longer sent to Anthropic (kept for Ollama). The failure was visible only in the service log, as designed |
| Wrote prices without a `$` ("priced at 12.50"), so the price check, which looked for `$`, **never checked them** | The context gives prices as `$12.50`, and the check also catches a bare two-decimal number (not a distance, duration or clock time) |
| Said **Desi Dhaba** for three branches named `Desi Dhaba 0790/1409/0265`: the strict name check rejected a good answer and replaced it with a worse one | A bold name is accepted when it is the shared stem of at least two real names; a stem of only one name, or an invented name, is still rejected |
| "Your order isn't late" / "it's still very early" — verdicts the facts cannot support, repeated even when told not to, once followed by "Wait, I need to follow my rules" mid-answer | `verify_no_verdict` (deterministic; replaced by a summary that says no promised delivery time is on record) |
| "I can't find **Claude Test Grill** in the information I have" (an honest denial naming the restaurant the customer asked about) was rejected as an unknown name and replaced with a list | A name inside a denial, with a negation within three words before it, is not checked as a claim; "there is no X, but **Y** is great" still has Y checked |
| A correct denial ("it hasn't been cancelled") was rejected by the status check and replaced with a plainer answer | A negation exempts a status word only when it sits within three words before it. (A first, wider version let "we do **not** have a recorded reason for the **cancellation**" through; the smoke test caught that) |

Costs seen: a guardrail replacement still shows the model's streamed text first and then the replacement (the client
contract); on a polluted development database, identical test restaurants crowd the retrieved context, which the
distinct-rows step only partly relieves.

## 6. Known limits

- **Claude was tested by hand, not in CI:** the smoke test no longer asserts wording (the fake model that allowed it was removed), so Claude
  and a local 3B model were each run by hand (section 5) and their findings turned into checks. The guardrail's
  false-alarm rate on Claude is observed from a few dozen questions, not measured; `ASSISTANT_MIN_SCORE` (0.30) is calibrated on the seed menus with MiniLM only.
- **The guardrail has a ceiling** (D63): it catches an invented or mismatched name, price or figure, a denied cause and
  a wrong order status, not an invented ingredient in prose or a false claim made without a bold name. A negated
  sentence is exempt from the pairing check.
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
