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
