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
