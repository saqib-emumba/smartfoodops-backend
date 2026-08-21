# SmartFoodOps — Key Decisions

Why the system is built the way it is. Each entry records what was decided, what it was
decided *against*, and what it costs — the trade-off, not just the outcome.

Scope: this is the engineering decision record. The forward-looking plan (weekly
milestones, stack matrix, grading criteria) lives in
[smartFoodOps_knowledgebase-v2.md](smartFoodOps_knowledgebase-v2.md); how to run and call
the system lives in [../README.md](../README.md) and
[api-testing-guide.md](api-testing-guide.md). This file is the "why" none of those carry.

Add an entry when a choice is not obvious from the code, or when a reviewer would
reasonably ask "why not the other way?". Supersede rather than edit — a decision that was
right in Week 1 and wrong in Week 3 is more instructive than one silently rewritten.

## Index

| # | Decision | Date | Status |
|---|---|---|---|
| [D01](#d01--database-per-service) | Database-per-service, one physical database per owner | 2026-08-13 | Accepted |
| [D02](#d02--cross-service-references-are-plain-uuids-verified-over-http) | Cross-service references are plain UUIDs verified over HTTP | 2026-08-13 | Accepted |
| [D03](#d03--payments-split-into-its-own-service-and-database) | Payments split into its own service and database | 2026-08-13 | Accepted |
| [D04](#d04--a-shared-chassis-in-servicescommon) | A shared chassis in `services/common/` | 2026-08-12 | Accepted |
| [D05](#d05--one-error-contract-for-every-service) | One error contract for every service | 2026-08-12 | Accepted |
| [D06](#d06--the-server-re-prices-every-cart) | The server re-prices every cart | 2026-08-13 | Accepted |
| [D07](#d07--money-is-decimal-until-the-json-boundary) | Money is `Decimal` until the JSON boundary | 2026-08-13 | Accepted |
| [D08](#d08--idempotency-keys-are-mandatory-and-a-replay-answers-200) | Idempotency keys are mandatory; a replay answers `200` | 2026-08-13 | Accepted |
| [D09](#d09--audit-logging-is-best-effort-and-goes-through-the-menu-service) | Audit logging is best-effort, through the Menu Service | 2026-08-13 | Superseded by [D24](#d24--the-tracking-trail-moved-into-the-order-database-and-stopped-being-best-effort) |
| [D10](#d10--the-card-gateway-is-a-seam-not-a-scattered-stub) | The card gateway is a seam, not a scattered stub | 2026-08-13 | Accepted |
| [D11](#d11--authentication-is-rs256-with-the-user-service-as-sole-issuer) | RS256, User Service as sole issuer | 2026-08-18 | Accepted |
| [D12](#d12--tokens-are-verified-in-process-not-at-the-gateway) | Tokens are verified in-process, not at nginx | 2026-08-18 | Accepted |
| [D13](#d13--identity-comes-from-the-token-never-from-the-request-body) | Identity comes from the token, never the body | 2026-08-18 | Accepted |
| [D14](#d14--sessions-are-rotating-refresh-tokens-in-redis) | Sessions are rotating refresh tokens in Redis | 2026-08-18 | Accepted |
| [D15](#d15--two-kinds-of-service-to-service-credential) | Two kinds of service-to-service credential | 2026-08-18 | Accepted |
| [D16](#d16--each-authorisation-decision-lives-in-exactly-one-place) | Each authorisation decision lives in exactly one place | 2026-08-18 | Accepted |
| [D17](#d17--an-unknown-user-id-is-403-not-404) | An unknown user id is `403`, not `404` | 2026-08-18 | Accepted |
| [D18](#d18--role-claims-are-re-checked-over-http-despite-being-in-the-token) | Role claims are re-checked over HTTP | 2026-08-18 | Accepted |
| [D19](#d19--secrets-live-only-in-a-gitignored-env) | Secrets live only in a gitignored `.env` | 2026-08-13 | Accepted |
| [D20](#d20--init_bootstrapsh-regenerates-config-byte-for-byte) | `init_bootstrap.sh` regenerates config byte-for-byte | 2026-08-13 | Accepted |
| [D21](#d21--blueprint-deviations-are-deliberate-and-recorded) | Blueprint deviations are deliberate and recorded | 2026-08-13 | Accepted |
| [D22](#d22--mongodb-was-dropped-menus-are-jsonb-in-postgres) | MongoDB dropped; menus are JSONB in Postgres | 2026-08-21 | Accepted |
| [D23](#d23--menus-are-read-through-a-redis-cache-aside-layer) | Menus are read through a Redis cache-aside layer | 2026-08-21 | Accepted |
| [D24](#d24--the-tracking-trail-moved-into-the-order-database-and-stopped-being-best-effort) | The tracking trail moved into the order database | 2026-08-21 | Accepted |
| [D25](#d25--temporal-orchestrates-the-order-lifecycle-and-the-workflow-id-is-the-order-id) | Temporal orchestrates; the workflow id is the order id | 2026-08-21 | Accepted |
| [D26](#d26--the-worker-authenticates-with-the-internal-key-never-a-forwarded-bearer) | The worker uses the internal key, never a bearer token | 2026-08-21 | Accepted |
| [D27](#d27--restaurant-acceptance-is-a-signal-and-a-timer-not-a-synchronous-call) | Restaurant acceptance is a signal and a timer | 2026-08-21 | Partly superseded by [D32](#d32--the-kitchen-queue-collapsed-into-the-orders-table) |
| [D28](#d28--riders-got-their-own-service-and-database) | Riders got their own service and database | 2026-08-21 | Accepted |
| [D29](#d29--dispatch-prevents-the-race-rather-than-detecting-it) | Dispatch prevents the race rather than detecting it | 2026-08-21 | Accepted |
| [D30](#d30--the-saga-owns-payment-authorisation-so-post-apiv1payments-now-answers-409) | The saga owns payment authorisation | 2026-08-21 | Accepted |
| [D31](#d31--a-status-transition-is-a-compare-and-set-and-the-enum-supplies-the-ordering) | A status transition is a compare-and-set | 2026-08-21 | Accepted |
| [D32](#d32--the-kitchen-queue-collapsed-into-the-orders-table) | The kitchen queue collapsed into the `orders` table | 2026-08-21 | Accepted |

---

## Service and data boundaries

### D01 — Database-per-service

**Decided:** every service owns one physical database with its own credentials —
`sfo_user_core`, `sfo_restaurant_core`, `sfo_order_core`, `sfo_payment_core`,
`sfo_menu_core` — plus Redis, used as a cache by the Menu Service and as a session store by
the User Service. The Menu Service was the last exception, on MongoDB; D22 closed it.

**Instead of:** one shared database with a schema per service, which is cheaper and is what
the first cut of the project used.

**Why:** a schema boundary is a convention; a credential boundary is enforced. With separate
credentials no service can reach another's tables *even by accident* — the mistake fails at
connection time rather than passing review. It also cuts the blast radius of a leaked
password down to one service's data.

**Costs:** no cross-service joins, no cross-service foreign keys, no single transaction
spanning two services. D02 and the Week 2 saga work exist to pay this bill.

### D02 — Cross-service references are plain UUIDs verified over HTTP

**Decided:** `restaurants.owner_id`, `orders.customer_id`, `orders.restaurant_id` and
`payments.order_id` are plain `UUID` columns. The referenced entity is fetched over HTTP
immediately before the write.

**Instead of:** foreign keys (impossible across databases), or trusting the client.

**Why:** the check the foreign key used to perform still has to happen somewhere. Putting it
directly before the insert keeps the window between "verified" and "written" as small as an
HTTP call allows.

**Costs:** the guarantee weakens from *enforced* to *checked*. A restaurant deleted between
check and insert leaves a dangling reference the database can no longer prevent. An unknown
reference surfaces as `422`, matching what the foreign-key violation returned: the request
is well formed, and the missing thing is not the order.

### D03 — Payments split into its own service and database

**Decided:** `payments` moved out of the Order Service into a service on 8005 with its own
database.

**Why:** card handling is the one part of this platform worth isolating on its own — the
compliance boundary shrinks to a single container and database. Operationally, a gateway
outage can no longer starve the threads that place and read orders. It also gives Week 2's
Temporal saga two independently compensatable participants instead of one shared
transaction.

**Costs:** `payments.order_id` lost its foreign key, which forced
`GET /api/v1/orders/{order_id}` into existence (see D21). Paying now costs a network hop.

### D04 — A shared chassis in `services/common/`

**Decided:** `PostgresPool`, `ServiceClient`, `errors`, `logging_config`, `config` and
`auth` live in one package, copied into every image at build time.

**Instead of:** a published internal package, or duplicating the plumbing per service.

**Why:** at five services on one machine a versioned package is overhead that buys nothing,
and duplication would let the failure contract drift service by service. Copying at build
time keeps each image self-contained with no registry in the loop.

**Costs:** every service rebuilds when `common/` changes, and there is no version skew to
hide behind — a breaking change there breaks everything at once. Accepted deliberately;
at this size that is a feature.

### D05 — One error contract for every service

**Decided:** status codes are chosen once in `common/errors.py`, not per handler.
Unreachable dependency → `503`; downstream `404` → `404` reworded by the caller; downstream
`401`/`403` → `403`; downstream `5xx` → `502`; business rule → `422`.

**Why:** the same failure should look the same regardless of which service the client hit.
Centralising it also stops a leaked `500` standing in for "a dependency is down" — the
difference between "retry" and "page someone".

**Costs:** a handler wanting a genuinely different code has to justify it, and the mapping
must be revisited whenever a new failure mode appears — as it was when auth landed and
`401` had to be added.

---

## Correctness and money

### D06 — The server re-prices every cart

**Decided:** the client's `total_amount` is compared, never trusted. Every line is
recomputed from the menu the Menu Service currently serves, and a mismatch is `422` naming
both figures.

**Why:** the client controls its own request, so a price it sends is a suggestion. The menu
is the only authority on what something costs.

**Costs:** an order fails if the menu changed between the customer loading it and checking
out. Correct, but a real user-visible failure that price-at-add-to-cart would avoid.

### D07 — Money is `Decimal` until the JSON boundary

**Decided:** amounts are `Decimal` quantized to `0.01` internally, converted to `float` only
when serialised.

**Why:** binary floats cannot represent `0.10` exactly, and a cart summing three of them
does not reliably equal the number the client computed. Both the equality check in D06 and
the Payment Service's "settles the order exactly" rule depend on exact arithmetic.

**Costs:** conversions at every boundary, and `float` still appears in the API contract — so
the guarantee is internal, not end-to-end.

### D08 — Idempotency keys are mandatory, and a replay answers `200`

**Decided:** `X-Idempotency-Key` is required on order and payment creation, enforced by a
unique index. A replay returns the stored record with `200`. Payments additionally require
the key in the body and reject a mismatch with `400`.

**Why:** `200` rather than `201` because nothing was created — `201` would be a claim the
client might act on. A unique index rather than application locking, because a concurrent
retry is then resolved by the database instead of by a race two services could both lose.

The header-and-body duplication for payments is unusual and was kept because the Week 1
contract specifies it: a caller that disagrees with itself about which transaction it is
retrying is worth rejecting outright.

**Costs:** an inconsistency between the two services — orders read the header alone,
payments demand both.

### D09 — Audit logging is best-effort and goes through the Menu Service

> **Superseded by [D24](#d24--the-tracking-trail-moved-into-the-order-database-and-stopped-being-best-effort) on 2026-08-21.** Kept because the reasoning below is
> exactly what changed: the argument for best-effort rested on the log being a separate
> write after the commit, and once the trail moved into the order database that stopped
> being true.


**Decided:** the Order Service posts the `created` transition to `POST /api/v1/menus/logs`
and never touches MongoDB. A failure there is logged, not returned.

**Why:** the order is already committed when the log call fires. Returning `500` would tell
the client their order failed when it exists — the worst available answer. Routing through
the Menu Service keeps MongoDB ownership with exactly one service.

**Costs:** the audit trail can silently miss entries. Fine while it is a trail; if anything
ever reconciles money from it, this decision has to be revisited.

### D10 — The card gateway is a seam, not a scattered stub

**Decided:** `services/payment/gateway.py` is the only module that simulates authorisation,
and the only one in that service which never touches the database. References are
`ch_mock_…` so a simulated charge is never mistaken for a real one in a log.

**Why:** it is the single file that changes when a live gateway is wired in. Keeping it away
from the database means a gateway failure can never leave a half-written row: the row is
written `pending` first and moved to `authorized` after, which is exactly what makes Week
2's compensation workflow possible.

**Costs:** payments can strand at `pending` if the gateway call fails. Deliberate — that is
the state the saga will reconcile — but nothing sweeps them today.

> **Resolved 2026-08-21 by [D30](#d30--the-saga-owns-payment-authorisation-so-post-apiv1payments-now-answers-409).** `POST /api/v1/payments/refund` moves `pending` rows as
> well as `authorized` ones, so the saga's compensation path is what finally reconciles
> them. `gateway.refund()` joined `authorize()` in the same seam, with `re_mock_` references
> so a refund is never mistaken for the charge it reverses.

---

## Authentication and authorisation

Added 2026-08-18. Before this the system had no authentication at all: passwords were
bcrypt-hashed at registration and never verified, and callers asserted identity by putting a
UUID in the request body.

### D11 — Authentication is RS256, with the User Service as sole issuer

**Decided:** access tokens are RS256. Only `user-service` receives `JWT_PRIVATE_KEY_B64`;
every other service gets the public key.

**Instead of:** HS256 with a shared secret, which is simpler to wire.

**Why:** with a shared secret every service can *mint* tokens as well as verify them, so a
compromise anywhere forges any identity — including `system_admin`. Asymmetric keys make
"can verify" and "can issue" different capabilities, which is the property worth having when
five services share a network. The split is asserted by the smoke test, not just documented.

**Costs:** key generation and distribution; RSA verification is slower than HMAC; rotating
the keypair invalidates every live token at once (see Open questions).

### D12 — Tokens are verified in-process, not at the gateway

**Decided:** a FastAPI dependency in `common/auth.py` verifies the token inside each
service. nginx stays a pure router.

**Instead of:** validating at the gateway — nginx's JWT module (nginx Plus only), or an
`auth_request` subrequest to the User Service.

**Why:** `auth_request` would add a network round-trip to *every* request and make the User
Service a hard dependency for all traffic, including reads that otherwise never touch it.
In-process verification is a signature check with no I/O.

**Costs:** each service must remember to apply the dependency — there is no chokepoint that
fails closed. A new endpoint added without it is silently unprotected, which is why the
smoke test asserts `401` on unauthenticated calls rather than only testing happy paths.

### D13 — Identity comes from the token, never from the request body

**Decided:** `owner_id` and `customer_id` were removed from the request schemas entirely and
are set from the verified token subject.

**Why:** this is the decision that actually closed the hole. While identity arrived in the
body, the role checks were honest but meaningless — anyone holding a `restaurant_admin` UUID
passed them legitimately. Removing the field makes impersonation unrepresentable rather than
merely rejected.

**Costs:** a breaking API change across five services, landed at once. Every client, the
smoke test and both guides had to change together.

### D14 — Sessions are rotating refresh tokens in Redis

**Decided:** a 15-minute stateless access token plus a 7-day opaque refresh token stored
SHA-256-hashed in Redis database 1. Refreshing consumes the presented token in the same
round trip that reads it (`GETDEL`).

**Instead of:** long-lived access tokens with no logout, or server-side sessions throughout.

**Why:** a JWT cannot be withdrawn before it expires, so without a refresh token "logout"
would not log anyone out. Rotation means a captured refresh token stops working the moment
the real client next refreshes. Hashing means a Redis dump is a list of hashes rather than a
set of live credentials — the same reasoning that keeps plaintext passwords out of Postgres.
Database 1 rather than 0, so an accidental `FLUSHDB` on the Menu Service's cache does not
sign every user out.

**Costs:** logout ends a session within one access-token lifetime, not instantly — which is
why that lifetime is short. Redis becomes a hard dependency of the User Service.

### D15 — Two kinds of service-to-service credential

**Decided:** calls made *on behalf of a user* forward that user's bearer token unchanged.
Endpoints no end user may reach — currently only `POST /api/v1/orders/logs` — take a shared
`X-Internal-Key` instead.

**Why:** forwarding means a service can never do more than the user who invoked it. That is
what makes the Payment Service unable to pay for someone else's order without a single line
in the Payment Service saying so. But the audit trail is the one thing a customer must not
be able to write, and a forwarded customer token cannot express that — so it needs a
different credential, not a different check.

**Costs:** a shared symmetric secret, which is exactly the property D11 rejected for tokens.
Justified only because it grants one narrow endpoint rather than the ability to mint
identities. If internal-only endpoints multiply, per-service keypairs become the better
answer.

> **That condition was met on 2026-08-21.** [D26](#d26--the-worker-authenticates-with-the-internal-key-never-a-forwarded-bearer) took the internal key from one endpoint to
> eleven, because the saga's worker has no user to forward. D32 brought it back down to
> **seven** by removing the Restaurant Service from the saga entirely. The reasoning
> above is unchanged and the conclusion it warned about is deferred, not resolved.

### D16 — Each authorisation decision lives in exactly one place

**Decided:** the Payment Service does not check who owns an order. It reads the order as the
caller, and the Order Service refuses.

**Why:** two services deciding the same question drift apart, and the one that drifts looser
is the vulnerability. Who owns an order is the Order Service's fact.

**Costs:** the refusal arrives as a `403` from a dependency and has to be mapped back
without being flattened into `502` — which is why D05 gained an explicit 401/403
pass-through rule.

### D17 — An unknown user id is `403`, not `404`

**Decided:** `GET /api/v1/users/{user_id}` runs the ownership check *before* the lookup, so
a stranger's id and a nonexistent id are indistinguishable from outside.

**Why:** the alternative turns the endpoint into an oracle for which user ids exist. The
same reasoning makes a failed login return one message — and take the same time — whether
the email is unknown or the password is wrong.

**Costs:** less helpful when debugging, and it reads as a bug until you know why. The smoke
test asserts it explicitly so a future "fix" trips a failing test.

### D18 — Role claims are re-checked over HTTP despite being in the token

**Decided:** `verify_owner` and `verify_customer` survived the auth work, even though the
token now carries a role.

**Why:** the role in a token was true when the token was signed. An account demoted since
then still presents a valid token until it expires, and only the HTTP lookup notices. The
apparent duplication is the difference between a claim and a current fact.

**Costs:** an extra network call on paths that appear to have the answer already, and code
that looks redundant to anyone who has not read this entry — hence the comments at the call
sites pointing here.

---

## Configuration and secrets

### D19 — Secrets live only in a gitignored `.env`

**Decided:** `.env` is the single source of truth for the four database passwords, the JWT
keypair and the internal key. `docker-compose.yml` names each secret exactly once, in a YAML
anchor merged into the containers that need it, and aborts via `${VAR:?message}` when one is
missing. Database and role names stay literal — they are not secrets.

**Why:** a password written twice is a password that will eventually be correct in one place
only. Failing the whole compose command beats starting the stack with an empty password or,
after auth, with no signing key.

**Costs:** a fresh clone does not run until `.env` exists, and regenerating it invalidates
every issued token.

### D20 — `init_bootstrap.sh` regenerates config byte-for-byte

**Decided:** the script emits `.env`, all four `init.sql` files, `docker-compose.yml` and
`nginx.conf` exactly as they exist in the repo.

**Why:** it is the reproducible path from empty directory to running stack, and it stays
trustworthy only while it matches.

**Costs:** **every change to those files must be mirrored into the script's heredocs**, or
the next bootstrap silently reverts it. The `.env` heredoc is quoted and cannot interpolate,
so generated values (the RSA keypair) are appended after it rather than written inside. This
is the easiest thing in the repo to forget.

### D21 — Blueprint deviations are deliberate and recorded

**Decided:** where [payments-service-migration.md](payments-service-migration.md) and
[db-per-service-guide.md](db-per-service-guide.md) conflict with what shipped, the
difference is written down in [../README.md](../README.md) rather than quietly absorbed.

The substantive ones: per-service ports instead of the blueprint's hardcoded 8000 (a literal
copy makes every route a `502`); `uuid_generate_v4()` over `gen_random_uuid()` for
consistency with the other three databases, dropping two indexes Postgres already provides
via `UNIQUE`; passwords interpolated from `.env` rather than inlined; schema mounts kept;
and `GET /api/v1/orders/{order_id}` added, because the blueprint hands the Payment Service
an `ORDER_SERVICE_URL` and no endpoint to call with it.

From the MongoDB migration (D22–D24): psycopg2 and the existing `PostgresPool` chassis
instead of the blueprint's SQLAlchemy async engines, which would have made the Menu and
Order Services the only two in the platform with a second way to reach a database; the
blueprint's redundant `idx_menus_restaurant_id` and speculative GIN index dropped; a `seq
BIGSERIAL` added to `order_tracking_logs` so `old_status` is derivable; and the blueprint's
claim that "API validation payloads remain untouched" honoured for the *wire* contract even
though its own DDL and Pydantic models changed the field names — the endpoint still takes
`status` / `service` / `raw_log`, and the mapping to `old_status` / `new_status` happens in
SQL.

**Why:** a blueprint followed exactly where it is wrong produces a broken system; one
departed from silently produces an unreviewable one. Recording the difference makes the
deviation itself the reviewable artifact.

**Costs:** the docs must be re-checked whenever a blueprint is revised.

---

## The MongoDB migration

Added 2026-08-21, implementing [postgres-menu-tracking-migration-v2.md](postgres-menu-tracking-migration-v2.md).
MongoDB held two unrelated things — a catalogue and an audit trail — for one reason: it was
the NoSQL box on the Week 1 stack diagram. Splitting them sent each to the service that owns
the fact it records, and left nothing for Mongo to hold.

### D22 — MongoDB was dropped; menus are JSONB in Postgres

**Decided:** the `menus` collection became a `menus` table in `sfo_menu_core`, one row per
restaurant, with the whole category/item/customization tree in a single `JSONB` column. The
Mongo container and the `motor` dependency are gone.

**Instead of:** keeping Mongo, or normalising the tree into `categories` / `items` /
`customization_groups` / `options` tables.

**Why:** the document shape was never the reason to run a second engine — Postgres stores
the same document in `JSONB` and reads it back the same way. What it adds is a credential
boundary the Menu Service was the last service to lack (D01), and an engine that enforces
"one live menu per restaurant" via `UNIQUE (restaurant_id)`, which turns publishing into a
single `ON CONFLICT` upsert instead of a read-then-write.

Not normalising is the other half of the decision. A menu is read whole, written whole, and
by exactly one key; four tables would buy joins nobody performs and cost a multi-statement
transaction on every publish. The relational shape is right for `order_tracking_logs` (D24)
and wrong here, and the difference is which reads the data actually gets.

**Costs:** no schema enforcement inside the tree — Pydantic remains the only thing checking
that a menu item has a price, exactly as under Mongo. Querying *inside* the tree ("which
restaurants serve a vegan main?") has no index behind it; the blueprint's GIN index was
deliberately not created, because until such a query exists it is a write cost on every
publish for a read nobody issues. One more container and one more password.

### D23 — Menus are read through a Redis cache-aside layer

**Decided:** `GET /api/v1/menus/{restaurant_id}` reads Redis first, falls back to Postgres,
populates the key with a one-hour TTL, and publishing deletes the key. Redis failures are
swallowed on the read path.

**Instead of:** no cache, or write-through on publish.

**Why:** this is the platform's hottest read — every checkout re-prices against it (D06) —
and one of its coldest writes. Cache-aside rather than write-through because a write-through
cache is a second place that must agree with the table; deleting the key leaves one, and the
next reader repopulates it from the row that was actually committed.

Swallowing read-path Redis errors is what keeps the cache from becoming a dependency: with
Redis down, menus still serve, just always from Postgres. That is why `cache_reachable:
false` in the health payload does not mean the service is degraded in the way
`database_reachable: false` does.

**Costs:** two real ones. A failed invalidation serves stale prices for up to the TTL — the
reason `MenuCache.invalidate` logs at *error* while the read path logs at *warning*, and the
reason the TTL exists at all. And a reader that misses can repopulate from a row that a
concurrent publish is about to replace, leaving a stale copy behind the write. Both are
bounded by an hour and neither is fixed here; a versioned key or a short lock is the answer
if menus ever change often enough for it to matter.

### D24 — The tracking trail moved into the order database, and stopped being best-effort

**Decided:** `order_tracking_logs` is an append-only table in `sfo_order_core`, one row per
transition, with a real `REFERENCES orders(id) ON DELETE CASCADE`. The Order Service writes
the opening `created` entry **in the same transaction as the order insert**.
`POST /api/v1/menus/logs` became `POST /api/v1/orders/logs`, still internal-key only, and
`GET /api/v1/orders/{order_id}/logs` was added so the trail is readable through the API
rather than only through `psql`. This supersedes D09.

**Instead of:** leaving it in Mongo, giving it a database of its own, or appending to a
JSONB array on `orders`.

**Why:** which state an order is in is the Order Service's fact, so the trail belongs where
the order does. Putting it there buys three things that were unavailable across a service
boundary:

* **The foreign key.** An entry against a nonexistent order is refused by the engine, and
  deleting an order takes its trail with it. Mongo could not express either.
* **The enum.** `new_status` is the same `order_status` type as `orders.status`, so there is
  one list of valid statuses and an invented one is a `422` from the database.
* **The transaction.** D09 argued audit logging had to be best-effort because the order was
  already committed when the log call fired — returning `500` would tell a client their
  order failed when it existed. That argument dissolves once both writes share a
  transaction: nothing is committed, so failing is safe and the client simply retries with
  the same idempotency key. An order without its first transition can no longer exist.

Append-only rather than a JSONB array on `orders`, because appending to a column rewrites
the whole order row under MVCC — a chatty delivery would rewrite the order once per GPS
ping, on the row the checkout path reads.

**Costs:** a `seq BIGSERIAL` had to be added that the blueprint did not have, because
`created_at` alone cannot order two entries written in one transaction and `old_status` has
to be derivable without a tie-break; the blueprint's `(order_id, created_at DESC)` index is
`(order_id, seq DESC)` here for the same reason. `POST /api/v1/menus/logs` is a breaking
change for anything that called it, and the response body changed with it —
`created_document` was a document-model artifact and is now `previous_status` plus the row's
`id`. The trail is no longer writable while the order database is down, which under D09 it
sort of was — the write simply vanished.

---

## The Week 2 saga

Added 2026-08-21, implementing
[week2-temporal-orchestration-blueprint.md](week2-temporal-orchestration-blueprint.md) —
whose first revision would have regressed six of the decisions above, and whose Section 0
records every departure from it.

Before this, an order was `created` and stayed there. Nothing advanced it, the customer paid
by calling the Payment Service themselves, `orders.rider_id` had never been written by any
code, and `restaurants.capacity` had never been read.

### D25 — Temporal orchestrates the order lifecycle, and the workflow id *is* the order id

**Decided:** `POST /api/v1/orders` keeps every step it had — idempotency, server-side
re-pricing, both HTTP verifications, the transactional insert of the order with the opening
entry of its trail — and then starts an `OrderWorkflow` whose id is `order-{order_id}`,
with `WorkflowIDConflictPolicy.USE_EXISTING`.

**Instead of:** a status-poller or a cron sweeping `created` orders, or a queue message.

**Why:** the lifecycle is a long-lived, failure-prone conversation with three other services
that has to survive process death, and that is exactly the thing a workflow engine is for.
The specific choice worth explaining is the *derived* id. Nothing records which workflow
belongs to which order, because the id is a pure function of the order — which makes
starting one idempotent for free, and makes the signal relay able to find a running saga
from nothing but the order id in the URL.

**Costs:** the start happens *after* the commit, and Temporal cannot enlist in a Postgres
transaction, so "the order exists" and "its saga started" are not one atomic fact. Given
the choice the order wins: it is what the customer was told about. A failed start leaves an
order sitting at `created`, logged at error, repaired by a retry with the same idempotency
key — the replay branch starts the saga too. This is D09's old argument resurfacing in a new
place, and it resolves the same way: a write that already succeeded must not be reported as
a failure.

Also: `create_order` became `async def`, so its repository calls now run on the event loop
rather than in FastAPI's threadpool.

### D26 — The worker authenticates with the internal key, never a forwarded bearer

**Decided:** activities call internal-key-guarded endpoints —
`POST /api/v1/payments/authorize`, `/payments/refund`, `/restaurants/tickets`,
`/riders/dispatch`, `/riders/release`, plus internal read paths on
`GET /api/v1/orders/{id}/internal` and `/restaurants/{id}/internal`.

**Instead of:** forwarding the customer's access token into the workflow, which is what
every other cross-service call in the platform does (D15).

**Why:** two independent reasons, either of which is decisive.

* **A workflow argument is durable history.** Anything passed to `start_workflow` is
  persisted by Temporal and rendered in its Web UI. Putting a bearer token there writes a
  live credential into a log.
* **Access tokens live 15 minutes.** A saga that waits on a kitchen and then searches for a
  rider routinely outlives that, and a workflow has no refresh path.

The ownership guarantee forwarding provided is not lost, only relocated: the saga did not
choose its order, it was started by an already-authorised `POST /api/v1/orders` whose
handler had established that the caller owns it.

**Costs:** **this is the moment D15's own caveat fires.** D15 justified a shared symmetric
secret on the grounds that it granted "one narrow endpoint rather than the ability to mint
identities", and noted that "if internal-only endpoints multiply, per-service keypairs
become the better answer". They multiplied — one to eleven — and then D32 took it back to
**seven** by deleting the saga's dependency on the Restaurant Service. The debt is
recorded rather than absorbed: per-service keypairs, or a signed service assertion, is
still the right answer, and the count is now moving in both directions rather than only up.

### D27 — Restaurant acceptance is a signal and a timer, not a synchronous call

> **Partly superseded by [D32](#d32--the-kitchen-queue-collapsed-into-the-orders-table) on 2026-08-21.** The signal-and-timer mechanism below is
> unchanged and still correct. What changed is *where the decision is recorded*: the
> `order_tickets` table this entry describes is gone, and the kitchen's answer is now a
> column on `orders`. Read the costs section with that in mind — the capacity leak it
> describes became structurally impossible rather than fixed.

**Decided:** the saga posts a ticket to `order_tickets` and then waits on
`workflow.wait_condition` with a 120-second timeout. A restaurant admin accepts or rejects
at their own pace, and the Restaurant Service relays the decision through
`POST /api/v1/orders/{id}/signals`. Rejection *and* silence both compensate.

**Instead of:** the blueprint's synchronous `POST` returning `{"accepted": bool}`.

**Why:** a real kitchen accepts when a human presses a button, which no HTTP response can
wait for. And the synchronous version had a concrete bug: the rejection was raised as a
plain exception inside a 3-attempt retry policy, so an order a restaurant had declined was
re-sent to them twice more before the saga gave up. Temporal retries every exception except
`ApplicationError(non_retryable=True)`, so the distinction between "the kitchen said no" and
"the kitchen's service is down" has to be made explicit — and it is now made once, in the
activity, rather than inferred from a status code at each call site.

Waiting on a timer rather than a connection is the other half. A saga parked in
`wait_condition` holds no thread, no connection and no memory in any service, and survives
a worker restart — which the resilience test asserts by restarting the worker while an
order sits there.

**Costs:** a decision can be lost in flight. The Restaurant Service commits the ticket
before relaying the signal and does not roll the decision back if the relay fails — the
kitchen should not see an error for something they did successfully.

That used to mean a lost acceptance eventually read as a refusal, which was this design's
one real hole. It is now closed by a **read-back on timeout**: when the wait for a decision
expires, the saga calls `read_ticket_activity` and asks the Restaurant Service what the
ticket actually says before concluding anything (`OrderWorkflow._recover_kitchen_decision`).
An `accepted` ticket resumes the saga; a `rejected` one compensates; only a ticket still
`pending`, already `expired`, or absent is treated as genuine silence.

The remaining exposure is narrower and deliberately biased: if the Restaurant Service cannot
be reached *at all* after the retry policy is exhausted, the saga treats that as no decision
and refunds. Refunding an accepted order is recoverable by a human; leaving a charged
customer waiting on a saga that will never finish is not. Both branches are asserted in
`scripts/saga-resilience-test.sh` §4, which simulates a lost relay by writing the decision
straight into `sfo_restaurant_core`.

The timeout also created a second-order leak that had to be closed with it: capacity is a
count of `pending` tickets, so a saga that gave up waiting left its ticket pending forever
and permanently consumed a slot in that kitchen's queue. Compensation therefore expires the
ticket (`POST /api/v1/restaurants/tickets/{order_id}/expire`, internal-key, `pending`-only
so it can never overwrite a real decision) — which is what the `expired` member of
`ticket_status` had been declared for since the table was created and nothing set.

### D28 — Riders got their own service and database

**Decided:** a Rider Service on 8006 with `sfo_rider_core`. The `riders` table moved out of
`sfo_user_core`, and `riders.user_id` lost its foreign key to `users`.

**Instead of:** adding rider endpoints to the User Service, which already owned the table —
or the blueprint's version, which put a *new* service on port 8004 (colliding with the Order
Service) and had it connect to `sfo_user_core` using the User Service's own credentials.

**Why:** that last part is a direct violation of D01, and D01 is the decision the whole data
layer rests on. Dispatch writes `is_available` and `current_order_id` on every assignment,
so somebody has to own those columns; a service that writes another service's tables makes
the credential boundary decorative. Keeping the table in `sfo_user_core` and putting the
endpoints on the User Service would have been legal, but it makes the identity service also
the logistics service, and the two have nothing to do with each other beyond a shared id.

**Costs:** the foreign key from `riders.user_id` to `users.id` was the price. It is now a
plain UUID verified over HTTP before the insert, with the same weakening D02 already
describes: enforced becomes checked. One more container, one more database, one more
password. And moving a table out of an initialised database means `docker compose down -v` —
`init.sql` only runs on an empty data directory.

### D29 — Dispatch prevents the race rather than detecting it

**Decided:** claiming a rider is one statement — `UPDATE riders SET … WHERE id = (SELECT …
ORDER BY haversine_km(…) LIMIT 1 FOR UPDATE SKIP LOCKED)` — preceded in the same
transaction by a check for a rider already carrying this order.

**Instead of:** the blueprint's read-all-riders-into-Python, sort, claim, and return `409`
when a concurrent workflow got there first.

**Why:** `SKIP LOCKED` makes the collision impossible rather than reportable. Two
simultaneous dispatches for different orders skip each other's locked row and each claim the
next-nearest rider, so both succeed on the first attempt; the blueprint's version made one
of them fail and re-run the whole search. Distance is computed in SQL — the platform's first
database function, `haversine_km`, plain `LANGUAGE sql` and `IMMUTABLE` — because it is
needed inside the `ORDER BY`, and computing it in Python is what forces the read-everything
approach that makes the row lock impossible to express.

The prior-claim check is not an optimisation, and this was confirmed rather than assumed: a
retried dispatch that skips it hits `duplicate key value violates unique constraint
"idx_riders_current_order"`, so the retry *fails* while the first rider stays held by a saga
that believes it has none.

**Costs:** the search is a sequential scan over a partial index, which is right for a fleet
this size and wrong for a large one — the `IMMUTABLE` marking is what keeps a functional or
PostGIS index available later. `haversine_km` is also the first function in any of these
schemas, so it is a new kind of thing to maintain.

### D30 — The saga owns payment authorisation, so `POST /api/v1/payments` now answers `409`

**Decided:** the workflow authorises payment through an internal endpoint, with an
idempotency key derived from the order id (`wf-pay-{order_id}`). The customer-facing
`POST /api/v1/payments` remains, but for an orchestrated order it now collides on
`UNIQUE (order_id)` and returns `409 "Order X has already been paid for"`.

**Instead of:** leaving payment client-driven and having the workflow wait for a signal
saying it happened.

**Why:** Week 2 asks for payment to be a compensatable step *inside* the transaction
boundary the saga controls. A client-initiated payment the workflow merely observes cannot
be refunded by the workflow without the workflow having authorised it, and leaves the order
stuck whenever a customer abandons checkout after the order is created.

**Costs:** a **behaviour change to a Week 1 contract**, which is why it is written down
rather than absorbed. The smoke test changed from *creating* a payment to *observing* the
one the saga made and asserting the `409` — deliberately, so a future change that re-enables
client-driven payment trips a failing test. The customer also no longer learns their payment
id from any response, which is why that assertion now reads it from the database.

This closes D10's "nothing sweeps them today": `POST /api/v1/payments/refund` resolves
`pending` rows as well as `authorized` ones, so a payment stranded by a failed gateway call
is finally reconciled by the compensation path.

### D31 — A status transition is a compare-and-set, and the enum supplies the ordering

**Decided:** `OrderRepository.transition` updates and appends the trail entry in one
transaction, guarded by `status <> new AND status NOT IN ('delivered','cancelled') AND (new
= 'cancelled' OR new > status)`. When the guard matches nothing, **no trail entry is
written**.

**Instead of:** the blueprint's unguarded `UPDATE orders SET status = :status`.

**Why:** Temporal guarantees activities run *at least* once. A worker that dies after
writing but before reporting will run the same activity again, so an unguarded update lets a
retry walk `delivered` back to `assigned`, and lets a five-times-retried activity write five
identical audit entries. Each clause answers one of those: the inequality makes a replay a
no-op, the terminal-state exclusion stops a late signal resurrecting a cancelled order, and
the last clause allows only forward movement except into `cancelled`.

The ordering comes from the schema rather than a lookup table, because a Postgres enum
compares by declaration order and `order_status` was declared in lifecycle order — so
`'delivered' > 'assigned'` is simply true. That is a dependency on how the type was written,
so it is worth knowing before anyone reorders it.

**Costs:** the guard is a real constraint on the state machine, not a safety net — adding a
status that is legitimately reachable backwards, or a second terminal state, means revisiting
this statement. And it leans on an enum's declaration order being stable, which is a
property no comment in `db/order/init.sql` previously depended on.

---

### D32 — The kitchen queue collapsed into the `orders` table

**Decided:** `order_tickets` and the `ticket_status` enum are gone. A kitchen's answer is
`orders.kitchen_decision` (a nullable `kitchen_decision` enum) plus `kitchen_decided_at`,
in the Order Service's own database. An admin reads their rail from
`GET /api/v1/orders/kitchen/{restaurant_id}` and answers on
`POST /api/v1/orders/{order_id}/accept|reject`, both on the **Order** Service, guarded by
`require_role("restaurant_admin")` plus an ownership check made over HTTP against the
Restaurant Service.

**Instead of:** keeping the ticket table, which is what D27 shipped and what the Week 2
blueprint's second revision specified.

**Why:** the table held a status, an items snapshot and a decision timestamp — and not one
of those was restaurant-domain data that `orders` did not already have. It was a second
database holding facts about an order's lifecycle, while every other fact about that
lifecycle, including two the *Rider* Service reports (`picked_up`, `delivered`), already
lived on `orders`. That asymmetry had no principled defence.

Collapsing it bought four things, in rough order of how much they matter:

* **The saga stopped calling the Restaurant Service at all** — from four calls (ticket,
  read-back, expiry, coordinates) to zero. `capacity`, `latitude` and `longitude` ride in
  the workflow payload, captured at checkout from a `verify_restaurant` lookup that was
  already happening and whose response was previously discarded. `order-worker` no longer
  even has `RESTAURANT_SERVICE_URL` in its environment.
* **An entire class of leak became unrepresentable.** The rail is defined as
  `status = 'confirmed' AND kitchen_decision IS NULL`, so cancelling an order removes it
  from the rail as a side effect. The `expire_ticket_activity` that D27 needed — and the
  capacity leak it was written to fix — are both simply gone.
* **The lost-signal recovery became a local read.** It was an HTTP call into another
  service, which forced a "we cannot tell, so assume the worst and refund" branch. Reading
  a column in the same database removes that failure mode.
* **Two fewer activities and one less internal endpoint surface.** Eight activities became
  six; the internal-key endpoint count fell from eleven to seven, which walks back some of
  the debt D15 and D26 flagged.

**Costs:** three real ones, and the first is the reason this was resisted for a while.

* **Read-path coupling.** A kitchen tablet polling its rail now reads `sfo_order_core`, the
  same database serving checkout and every saga transition. The partial index
  `idx_orders_kitchen_queue` matches the predicate exactly so the query stays cheap, but the
  *isolation* the separate database provided is gone. If kitchen polling ever becomes heavy,
  a read replica — not another table — is the answer.
* **A restaurant-facing surface on the Order Service.** It had no list endpoint at all
  before. The privacy exposure that could have come with it is closed deliberately:
  `KitchenOrderResponse` omits `total_amount`, `customer_id` and `idempotency_key`, so
  moving the queue did not widen what a restaurant can read.
* **Nowhere for real kitchen state to go.** Prep times, station routing, course sequencing
  and bump-bar state do not belong on `orders`. If any of that arrives, a
  restaurant-domain table comes back — but it will hold kitchen concepts rather than a
  duplicate of an order's lifecycle.

Two implementation notes worth carrying forward. `kitchen_decision` is deliberately **not**
a member of `order_status`: acceptance does not advance the lifecycle (an accepted order is
still `confirmed` until a rider is found), and adding a value would perturb the declaration
order that D31's compare-and-set depends on. And the capacity check lives *inside*
`OrderRepository.transition` rather than in a method of its own, because entering
`confirmed` **is** joining the rail — counting the rail and joining it must be one
transaction, or two orders can both take the last slot.

---

## Open questions

Not yet decided, and worth settling before the code forces an answer:

- **Key rotation.** Rotating the RSA keypair invalidates every live token simultaneously.
  There is no overlap mechanism (`kid` header, multiple accepted public keys) yet.
- **Where the private key lives outside a laptop.** `.env` is right locally and wrong for
  anything shared; a secrets manager or mounted key file is the Week 3 answer.
- **Whether `system_admin` should bypass ownership checks.** It currently does, everywhere,
  via `require_role` and `require_self_or_admin`. Convenient, and unaudited.
- ~~**Sweeping payments stranded at `pending`** (D10).~~ Settled by D30: the saga's refund
  endpoint resolves `pending` as well as `authorized`.
- ~~**Whether the audit trail stays best-effort** (D09).~~ Settled by D24 for the opening
  entry and by D31 for the saga's own transitions, which now commit with the status change
  they describe. The reported half is **partly** settled: a decision or delivery is
  committed locally and *then* relayed as a signal, and a failed relay is logged rather than
  retried — but the saga now reads the ticket back when its timeout fires, so a lost
  *kitchen decision* self-corrects (D27). **A lost rider pickup or delivery signal does
  not**, because there is no equivalent record to read back: the Rider Service's
  `current_order_id` says who is carrying the order, not how far along they are. An outbox
  table in each reporting service is the general answer; Week 3's Kafka work is the natural
  place for it.
- **Per-service credentials for internal calls** (D15, D26). The shared `INTERNAL_API_KEY`
  now unlocks **seven** endpoints across three services, including refunds — down from
  eleven after D32. D15 named this threshold in advance; it was crossed by accretion and
  has since been partly walked back, which is an argument for deciding it deliberately
  rather than letting the count drift with each change.
- **Whether the rider search window belongs in config or per-restaurant.**
  `RIDER_SEARCH_ATTEMPTS × RIDER_SEARCH_INTERVAL_SECONDS` is one platform-wide number, so a
  dense city centre and a rural outpost get the same 60 seconds before an order is refunded.
- **Nothing reclaims capacity from an *accepted* order that never completes.** The
  compensation path expires `pending` tickets (see D27's cost note), but a ticket the kitchen
  accepted before the saga failed stays `accepted` — correctly, since the kitchen did accept
  it — and `accepted` rows are excluded from the capacity count, so nothing leaks today.
  What is unresolved is that no state ever marks an accepted order *finished*: `capacity` is
  a count of pending tickets rather than of food actually being cooked, which is a thinner
  model of a kitchen than the name suggests.
