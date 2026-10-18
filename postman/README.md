# SmartFoodOps — Postman Collection

`smartfoodops.postman_collection.json` is every route the Nginx gateway exposes across all 8
services (user, restaurant, menu, order, payment, rider, orchestrator, ai), built from the same
contract [scripts/smoke-test.sh](../scripts/smoke-test.sh) drives: 191 requests across 12 folders,
covering the happy path, authorisation boundaries, validation edge cases, and the Temporal-driven
order saga end to end (kitchen accept/reject, rider dispatch, pickup, delivery), the Week 4 AI
layer (dietary-tagged menus, semantic search, RAG context), and the Week 5 AI layer (the streaming
assistant, order explanations, order reviews, the restaurant-admin authoring tools).

The JSON is hand-maintained directly — there is no generator script in this repo despite what an
earlier version of this doc said. When you add or change a request, check it for duplicate names
and valid JSON yourself before trusting it (e.g. `python3 -c "import json;
json.load(open('smartfoodops.postman_collection.json'))"`).

## Import

1. Postman → **Import** → both files in this folder:
   - `smartfoodops.postman_collection.json`
   - `smartfoodops-local.postman_environment.json`
2. Select the **SmartFoodOps Local** environment (top-right environment picker).
3. Open its variables and set `internal_api_key` to the `INTERNAL_API_KEY` value from your own
   `.env` (see the [README's Environment file section](../README.md#environment-file)). A handful
   of requests — the audit-log write, the internal order read, the saga's internal boundary
   checks — need it; without it those requests will 401, which is itself the correct answer for
   an unauthenticated caller, just not a useful one for testing the authenticated path.
4. Make sure the stack is up: `docker compose up -d` from the repo root, then
   `./scripts/smoke-test.sh --wait` if you want confirmation everything is actually healthy
   before you start.

## Running it

**Run order matters.** Folders are numbered 1–11 because later ones read collection variables
(bearer tokens, ids) that earlier ones set via `pm.collectionVariables.set(...)` in their Tests
scripts — the same reason `scripts/smoke-test.sh` is one long script rather than independent
pieces. Use the **Collection Runner** (Postman's GUI test runner, not the `Send` button) and run
the whole collection top to bottom in one sitting the first time.

A collection-level **Pre-request Script** stamps a run-unique `{{tag}}` (and per-identity emails,
phones, and a shared `{{password}}`) into collection variables the first time any request fires —
the same reason `smoke-test.sh` tags every identity with `smoke$(date +%s)$$`. That means you can
re-run the whole collection from the top (Collection Runner, or Newman) without tripping the
email/phone `UNIQUE` constraints. One caveat: if you have been sending requests one at a time with
**Send**, the stamped values persist for the session, so reset the collection's variables before
starting a fresh top-to-bottom run.

The same script gives each session **its own restaurant and rider coordinates**. Dispatch hands an order to the
*nearest* free rider, and a development stack keeps every rider that earlier runs registered where they were left,
so a fixed location gave the order to somebody else's rider and the saga folders stalled on a `403` when this
collection's rider tried to report the pickup. A random location puts only this session's riders in range.

**Folders 7 and 8 contain self-looping poll requests.** Since Week 2 the order lifecycle is driven
by a Temporal saga (`readme/order-saga-orchestration-guide.md`), so a status is reached
*eventually*, not by the time the request that triggered it returns — `POST .../accept` returns
before the rider has actually been dispatched. Each poll request re-sends itself via
`postman.setNextRequest(...)` until the order reaches the status it's waiting on, up to a bounded
number of tries, then moves on regardless (failing its own `pm.test` if the status never arrived).
**This only works in the Collection Runner** — a single `Send` click sends the request once and
stops. In the Runner, set a **Delay** of ~2000ms between requests for these two folders so the
loop doesn't hammer the gateway every few milliseconds.

Individual folders can be re-run later in the same Postman session (e.g. iterating on the Payment
Service folder) as long as folder 2 has already populated tokens this session — reopening Postman
resets collection variables, so start from folder 1 again after a restart.

**Folder 10 (AI Retrieval) needs folders 2–4 first** (an owner token and a published restaurant) and
is best run after 6–8, because a *delivered* order is what gives the customer order history in the
RAG context. Its first search waits ~2.5s in a pre-request script: the menu published just before
is indexed asynchronously by the ingestion worker, via Kafka. The cases that need a container
stopped (a degraded analytics or rider source, or the ingestion worker, to prove search reads price and availability live
rather than from the index, D66), the vector tables' columns inspected, or Kafka inspected (the dead-letter queue) are in
`scripts/smoke-test.sh` only — Postman cannot stop a container.

**Folder 11 (AI Assistant) needs folders 2–4, 6–8 and 10 first** and a chat provider on the AI service. See
[Testing the AI assistant with Postman](#testing-the-ai-assistant-with-postman) below.

## Testing the AI assistant with Postman

Folders 10 and 11 cover the Week 4 retrieval layer and the Week 5 assistant. They were run end to end with Newman
against the live stack on Claude, with no failures.

### 1. Choose which model answers

The assistant needs a chat model behind the AI service. Pick one, then reload the gateway (it resolves the AI
service's address once, so a recreated service otherwise answers `502`):

| Provider | Use it for | Command (from the repository root) |
|---|---|---|
| **ollama** | Real language, local, no key | `ollama pull llama3.2:3b`, then `LLM_PROVIDER=ollama OLLAMA_MODEL=llama3.2:3b docker compose up -d ai-service` |
| **anthropic** (Claude) | Best answers. Needs `ANTHROPIC_API_KEY` in `.env` | `LLM_PROVIDER=anthropic docker compose up -d ai-service` |

```bash
docker exec sfo-api-gateway nginx -s reload
curl -s http://localhost/api/v1/ai/health      # names the provider and model; never the key
```

A `LLM_PROVIDER` line in `.env` beats the default, and a variable in your shell beats `.env`.

### 2. Run it

**Everything, top to bottom (recommended):** import both files, select the **SmartFoodOps Local** environment, and
run the whole collection in the Collection Runner with a ~250 ms delay. It takes about 2–3 minutes. Folder 11 uses ids
and tokens set by earlier folders:

| Folder 11 request needs | Set by |
|---|---|
| tokens (`cust_token`, `owner_token`, `admin_token`…) | folder 2 |
| `{{rest_id}}`, `{{ai_rest_id}}` | folders 3 and 10 |
| `{{order_id}}` (a **delivered** order) | folders 5–7 |
| `{{rej_order_id}}` (rejected by the kitchen, refunded) | folder 8 |

**Just the assistant, again later:** access tokens last **15 minutes**. Re-run folder 2 (log in), then folders 10 and
11. The ids above persist in the session unless you reset it.

**Without the app:** [Newman](#from-the-command-line-newman) runs the same collection.

### 3. Reading a streamed answer

The stream requests send `Accept: text/event-stream` and the service replies with `data:` lines. Depending on your
Postman version the response panel shows events as they arrive or the whole body once the stream ends; either way
the tests run after it has ended. **Open the Visualize tab**: it shows the routed intent, whether the answer was
grounded, how many chunks arrived, what streamed, and, in a red box, the replacement if the guardrail rejected it.

The last chunk carries the verdict: `{"token": "", "done": true, "conversation_id": "…", "intent": "food_discovery",
"grounded": true}`. If `grounded` is `false`, a `replacement` is included and **that is what a client should show**
instead of what streamed.

### 4. Try your own questions

Duplicate **Assistant stream: a food question**, open **Params** and edit `q`. The first stream request stores the
conversation id as `{{ai_conversation_id}}`; add `conversation_id={{ai_conversation_id}}` to a second request to ask a
follow-up ("and something vegan?"). Add `latitude` and `longitude` together for "near me". Good questions to send:

| Ask | Proves |
|---|---|
| `Recommend a spicy halal dish under $15` | Budget and tag extraction, hybrid retrieval, a grounded answer with real prices |
| `Do you have a margherita pizza at <a restaurant that has none>?` | It does not invent a dish |
| `Recommend a dish under $0.50` | The similarity floor: "I couldn't find anything…", no model call |
| `Who won the 2018 football world cup?` | The router refuses |
| `Why was order <rej_order_id> cancelled, and was I charged?` | The recorded cause and "your payment was refunded" |
| `Is my order late?` | No verdict: it says what happened and that no promised time is on record |

### 5. What the requests assert

Assertions are written to hold on **any** provider, because a real model's wording varies: they check what the client
would **show**, which is the replacement when the guardrail rejected the model's text.

| Group | Examples |
|---|---|
| Access control and validation | no token `401`, a rider or an owner `403`, a one-character question `422`, a malformed conversation id `422`, a latitude without a longitude `422` |
| Fixed replies | off-topic is declined; an impossible budget gives "nothing matches" |
| Order questions and explanations | a rejected order is explained as cancelled and refunded, never "not charged", never a lateness verdict; a delivered order has no invented cause or refund |
| Internal routes stay internal | the four new internal order and rider routes answer `403` even to an admin's bearer |
| Partner tools and reviews | copy stays inside the owner's input; an owner is refused for another owner's restaurant; one review per delivered order |

The first request in folder 11, **AI health names the chat provider**, stores the provider as `{{ai_llm_provider}}`
and shows it in the Console.

The assertions check what a client would show, so they hold whichever real model answers. The guardrail's own
failure cases (a made-up dish, a wrong refund claim, a lateness verdict) are not probed here: a real model cannot be
made to misbehave on demand. The rules live in `services/ai/llm/guardrails.py` and are exercised only when a real
answer happens to break one.

### From the command line (Newman)

```bash
# The internal key is only needed for the requests that call internal-key routes directly (folders 5-7).
# Taking it from the running container keeps it out of your shell history and files.
export INTERNAL_API_KEY=$(docker exec sfo-order-service printenv INTERNAL_API_KEY | tr -d '\r\n')

# Run from this postman/ directory. host.docker.internal is how the container reaches the gateway on your machine.
docker run --rm -v "$PWD:/c:ro" postman/newman:alpine run /c/smartfoodops.postman_collection.json \
  -e /c/smartfoodops-local.postman_environment.json \
  --env-var base_url=http://host.docker.internal --env-var "internal_api_key=$INTERNAL_API_KEY" \
  --delay-request 250 --timeout-request 150000 --color off
```

Measured against Claude: 191 requests, all passing, in about 150 s, with the one manual request reported as skipped. **Without the
internal key exactly 9 assertions fail**, each on a request that correctly answers `401` when the key is missing
(the audit-log write, the internal order read, and the two rider-report durability checks).

### Troubleshooting

| Symptom | Cause |
|---|---|
| `502` on every AI request | The AI service was recreated: `docker exec sfo-api-gateway nginx -s reload` |
| Stream requests answer `503` | The model call failed. The reason is only in the log: `docker compose logs ai-service \| grep "Chat model failed"` (no key, Ollama model not pulled, Ollama not running) |
| Folder 11 order requests fail, folder 8 never ran | `{{rej_order_id}}` and `{{order_id}}` are set by folders 5–8; run them first |
| `401` partway through | Access tokens last 15 minutes; re-run folder 2 |
| Everything fails on a second run | Variables persist in the app session after single sends; reset the collection's variables |

## Testing just the happy path

To walk through register → onboard → order → deliver without the authorisation-boundary and
validation-edge-case noise mixed into folders 2–6, send these requests **in this order**. None of
them loop, so individual `Send` clicks are fine here — no Collection Runner needed yet:

| # | Folder | Request |
|---|---|---|
| 1 | 2. Authentication & Identity | **Register Restaurant Owner** |
| 2 | 2. Authentication & Identity | **Register Customer** |
| 3 | 2. Authentication & Identity | **Register Rider** |
| 4 | 2. Authentication & Identity | **Login Owner** |
| 5 | 2. Authentication & Identity | **Login Customer** |
| 6 | 2. Authentication & Identity | **Login Rider** |
| 7 | 3. Restaurant Service | **Onboard restaurant** |
| 8 | 4. Menu Service | **Publish menu** |
| 9 | 5. Order Service -- Core | **Create order** |
| 10 | 6. Order Saga -- Fleet Setup & Internal Boundaries | **Rider registers a profile** |

That covers user registration, restaurant + menu creation, customer registration, and placing the
order — the saga takes over from here (authorising payment, confirming the order, dispatching the
rider), so the rest has to be watched for asynchronously rather than sent once.

**From here, run all of folder 7 — "Order Saga -- Happy Path to 'delivered'" — with the
Collection Runner**, not individual `Send` clicks. Every poll request in it is wired via
`postman.setNextRequest(...)` to the *next specific request in that folder*, so it only works
running the folder as a whole (see "Running it" above for why, and the ~2000ms delay setting).
Running it end to end takes you through, in order:

1. **Poll: order reaches 'confirmed'** — waits out payment authorisation
2. **Kitchen queue shows the order**, then **Kitchen accepts the order** — the restaurant owner's decision
3. **Poll: order reaches 'assigned'** — waits out rider dispatch, and resolves which of your
   registered riders actually got it into `{{carrier_token}}`
4. **Rider reports the pickup**, then **Poll: order reaches 'picked_up'**
5. **Rider reports the delivery**, then **Poll: order reaches 'delivered'**
6. **The trail records the full lifecycle** — asserts the whole
   `created → confirmed → assigned → picked_up → delivered` sequence in one read

A handful of authorisation-boundary requests are interleaved into folder 7 itself (the
system_admin/restaurant_admin kitchen-queue checks, "the other rider cannot report this
delivery"); they're read-only or refused on purpose, so leaving them in the run doesn't touch
order state — there's no need to strip them out for a happy-path run.

If you only want to *watch* a run rather than drive it — e.g. you already have the stack up and
just want to see the lifecycle happen — [scripts/smoke-test.sh](../scripts/smoke-test.sh) does the
identical sequence from the terminal in one command and prints each step as it passes.

## Manual walkthrough — exact requests and sample data

The table above tells you *which* pre-built request to send. This section is the same
happy path spelled out with **literal request bodies** — useful if you're building the
requests by hand instead of using the collection, or just want to see real values instead of
`{{tag}}`-templated ones. Every number here is the same one used throughout this repo's docs
(`readme/api-testing-guide.md`, `scripts/smoke-test.sh`), so the totals below aren't arbitrary
— they're worth cross-checking if something looks off.

All requests go through the gateway at `http://localhost` (`{{base_url}}`). `Content-Type:
application/json` is required on every POST/PATCH below; it's omitted from each entry for
brevity.

**These exact values only work once** — `email` and `phone` are `UNIQUE` columns, so a second
run with the same body gets a `409 Conflict` on registration. Change the email/phone (e.g.
`customer2@example.com`) to run it again, or skip straight to step 2 and reuse the token you
already have.

### 1. Register a customer

```
POST /api/v1/users/register

{
  "email": "customer@example.com",
  "password": "Passw0rd!",
  "full_name": "Jane Customer",
  "phone": "+15550100001",
  "role": "customer"
}
```
Response `201` — copy `body.id` as `customer_id`.

### 2. Log in as the customer

```
POST /api/v1/users/login

{
  "email": "customer@example.com",
  "password": "Passw0rd!"
}
```
Response `200` — copy `body.access_token` as `customer_token`. Every request from here on
that's marked **(customer)** needs header `Authorization: Bearer <customer_token>`.

### 3. Register a restaurant owner and log in

Same shape as step 1/2, with `"role": "restaurant_admin"`:

```
POST /api/v1/users/register

{
  "email": "owner@example.com",
  "password": "Passw0rd!",
  "full_name": "Owen Owner",
  "phone": "+15550100002",
  "role": "restaurant_admin"
}
```
then `POST /api/v1/users/login` with the same `email`/`password` → `owner_token`.

### 4. Register a rider and log in

Same shape again, `"role": "rider"`:

```
POST /api/v1/users/register

{
  "email": "rider@example.com",
  "password": "Passw0rd!",
  "full_name": "Ryan Rider",
  "phone": "+15550100003",
  "role": "rider"
}
```
then `POST /api/v1/users/login` → `rider_token`.

### 5. Onboard a restaurant (owner)

```
POST /api/v1/restaurants/onboard
Authorization: Bearer <owner_token>

{
  "name": "Downtown Diner",
  "address": "221B Baker Street",
  "latitude": 33.68,
  "longitude": 73.04,
  "capacity": 10
}
```
Response `201` — copy `body.id` as `restaurant_id`.

### 6. Publish its menu (owner)

```
POST /api/v1/menus
Authorization: Bearer <owner_token>

{
  "restaurant_id": "<restaurant_id>",
  "categories": [
    {
      "category_id": "c1",
      "category_name": "Mains",
      "display_order": 1,
      "items": [
        {
          "item_id": "burger",
          "name": "Burger",
          "description": "Beef burger",
          "base_price": 10.00,
          "is_available": true,
          "customization_groups": [
            {
              "group_id": "cheese",
              "group_name": "Cheese",
              "min_selection": 1,
              "max_selection": 1,
              "options": [
                { "name": "cheddar", "extra_price": 1.50 },
                { "name": "none", "extra_price": 0.0 }
              ]
            },
            {
              "group_id": "extras",
              "group_name": "Extras",
              "min_selection": 0,
              "max_selection": 2,
              "options": [
                { "name": "bacon", "extra_price": 2.00 },
                { "name": "egg", "extra_price": 1.00 }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```
Response `200`.

### 7. Register the rider's fleet profile (rider)

```
POST /api/v1/riders
Authorization: Bearer <rider_token>

{
  "vehicle_type": "motorbike",
  "vehicle_number": "RIDER-001",
  "current_latitude": 33.68,
  "current_longitude": 73.04
}
```
Response `201`. `current_latitude`/`current_longitude` need to be near the restaurant's
coordinates — dispatch searches within a 10km radius.

### 8. Place the order (customer)

```
POST /api/v1/orders
Authorization: Bearer <customer_token>
X-Idempotency-Key: order-happy-path-001

{
  "restaurant_id": "<restaurant_id>",
  "items": [
    {
      "item_id": "burger",
      "quantity": 2,
      "customizations": { "cheese": "cheddar", "extras": ["bacon"] }
    }
  ],
  "total_amount": 27.00
}
```
Response `201`, `body.status` is `"created"` — copy `body.id` as `order_id`. The total is
recalculated server-side: base `10.00` + cheddar `1.50` + bacon `2.00` = `13.50` per burger,
`× 2` = `27.00`. Send a `total_amount` that doesn't match and you'll get a `422` instead — the
server never trusts the client's arithmetic.

**Everything past this point happens because the saga is running, not because you send another
request that "does" it.** Re-send `GET /api/v1/orders/<order_id>` (customer) every couple of
seconds to watch `status` change on its own.

### 9. Wait for `status: "confirmed"`

```
GET /api/v1/orders/<order_id>
Authorization: Bearer <customer_token>
```
Poll until `body.status` is `"confirmed"` (payment authorised — a few seconds).

### 10. Kitchen accepts the order (owner)

```
POST /api/v1/orders/<order_id>/accept
Authorization: Bearer <owner_token>
```
Response `200`, `body.decision` is `"accepted"`. No body needed.

### 11. Wait for `status: "assigned"`

Same poll as step 9. Once `body.status` is `"assigned"`, `body.rider_id` is populated — that's
who dispatch picked.

### 12. Rider reports pickup

```
POST /api/v1/riders/me/orders/<order_id>/picked-up
Authorization: Bearer <rider_token>
```
No body. Response `200`. Poll again for `status: "picked_up"`.

### 13. Rider reports delivery

```
POST /api/v1/riders/me/orders/<order_id>/delivered
Authorization: Bearer <rider_token>
```
No body. Response `200`. Poll once more for `status: "delivered"` — that's the terminal state.

### 14. Confirm the full trail (optional)

```
GET /api/v1/orders/<order_id>/logs
Authorization: Bearer <customer_token>
```
Response `200`, a list of entries whose `status` values read, in order:
`created, confirmed, assigned, picked_up, delivered`.

## What this can't check that `smoke-test.sh` can

Named here rather than silently missing:

- **Three tables `smoke-test.sh` reads directly** via `docker exec` — `payments` (to confirm a
  refund actually landed and its gateway reference), `order_tracking_logs` (to confirm the exact
  trail row count matches state-change count, catching a retried activity that emitted twice),
  and `riders` (to confirm no rider is left stranded after every saga in a run finished). No HTTP
  response exposes any of these directly; a database client or the psql one-liners in
  `scripts/smoke-test.sh` itself are the only way to see them.
- **Two compensation paths that need direct database manipulation to set up**: the no-rider-
  available path (grounds the entire fleet via a direct `UPDATE riders SET is_available = FALSE`)
  and the at-capacity path (races two orders against a one-slot restaurant and asserts *exactly
  one* wins, which is a property about concurrent scheduling, not something a fixed request
  sequence can reproduce). Both are real, both are covered by `smoke-test.sh` — they're just not
  reproducible through the HTTP API alone.
- **The `GET /api/v1/payments/{id}` request** is included but is *skipped* until you set the collection variable
  `payment_id` by hand — the customer never learns that id from any API response (the saga creates the payment,
  D30), so `smoke-test.sh` reads it out of `sfo_payment_core` directly. From Postman, pull it the
  same way, or off a Jaeger trace / the `order.payment.authorized` Kafka event.

None of this is a gap in the collection so much as a reminder that a Postman collection is an HTTP
client, and a few of this platform's strongest guarantees are provable only by looking at what
actually landed in a database or a message log — see
[readme/observability-and-eventing-guide.md](../readme/observability-and-eventing-guide.md) for
where to look for those directly (Jaeger, the outbox tables, Kafka).
