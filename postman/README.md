# SmartFoodOps — Postman Collection

`smartfoodops.postman_collection.json` is every route the Nginx gateway exposes across all 7
services (user, restaurant, menu, order, payment, rider, orchestrator), built from the same
contract [scripts/smoke-test.sh](../scripts/smoke-test.sh) drives: 129 requests across 9 folders,
covering the happy path, authorisation boundaries, validation edge cases, and the Temporal-driven
order saga end to end (kitchen accept/reject, rider dispatch, pickup, delivery).

It was generated from a script rather than hand-written, precisely so its 129 requests could be
checked for duplicate names and well-formedness before being trusted — see the header comment in
that script if you ever need to regenerate or extend it rather than hand-editing the JSON.

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

**Run order matters.** Folders are numbered 1–9 because later ones read collection variables
(bearer tokens, ids) that earlier ones set via `pm.collectionVariables.set(...)` in their Tests
scripts — the same reason `scripts/smoke-test.sh` is one long script rather than independent
pieces. Use the **Collection Runner** (Postman's GUI test runner, not the `Send` button) and run
the whole collection top to bottom in one sitting the first time.

A collection-level **Pre-request Script** stamps a run-unique `{{tag}}` (and per-identity emails,
phones, and a shared `{{password}}`) into collection variables the first time any request fires —
the same reason `smoke-test.sh` tags every identity with `smoke$(date +%s)$$`. That means you can
re-run the whole collection from the top as many times as you like without tripping the
email/phone `UNIQUE` constraints; you don't need to reset anything between runs.

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
- **The `GET /api/v1/payments/{id}` request** is included but needs `{{payment_id}}` filled in by
  hand — the customer never learns that id from any API response (the saga creates the payment,
  D30), so `smoke-test.sh` reads it out of `sfo_payment_core` directly. From Postman, pull it the
  same way, or off a Jaeger trace / the `order.payment.authorized` Kafka event.

None of this is a gap in the collection so much as a reminder that a Postman collection is an HTTP
client, and a few of this platform's strongest guarantees are provable only by looking at what
actually landed in a database or a message log — see
[readme/observability-and-eventing-guide.md](../readme/observability-and-eventing-guide.md) for
where to look for those directly (Jaeger, the outbox tables, Kafka).
