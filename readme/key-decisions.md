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
| [D22](#d22--mongodb-was-dropped-menus-are-jsonb-in-postgres) | MongoDB dropped; menus are JSONB in Postgres | 2026-08-21 | Partly superseded by [D50](#d50--menu-categories-and-order-items-move-from-jsonb-to-normalized-relational-tables) |
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
| [D33](#d33--the-order-services-kitchen-routes-now-honour-the-system_admin-bypass) | The Order Service's kitchen routes now honour the `system_admin` bypass | 2026-08-27 | Accepted |
| [D34](#d34--each-service-is-a-layered-python-package-not-a-flat-module-list) | Each service is a layered Python package, not a flat module list | 2026-08-27 | Accepted |
| [D35](#d35--every-response-is-an-envelope-status-body-message-errors) | Every response is an envelope: `{status, body, message, errors}` | 2026-08-27 | Accepted |
| [D36](#d36--the-order-sagas-workflow-and-worker-split-into-their-own-deployable) | The order saga's workflow and worker split into their own deployable | 2026-08-27 | Accepted |
| [D37](#d37--the-orchestrator-services-subdirectories-are-entity-scoped) | The Orchestrator Service's subdirectories are entity-scoped | 2026-08-27 | Accepted |
| [D38](#d38--kafka-carries-facts-temporal-still-owns-decisions) | Kafka carries facts, Temporal still owns decisions | 2026-09-08 | Accepted |
| [D39](#d39--a-transactional-outbox-not-a-post-commit-publish) | A transactional outbox, not a post-commit publish | 2026-09-08 | Accepted |
| [D40](#d40--one-topic-keyed-by-order_id-versioned-in-the-envelope) | One topic, keyed by `order_id`, versioned in the envelope | 2026-09-08 | Accepted |
| [D41](#d41--metrics-is-the-documented-exception-to-d35) | `/metrics` is the documented exception to D35 | 2026-09-08 | Accepted |
| [D42](#d42--observability-and-eventing-config-joins-d20s-byte-for-byte-set) | Observability and eventing config joins D20's byte-for-byte set | 2026-09-08 | Accepted |
| [D43](#d43--the-riders-report-becomes-a-durable-column-on-orders) | The rider's report becomes a durable column on `orders` | 2026-09-08 | Accepted |
| [D44](#d44--the-analytics-service-gets-its-own-database-and-dedups-by-consumer_group-event_id) | The Analytics Service gets its own database, dedups by `(consumer_group, event_id)` | 2026-09-08 | Accepted |
| [D45](#d45--kafka-is-the-ledger-rabbitmqcelery-is-the-concurrency-pool) | Kafka is the ledger, RabbitMQ/Celery is the concurrency pool | 2026-09-09 | Accepted |
| [D47](#d47--services-hold-their-own-temporal-client-and-name-workflows-by-string) | Services hold their own Temporal client and name workflows by string | 2026-09-09 | Accepted |
| [D48](#d48--multiple-roles-per-user-via-a-junction-table-not-an-implicit-everyone-is-a-customer-rule) | Multiple roles per user via a junction table | 2026-09-22 | Accepted |
| [D49](#d49--rider-live-location-moves-from-postgres-columns-to-a-redis-geo-index) | Rider live location moves from Postgres to a Redis GEO index | 2026-09-23 | Accepted |
| [D50](#d50--menu-categories-and-order-items-move-from-jsonb-to-normalized-relational-tables) | Menu categories and order items move from JSONB to normalized relational tables | 2026-09-23 | Accepted |

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

> **Partly superseded by [D50](#d50--menu-categories-and-order-items-move-from-jsonb-to-normalized-relational-tables)
> on 2026-09-23.** "Not normalising is the other half of the decision" below is reversed —
> the tree is now five relational tables. "MongoDB was dropped" is not: that half of this
> entry, and the reasoning for it, still stands unchanged.

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

### D33 — The Order Service's kitchen routes now honour the `system_admin` bypass

**Decided:** `order/clients/restaurant.py::verify_owner` now takes the caller's
`CurrentUser` and checks ownership through `common.auth.require_self_or_admin`, the same
helper every other ownership check in the platform already used. A `system_admin` calling
`GET /api/v1/orders/kitchen/{restaurant_id}`, `POST /{order_id}/accept` or
`POST /{order_id}/reject` for a restaurant they do not own now gets `200`/the decision,
not `403`.

**Instead of:** `verify_owner` comparing `restaurant["owner_id"]` to the caller's
`user_id` directly and raising `forbidden()` on any mismatch — a hand-rolled comparison
that never consulted `current_user.is_admin`.

**Why:** the platform's stated rule is that `system_admin` bypasses ownership checks
*everywhere* (see the Open Questions entry this settles, and D16's "one place" principle).
That was true of `require_role` and of `require_self_or_admin`'s other callers, and false
of exactly this one function — which made the two route guards on these endpoints disagree
with each other: `require_role("restaurant_admin")` admits an admin (D18's bypass), and
`verify_owner` then refused them behind it. A `system_admin` could pass the gate and be
turned away by the lock on the other side of it. This was found, not designed: a router
refactor moved these call sites without changing their behaviour, and reading them next to
`require_self_or_admin` made the divergence visible.

**Cost, and why it is one worth naming:** this is a privilege *expansion*, not a pure
refactor. Before this decision, no `system_admin` token could read a foreign restaurant's
kitchen queue or decide its orders; after, every one can, matching what the role has always
been able to do on every other ownership-gated endpoint in the platform. It shipped gated
on new coverage rather than on trust: `scripts/smoke-test.sh` had no `system_admin`
principal and no second `restaurant_admin` before this decision, so neither the bypass nor
the ownership check it sits beside was under test. Both were added first — a `system_admin`
reading a foreign kitchen queue and republishing a foreign menu (`200`), and a second,
non-owning `restaurant_admin` refused the same two calls (`403`) — and confirmed to fail
against the old code before the fix landed, and pass after.

---

### D34 — Each service is a layered Python package, every concern its own directory

**Decided:** every service under `services/` is now a real Python package
(`services/<svc>/__init__.py` exists, and its Dockerfile does
`COPY <svc>/ /app/<svc>/` + `CMD uvicorn <svc>.main:app`, not the flat `/app/` copy of
before). Inside, each service follows the same shape: `main.py` is a composition root that
builds the app and mounts routers; `deps.py` holds the process-lifetime singletons; and
four concerns are *always* a directory, never a bare `.py` file, regardless of how much
each one holds — `apis/` (routes, grouped by domain area or by caller audience),
`repositories/` (database access, one module per table or per shared SQL seam), `clients/`
(outbound calls, one module per sibling service called) and `schemas/` (request/response
models, one module per domain area). `restaurant/apis/restaurants.py` is a one-file package
holding exactly what `restaurant/api.py` held before; the file moved, nothing about it
changed. The Order Service additionally has `activities/`, wrapping `OrderActivities`
alone — see the note on it below.

**Instead of:** the flat layout D21 inherited from the Week 1/2 blueprint — one `main.py`
per service with every route, and every domain-area helper, declared inline on `app`, and
(for one revision of this decision) a middle state where a concern became a directory only
once it held three or more files, so `user/api.py` and `restaurant/repository.py` stayed
bare modules while `order/`'s equivalents were packages. `services/order/main.py` had
reached 485 lines across four unrelated areas (checkout, kitchen, saga-relay, tracking)
before either revision.

**Why:** the flat layout was the right shape for a service with three routes and wrong for
one with ten — that much motivated the first revision. The uniform-directory rule that
finished it is a readability argument, not a technical one: six services that agree
`services/<svc>/repositories/` is always where database access lives are six services a new
contributor can navigate identically, without first checking whether *this* service earned
a directory or stayed a file. The split by area still earned its keep on its own terms.
Splitting `order/clients.py` by which sibling each client called has one concrete, verified
payoff beyond readability: `order/worker.py` imports `order.activities`, which used to
import the whole of `clients.py` to reach the saga's two clients, pulling
`MenuServiceClient` and `RestaurantServiceClient` (and their `os.getenv` URL resolution)
into the worker process, which is never given `MENU_SERVICE_URL` or `RESTAURANT_SERVICE_URL`
(see D32's dependency-reduction). Splitting `clients.py` into one file per sibling made that
absence structural rather than merely true in code — confirmed by inspecting `sys.modules`
inside the running `order-worker` container, which loads `order.clients.payment` and
`order.clients.rider` and nothing else under `order.clients`, before and after the
directory-uniformity pass.

**Cost:** more files and more directories to navigate per service — a two-route service
now has an `apis/` folder holding one file — and four footguns specific to Temporal instead
of two, mitigated the same way each time: a docstring-only rule held everywhere rather than
an exception carved out where it happens to matter today. `order/__init__.py`,
`order/clients/__init__.py`, `order/repositories/__init__.py` and
`order/activities/__init__.py` are all imported by the workflow sandbox while resolving
`order.activities.activities` from inside `workflows.py`'s `imports_passed_through()`
block — the first because importing any submodule imports its parent package first, the
other three because they sit on the same import path. A re-export in any of them would drag
something the worker does not need back into the sandbox: `deps.py` and
`required("DATABASE_URL")` through the first, every client instead of two through the
second, nothing through the third and fourth today, but the rule is held on all four anyway
so it never has to be remembered selectively. None has ever held anything but a docstring.

`OrderActivities` was deliberately **not** split along the same lines, and this is the one
place "every concern is its own directory" does not mean "one file per thing this concern
does." Temporal records each `@activity.defn` method's name and the `OrderWorkflow` class
name in durable workflow history; splitting the class into per-domain classes would risk a
rename during the split, and any workflow started before such a deploy would fail to find
its registered activity — retried forever, the order stuck. `activities/` holds exactly one
module, `activities.py`, and that module holds exactly the one class it always did, sectioned
internally by domain (state, payment, kitchen, fleet). Wrapping it in a directory was for
naming consistency with `apis/`, `repositories/`, `clients/` and `schemas/` alone — moving
the module was safe, and splitting the class inside it was never attempted.

---

### D35 — Every response is an envelope: `{status, body, message, errors}`

**Decided:** every route in every service — success or failure — answers
`{"status": <int>, "body": <the result, or null>, "message": <str>, "errors": <list[str] |
null>}`. `common/responses.py::ok()` builds the success form; `install_error_handlers()`
registers three FastAPI exception handlers (`HTTPException`, `RequestValidationError`, and a
catch-all `Exception`) so every failure path — a deliberate rejection, a validation error, an
unhandled bug — answers through the same shape. `common/service_client.py::_payload` unwraps
`body` at the one place every cross-service call passes through, so no client above that
layer has to know the envelope exists. The gateway's own `/health` literal in
`api-gateway/nginx.conf` is enveloped too, so there is no second shape anywhere in the
platform's public surface.

**Instead of:** seven distinct top-level shapes with no shared base — a bare entity object, a
bare array, a hand-built `dict` with no schema, a bespoke result object, and `None`/`204` —
and, worse, two *structurally different* `422` bodies depending on which layer rejected the
request: `common/errors.py::unprocessable()` put a string under `detail`, and FastAPI's own
`RequestValidationError` handler put a list of objects under the same key. A client could not
parse a `422` without first guessing which kind it was looking at.

**Why:** three concrete defects, not a taste preference. First, the two-shaped `422` — closed
by giving `message` a string always and `errors` a list-or-null always, regardless of which
layer rejected the request. Second, an unhandled exception answered Starlette's default
plain-text `Internal Server Error` with **no JSON body at all** — a shape no client's
envelope-parsing code could even attempt to read — which is what the catch-all `Exception`
handler now prevents; auditing for this surfaced two live `None`-dereference bugs
(`order/apis/kitchen.py::_decide_kitchen`, `payment/apis/saga.py::refund_for_saga`) that had
been crashing with a bare `TypeError` instead of answering cleanly, both fixed in the same
pass. Third, `POST /api/v1/orders` declared `201` in its OpenAPI schema but answered `200` on
an idempotent replay with no `responses=` entry documenting it — an omission of a pattern
`payment/apis/payments.py` had already established (`REPLAY_RESPONSE`, now shared from
`common/responses.py`).

**Cost:** three 204 routes (`POST /users/logout`, `.../riders/me/orders/{id}/picked-up`,
`.../delivered`) became `200` with `body: null`, since a `204` has no body to carry the
envelope in. Health payloads dropped their inner free-text `status` field (six different
sentences, three different spellings, and the one key present on every probe was the one a
client could not parse) — the sentence now travels as the envelope's `message` instead, so
it is not lost, only relocated. And one inconsistency was left standing rather than
"fixed": `X-Idempotency-Key` is required on Order Service writes (missing → `422`) but
optional-then-checked on Payment Service writes (missing → `400`) — D08 records that
divergence as a deliberate Week-1 contract, not an oversight, so unifying it here would have
overridden an accepted decision rather than closed a genuine gap.

A defect found and fixed during the rollout, worth recording because it explains why
`common/service_client.py::_payload` logs a warning rather than raising when a response has
no `body` key: the first deploy of this decision enveloped three services
(restaurant, menu, user) and rebuilt the callers that read them (order, rider) without
updating `_payload` to unwrap the new shape. Every cross-service read of an enveloped
account or restaurant — `assert_account_role`, `verify_owner`, `verify_active` — silently
read the *outer* envelope's non-existent `role`/`owner_id`/`is_active` keys, and a rider
registering with a genuinely valid, correctly-signed token was refused with "role 'None'".
Caught by a full smoke run before any commit, not by a review — the fix is the unwrap plus a
same-service fallback: a 2xx body with no `body` key is treated as unenveloped rather than
raising, so a stack mid-rollout degrades to "reads the old shape" instead of every
cross-service call failing at once.

---

### D36 — The order saga's workflow and worker split into their own deployable

**Decided:** `services/orchestrator/` is a new service — its own image, its own two
containers (`orchestrator-service` for the API side, `orchestrator-worker` for the Temporal
worker) — holding the workflow (`workflows/order.py`), the activities
(`activities/order.py`), and the Temporal client the Order Service used to hold directly
(file paths as entity-scoped by D37, written the same day; the shape at first landing was
`workflows.py` and `activities/activities.py`, flat). Neither orchestrator
process has a database of its own. `transition_order_activity` and
`read_kitchen_decision_activity`, the two activities that used to write and read
`sfo_order_core` directly through a shared `OrderRepository`, now do both over HTTP: a new
internal route, `POST /api/v1/orders/{id}/transitions` (`order/apis/transitions.py`), and the
existing internal read, `GET /api/v1/orders/{id}/internal`. The Order Service's own
`saga.py` — which held a Temporal client to start workflows and relay signals — became
`clients/orchestrator.py`, an `HTTPX`-based client like every other sibling call it makes.
`common/service_client.py` gained `apost`/`passthrough` to support this: an async POST (real
callers this time, unlike the `apost` a prior revision deleted as dead code) and a mechanism
for a downstream status code — a full kitchen answering `409` — to cross the HTTP boundary as
something more specific than the blanket `502` "unexpected response" every other non-2xx
became.

**Instead of:** `order-worker` sharing the Order Service's image and, through it, its
database — the shape since D25. The two processes differed only in `command:`, and the
worker's direct `psycopg2` access to `sfo_order_core` was the one exception to "every
cross-service fact is reached over HTTP" anywhere in the platform.

**Why:** the review that asked for this named the goal directly — clear service boundaries —
and the boundary that mattered was the database. `order-worker` reading and writing
`sfo_order_core` outside any service's own request path was the one place a comment in
`worker.py` had to explain why an apparent violation of database-per-service was actually
fine ("this is the Order Service's own code"); after the split, it no longer needs to be
explained, because it is no longer true. The knock-on effect the audit called out
independently is now fixed too: `order/saga.py` used to import `order.workflows`, which
imported `order.activities.activities`, which loaded `order.clients.payment` and `.rider`
**inside the API process** — with neither `PAYMENT_SERVICE_URL` nor `RIDER_SERVICE_URL` set
there, silently falling back to the in-network defaults in `common/config.py`. Moving the
saga hand-off to an HTTP client removes the import edge entirely; `order-service` holds no
`temporalio` import at all now, confirmed the same way D34's equivalent claim was — reading
`sys.modules` inside the running container.

**Cost, named rather than hidden:**

* **A two-way HTTP dependency that did not exist before.** `order-service` calls
  `orchestrator-service` to start and signal a saga; `orchestrator-worker` calls
  `order-service` to record a transition and read the kitchen's decision. Neither call
  cycles back to itself and both are the kind of request that can retry, so this is judged
  acceptable coupling rather than a design smell — but it is coupling a single-image
  arrangement did not have.
* **`read_kitchen_decision_activity` reintroduces a failure mode D32 explicitly removed.**
  D32's dependency-reduction argument was that a *local* read of `orders.kitchen_decision`
  eliminated "the Order Service is unreachable so we cannot tell" as a possible answer.
  Reading it over HTTP now brings that failure mode back — mitigated, not eliminated, by the
  activity's `STATE` retry policy and by the same safe-default bias D32 already chose:
  a database (now a service) that will not answer is treated as no decision and the order is
  compensated, because refunding an accepted order is recoverable by a human and leaving a
  charged customer on a saga that never finishes is not.
* **`order_tracking_logs.service` needed a second constructor parameter.** The column used
  to be stamped from a *fixed* value baked into whichever `OrderRepository` instance handled
  the write (`"order-service"` or `"order-worker"`, chosen at process start). Once the write
  arrives over HTTP from a process this service does not construct the repository for,
  `OrderRepository.transition()` takes an optional per-call `service` override — the internal
  transition request carries `"orchestrator-worker"` explicitly, or the column would
  silently start recording `"order-service"` for every saga transition, an audit-trail
  regression no test in either suite would have caught (confirmed: neither script asserts
  this column).
* **~6 extra in-network HTTP hops per order** — two to start/signal the saga (down from
  zero HTTP, previously in-process gRPC calls to a local Temporal client) and one each for
  the two activities that used to touch Postgres directly. Not measured against a latency
  budget, because none exists yet for this platform; recorded as a cost to weigh if one is
  ever set.

**Durable-history safety, unchanged:** the workflow type (`OrderWorkflow`), all six
`@activity.defn` method names, the task queue (`"order-tasks"`), the workflow-id prefix
(`"order-"`), the three signal names, and the `stage` query are all identical to before the
move — only module *paths* changed, verified by running the full saga-resilience suite
(worker restart mid-saga, twice) against the new topology before this was considered done.
`OrderActivities` was not split, for the same reason D34 already gave it a package of its
own rather than one file per activity: a class split risks a rename, and a workflow started
before such a deploy would fail to find its registered activity, retried forever.

---

### D37 — The Orchestrator Service's subdirectories are entity-scoped

**Decided:** `services/orchestrator/apis/`, `schemas/`, `activities/`, `clients/` and
`workflows/` all hold one file (or, for `clients/`, one subdirectory) per *entity this
service orchestrates* — today that is only `order`, so every one of them holds exactly one
`order.py` (or `order/`). `workflows.py`, previously a flat module at the package root,
became a package — `workflows/order.py` — for the same reason `activities/activities.py`
became `activities/order.py`: a name that said "this is the code", not "this is the order
entity's code". `clients/` went one level deeper than the others, into
`clients/order/{order_service,payment,rider}.py`, because unlike `apis/`, `schemas/`,
`activities/` and `workflows/` — each already a single class or a handful of request
models with no real chance of collision — the three client classes are HTTP facades whose
every method is shaped around `order_id` specifically (`OrderServiceClient.transition`,
`SagaPaymentClient.authorize(order_id, amount, ...)`, `SagaRiderClient.dispatch(order_id,
...)`); a second entity calling the same sibling services would need different methods, not
a shared client, so entity-scoping here prevents a real collision rather than a
hypothetical one. `schemas/order.py`'s four models were renamed `OrderSaga*Request`/
`*Response` (from bare `Saga*`) to match the platform's own convention of naming a class for
its entity even when the file already does (`OrderCreateRequest` in `order/schemas/orders.py`,
`RiderRegisterRequest` in `rider/schemas/riders.py`).

**Instead of:** the flat shape D36 shipped hours earlier — `workflows.py`,
`activities/activities.py`, `clients/{order,payment,rider}.py`, `schemas/sagas.py`,
`apis/sagas.py` — which was correct for a service with exactly one workflow and would have
forced an awkward choice the moment a second one arrived: cram a second entity's activities
into `OrderActivities`'s file, or invent the entity-scoping this decision does anyway, but
later and around code already depended on elsewhere.

**Why:** raised directly — "so that in future if the orchestrator has to support other
entities then it can." The Order Service went through the equivalent shape change once
already (D34: `apis/` and `schemas/` split from a monolithic `main.py` into one file per
domain area), and the lesson transfers: the right time to draw the boundary is before a
second occupant needs it, not after, when every existing file's imports have to be
untangled from the one that used to be alone. Naming every file for its entity rather than
its role — `order.py` inside `activities/`, not `order_activities.py` inside a flat
`activities.py` — also matches how `apis/`, `schemas/`, `repositories/` already read
platform-wide: the directory says what kind of file this is, the filename says which thing
it's about.

**Cost:** two more docstring-only `__init__.py` files on the Temporal sandbox's import path
— `workflows/__init__.py` (new, because `workflows.py` becoming a package puts a parent
package on the path that did not exist before) and `clients/order/__init__.py` (new, because
entity-scoping `clients/` added a directory `activities/order.py`'s own imports now resolve
through). Both inherit the exact constraint `orchestrator/__init__.py`,
`orchestrator/activities/__init__.py` and `orchestrator/clients/__init__.py` already carried
— see `orchestrator/__init__.py`'s docstring for the full, now five-file list — so this is
one more instance of an existing rule, not a new kind of risk. Verified the same way every
sandbox-sensitive change in this codebase has been: draining in-flight workflows first,
then a full smoke and saga-resilience run against the restructured service before treating
this as done.

**What this does *not* do:** invent a route, a database, or a shared abstraction for a
second entity that does not exist yet. `apis/order.py`'s URLs (`/api/v1/orchestrator/sagas`,
`.../sagas/{id}/signals`) are unchanged — a second entity would need its own prefix, added
when it exists, not reserved in advance. (D47 deleted `apis/order.py` outright: services
name workflows by string on their own Temporal client, so a second entity needs no route at
all — only an entry in `orchestrator/registry.py`.) No `clients/shared/` was created for the case where
two entities might one day want the *same* payment or rider client: today's three clients
are order-shaped and duplicating a genuinely reusable client, if one turns out to be needed,
is a decision for when a second entity's requirements are known, not a guess made now.

---

## Week 3 — events and observability

Added 2026-09-08, implementing a corrected version of
[week3-event-driven-observability-blueprint.md](week3-event-driven-observability-blueprint.md).
The draft blueprint described a repository that does not exist (a nested `services/common/common/`,
an `orchestration/` package, six of seven services omitted) and several of its code samples would
have broken the platform outright if pasted verbatim — replacing `common/logging_config.py`
wholesale breaks all eight processes on import, and its example `create_order` handler drops the
connection pool and every existing route. None of it was implemented as written; this section
records what was built instead and why.

### D38 — Kafka carries facts, Temporal still owns decisions

**Decided:** every Kafka record describes a state change already committed in some service's
database, in the past tense, with no reply expected. Nothing on the order's critical path blocks
on Kafka, and no consumer writes back into `orders`. The Orchestrator Service holds no Kafka
client at all — it produces nothing.

**Instead of:** the blueprint's framing, which proposed moving to "an asynchronous event-driven
**choreographic** pipeline."

**Why:** this platform deliberately chose orchestration in Week 2 (D25), and
[saga-resilience-test.sh](../scripts/saga-resilience-test.sh) exists specifically to prove the
saga survives a mid-saga worker kill. A choreographic pipeline over Kafka would scatter
compensation logic across consumers with no durable execution, no timers, and no query surface —
it is a proposal to undo Week 2, not extend it. Keeping the orchestrator producer-free also removes
a real hazard by construction rather than by discipline: a `@workflow.defn` may not do I/O (it
replays deterministically and the sandbox blocks the imports), so an `aiokafka` call inside
`OrderWorkflow.run` would fail the workflow task, and Temporal retries workflow tasks forever —
every in-flight order would wedge permanently.

**Costs:** saga-level facts (compensation reason codes, timeout causes) only reach Kafka
indirectly, riding out through `orders.transition()`'s `metadata` argument on the next lifecycle
event rather than being emitted directly by the workflow. That is judged the right trade: the saga
stays observable through traces and Temporal's own metrics (port 9233), not through a second
channel that could disagree with what Temporal itself records.

### D39 — A transactional outbox, not a post-commit publish

**Decided:** `order_outbox` and `payment_outbox` tables, written inside the same
`cursor(commit=True)` block as the business write and the trail row. A background relay, composed
into each service's own lifespan, publishes unpublished rows to Kafka and marks them published.

**Instead of:** the blueprint's `await kafka_producer.send_event(...)` called after the database
commit, with the exception re-raised on failure.

**Why:** this is D09's argument again, verbatim. D09 said audit logging had to be best-effort
because the order was already committed when the log call fired — returning `500` would tell a
client their order failed when it existed. D24 closed that only by moving the write **into the
transaction**. The blueprint's post-commit publish reopens exactly the hole D24 closed: a Kafka
outage under the blueprint's code returns a spurious `500` for an order that was, in fact, created,
and the client's idempotent retry then returns a confusing `200`. The outbox makes Kafka's
availability irrelevant to whether checkout succeeds — proven by running the full smoke suite
twice, once with Kafka up and once with the broker stopped, both required to pass 185/185.

The outbox emission at `OrderRepository.transition()` is gated on the same `changed` flag the
trail-row write already uses, for the same reason: an activity retried five times by Temporal's
at-least-once guarantee must produce one event, not five.

**Costs:** two extra tables to maintain, a relay process inside two services, and an
at-least-once delivery contract — a crash between the broker ack and marking a row published
republishes it, so every consumer must dedup on `event_id` rather than assume single delivery.

### D40 — One topic, keyed by `order_id`, versioned in the envelope

**Decided:** a single topic, `sfo.order.events.v1`, three partitions, carrying both order and
payment events, keyed by `order_id`. The envelope is `{event_id, event_type, event_version,
aggregate_type, aggregate_id, seq, occurred_at, producer, data}`; `traceparent`/`tracestate` travel
as Kafka message headers, not envelope fields.

**Instead of:** the blueprint's one-topic-per-event-type layout (`order_placed`,
`order_cancelled`, `order_delivered`, …).

**Why:** Kafka orders records only within a single partition of a single topic. The blueprint's
analytics consumer depends on seeing `order_placed` before `order_delivered` for the same order,
and across two topics that ordering is not guaranteed — it is a design flaw, not a detail. A single
topic keyed by `order_id` is the only way per-order ordering holds for every consumer, present and
future.

**Costs:** every consumer filters event types it does not care about — a few lines, not a real
cost. A schema change that is not backward-compatible needs a new topic suffix (`v2`), produced
alongside `v1` during migration, rather than a per-event-type version bump.

### D41 — `/metrics` is the documented exception to D35

**Decided:** every service's `/metrics` route returns Prometheus's own text exposition format,
`include_in_schema=False`, bypassing both `install_error_handlers` and the `Envelope` wrapper. It
is not reachable through the gateway — nginx has no `/metrics` location, matching how the Temporal
Web UI is already handled.

**Why:** D35 made every response in the platform the same envelope, and that rule is right for
every API a client parses. Prometheus's exposition format is a machine-defined content type that
no envelope can wrap without breaking every scraper that exists. Rather than let that exception
happen silently the first time someone reaches for `Response(generate_latest())`, it is recorded
here: `/metrics` is an operator surface, in the same category as the Temporal UI, not an API route.

**Costs:** one documented inconsistency in a platform that otherwise has exactly one response
shape. Guarded by a smoke-test assertion that `/metrics` is not routable through the gateway, so
the exception cannot silently expand into a second public shape.

### D42 — Observability and eventing config joins D20's byte-for-byte set

**Decided:** `prometheus.yml`, `grafana/provisioning/**`, and the Kafka topic-init script are
generated by `scripts/init_bootstrap.sh` alongside the four artifacts D20 already covered, and
verified with the same scratch-directory diff.

**Why:** D20 already named `.env`, the four (now more) `init.sql` files, `docker-compose.yml` and
`nginx.conf` as "the easiest thing in the repo to forget." Week 3 adds three more generated
artifacts of the same kind; leaving them out of the mirroring discipline would make the bootstrap
script quietly stop being the reproducible path from empty directory to running stack that D20
exists to guarantee.

**Costs:** none beyond keeping the heredocs current, which the scratch-diff gate already forces
for every phase of this work.

### D43 — The rider's report becomes a durable column on `orders`

**Decided:** `orders.rider_reported_stage` (nullable `rider_report_stage` enum,
`'picked_up' | 'delivered'`, declared in that order for the same reason `order_status` is —
D31's guard compares with `>`) and `orders.rider_reported_at`.
`POST /api/v1/orders/{id}/signals` records the stage, guarded forward-only, **before**
relaying to the orchestrator — mirroring D27's rule for kitchen tickets exactly: commit the
fact, then relay it, and never roll the fact back if the relay fails. (That route is
`POST /api/v1/orders/{id}/rider-report` as of D47, and it no longer relays: the Rider Service
signals Temporal itself, immediately after this write. The ordering rule is unchanged, and is
the reason the write still crosses a service boundary at all.) A new
`read_rider_report_activity`, shaped identically to `read_kitchen_decision_activity`, lets
the saga read it back when a pickup or delivery wait times out, and resume instead of
compensating if the rider genuinely reported.

**Instead of:** leaving the gap the Open Questions section already named — a lost kitchen
decision self-corrects (D27), a lost rider pickup or delivery signal did not, because
`riders.current_order_id` says who is carrying an order, not how far along they are, and
there was no equivalent record anywhere to read back.

**Why:** this is D32's fix, applied to the other place it was needed, using a table that
already existed rather than a new one. D32 collapsed the kitchen queue onto
`orders.kitchen_decision` specifically so a timed-out wait could read back what actually
happened instead of assuming silence means refusal.
[services/rider/apis/delivery.py](../services/rider/apis/delivery.py) writes nothing
locally — both reporting routes are pure HTTP relays through the Order Service — so before
this decision a lost signal after `DELIVERY_TIMEOUT_SECONDS` (3600s) read as silence, and
the saga refunded a delivered order.

Two alternatives were rejected in favour of a column. Writing to `order_tracking_logs`
needs no new DDL, but `new_status` is `NOT NULL order_status` and `old_status` is derived
from the preceding row (D24) — a "reported" entry with no real status change is
indistinguishable from a genuine transition and corrupts the chain the very next real one
reads. Having the Order Service perform the `picked_up`/`delivered` transition directly,
rather than only recording that a report arrived, would make it a second orchestrator and
break D25/D31's single-writer rule — recording a fact and acting on it are kept separate on
purpose, exactly as `kitchen_decision` already keeps them separate from `status`.

**Costs:** adding an activity call to a live workflow is safe for workflows started after the
deploy, but a running workflow replayed against the new code hits a non-determinism error — the
same class of risk D37's cost section already names, and closed the same way, by draining
in-flight workflows before deploying. `saga-resilience-test.sh` gained a fifth section
(lost-rider-signal) mirroring its existing lost-kitchen-decision case exactly, writing the
report straight into `sfo_order_core` to simulate the lost relay.

### D44 — The Analytics Service gets its own database, and dedups by `(consumer_group, event_id)`

**Decided:** a fifth Postgres database, `sfo_analytics_core` (port 5438), holding exactly
two tables: `processed_events` (consumer-side dedup, keyed by `(consumer_group, event_id)`)
and `order_projections` (one row per order, updated incrementally as its events arrive). The
consumer commits the projection write and the dedup row in one transaction, then commits
the Kafka offset — never the other way around — and seeds its Prometheus Counters from
`order_projections` at startup via `Counter._value.set(...)`, so a restart does not silently
reset business totals to zero.

**Instead of:** two alternatives considered and rejected —

* **A stateless log-fold.** Assign partitions from offset 0 at startup, never commit
  offsets, hold counters and a seen-`event_id` set entirely in memory. Simplest possible,
  and it would have saved a container, a credential, and the D20 mirroring this decision
  now owes. Rejected because it bounds correctness to topic retention: once messages age
  out, a restart can no longer rebuild an accurate picture, and this platform sets no
  retention policy today that would make that bound meaningful.
* **Prometheus as the only store.** No database at all, counters living purely in
  `prometheus_client`. Rejected for the same reason D39 chose a durable outbox over a
  fire-and-forget publish: a Counter's value is process-local and resets to zero on every
  restart, and Prometheus cannot detect that reset the way it can a legitimate counter
  rollover — every restart would show as a cliff in `rate()`/`increase()`, not a gap.

**Why:** this service earns the same argument D01 already made for every other table in the
platform — a physical database boundary is enforced, a convention is not — applied here to
data that happens to be *derived* rather than authoritative. Nothing else ever reads or
writes `sfo_analytics_core`, and nothing in it is a fact any other service's request path
depends on: the whole database could be dropped and rebuilt by resetting the consumer
group's offset and replaying, which is the property that makes owning a database here cheap
rather than a second source of truth competing with the outbox tables in `sfo_order_core`
and `sfo_payment_core`.

The dedup key is `(consumer_group, event_id)`, not `event_id` alone, for the reason
`services/analytics/repositories/projections.py` states directly: this service runs one
logical consumer today, but a table shaped for exactly one group would need a schema change
the day a second one — a future GenAI read-model over the same topic, say — starts reading
independently.

**Costs:** a container, a credential, a port, and a `db/analytics/init.sql` that D20 now
requires stay mirrored into `init_bootstrap.sh` alongside everything else. And a genuinely
separate cost worth naming: Prometheus does not hot-reload `prometheus.yml` — adding this
service's scrape target required a manual `SIGHUP` to the running container, confirmed
necessary the first time this service was deployed. That is an operational step, not a
config one, and it is easy to forget the next time a target is added.

**Verified, not assumed:** deployed against a topic already carrying several thousand
duplicate deliveries — an artifact of the `uuid = text` cast bug D39's relay shipped with
and fixed (see below) — and the dedup collapsed them to the correct count of distinct
events on the first run, with the derived `order_projections` rows (status counts, average
delivery time) cross-checked against `order_tracking_logs` for the same orders and matching
exactly.

### A bug found and fixed while deploying D39's relay: array parameters need an explicit cast

**Found:** `OutboxRelay._mark_published`'s `UPDATE {table} SET published_at = ... WHERE id
= ANY(%(ids)s)` failed on every batch with `operator does not exist: uuid = text` —
psycopg2 adapts a Python list of strings to a `text[]` literal, and Postgres will not
implicitly cast a whole array parameter to compare against a `uuid` column, unlike a scalar
`%s::uuid`, which does cast implicitly. The relay's own retry loop meant this was not a
one-time failure: each iteration successfully published its claimed batch to Kafka via
`send_and_wait`, then failed at the very next step, so the same rows were reclaimed and
republished on every poll interval until the fix shipped.

**Fixed:** both `_MARK_PUBLISHED` and `_MARK_FAILED` now read `WHERE id = ANY(%(ids)s::uuid[])`.

**Why this is worth a paragraph rather than a silent fix:** nothing was lost. Every
duplicate publish carried the same `event_id`, so the at-least-once contract D39 designed
for — "a relay crash between broker-ack and marking a row published republishes it;
downstream dedups on `event_id`" — held exactly as intended under a real failure, not just
the one it was written to anticipate. The Analytics Service's dedup collapsing several
thousand redelivered messages back down to the correct count, on its very first run against
real data, is the closest this platform has come to a live test of that guarantee.

### D45 — Kafka is the ledger, RabbitMQ/Celery is the concurrency pool

**Decided:** `notification-consumer` and `notification-worker` — two containers built from
one `services/notification/` image. The consumer reads `sfo.order.events.v1` under its own
group (`"notification"`, independent of analytics' `"analytics"`), resolves the customer's
contact details over a new internal-key endpoint, and enqueues a Celery task — committing
its Kafka offset only *after* `send_task` returns. The worker is a plain Celery consumer of
those tasks, holding no Kafka client and no JWT keys, because it never imports
`common.auth` or touches the topic at all.

Also decided: `GET /api/v1/users/{user_id}/internal`, internal-key guarded, added because
this consumer has no bearer token to forward — the same gap `get_order_internally` (D26)
closed for the saga's activities, closed the same way for a Kafka consumer instead of a
Temporal one.

**Instead of:** the curriculum's literal ask read one way — Kafka **and** RabbitMQ as two
independent, competing durability layers — would have meant a lost RabbitMQ message and a
lost Kafka message were two different incidents needing two different recoveries. Making
Kafka's offset the actual commit point collapses that to one: a crash between enqueueing
and committing redelivers the same event and re-enqueues the same task, so recovery is
always "reset the notification consumer group and replay," never "hope RabbitMQ's own
persistence held."

**Why:** a duplicate SMS costs nothing like a duplicate charge does, so this consumer
deliberately does **not** carry the platform's usual at-least-once-with-dedup discipline
(no `processed_events` table here, unlike analytics' — see D44). `task_acks_late=True` +
`worker_prefetch_multiplier=1` make a worker crash mid-dispatch redeliver to another worker
rather than silently drop the task; `task_ignore_result=True` because nothing ever calls
`.get()` on a fire-and-forget dispatch, so no result backend is configured at all.

Only three event types are notification-worthy — `order.confirmed`, `order.delivered`,
`order.cancelled` — a smaller set than analytics tracks, and none of the other four
(`order.created`, `order.assigned`, `order.kitchen.decided`, `payment.*`) go through schema
validation at all: this consumer never touches the data of an event it will not act on.

**Costs:** a genuinely hardcoded-secret bug caught before it shipped — an early draft of
`notification/worker.py` defaulted the whole broker URL to
`amqp://guest:guest@rabbitmq:5672//`, exactly the mistake this platform's own review of the
Week 3 blueprint had already flagged in a different file. Fixed by requiring
`RABBITMQ_USER`/`RABBITMQ_PASSWORD` separately (D19) and composing the URL from them; only
the credential-free hostname may default. And a second stateful broker in the observability
week, whose durability this design deliberately does not lean on — see the "instead of"
above for why that is the point, not an oversight.

---

### D47 — Services hold their own Temporal client and name workflows by string

**Decided:** the services that observe a saga fact talk to Temporal directly, through a
shared `SagaClient` in [services/common/temporal.py](../services/common/temporal.py), instead
of posting to the Orchestrator Service. `order-service` starts `OrderWorkflow` and signals
`restaurant_decision`; `rider-service` signals `rider_pickup` and `rider_delivery`. The
Orchestrator Service keeps its container but loses its two saga routes — `apis/order.py` and
`schemas/order.py` are deleted, leaving `apis/health.py` and the `/metrics` route
`instrument_app` mounts. What the worker runs moves out of `worker.py` into a new
[orchestrator/registry.py](../services/orchestrator/registry.py), and
`order/apis/signals.py` becomes `apis/rider_reports.py`, keeping the durable write and
dropping the relay.

**The mechanism that makes it safe is naming the workflow by string.** `SagaClient.start`
takes `"OrderWorkflow"`, not `OrderWorkflow.run`. That is the whole difference from what D36
found unworkable: a class reference means importing `orchestrator.workflows.order`, which
imports `activities/order.py`, which loads the payment and rider clients into a process where
neither `PAYMENT_SERVICE_URL` nor `RIDER_SERVICE_URL` is set. A string imports nothing. So no
service outside `services/orchestrator/` may import a workflow or activity module, and the
contract between them is `SagaClient` plus the task-queue and signal-name constants in
`common/config.py`. Verified the way D34 and D36 verified their equivalent claims — reading
`sys.modules` inside the running containers, which lists no `orchestrator.*` module in either
`order-service` or `rider-service`.

**Instead of:** D36's HTTP facade, whose two route bodies were `client.start_workflow(...)`
and `handle.signal(...)` and nothing else. Also instead of moving the seven activities into
the services that own their data — that would dissolve D36's whole cost list, but it makes
the orchestrator a workflow-only host rather than the place workflows live and run, and it
needs a drain. Recorded here as considered and declined, not overlooked.

**Why:** the facade was a workaround for an import problem, and the import problem has a
cheaper fix. Holding a Temporal client is how Temporal is designed to be used — the client is
a stateless gRPC connection, not a heavyweight resource, and one per service that needs it is
the ordinary shape. The facade also taxed every future workflow: per D37, a second entity
needed `apis/<entity>.py` and `schemas/<entity>.py` purely to forward two calls. With the
registry, a new entity costs `workflows/<entity>.py`, `activities/<entity>.py`, a task-queue
constant and one registry line — no HTTP surface at all.

**What deliberately did not change:** the workflow type name, all seven `@activity.defn`
names, the task queue (`order-tasks`), the workflow-id prefix (`order-`), the three signal
names and the `stage` query are identical, so durable history stays compatible and **no
workflow drain was needed** — unlike D36's own deploy. Activities still execute in
`orchestrator-worker` over HTTP. `orders.rider_reported_stage` stays on `orders`: an order's
state is the Order Service's to hold (D01), and it is what `read_rider_report_activity` reads
back on a timeout, so `rider-service` keeps one HTTP call to record it and then signals the
saga itself. Recording into `sfo_rider_core` instead was considered and rejected — the stage
would survive onto the rider's *next* order unless `_CLAIM_NEAREST` cleared it, a silent
"already delivered" fault an hour later.

**Cost, named rather than hidden:**

* **The `require_internal` gate and the Pydantic validation in front of the saga are gone.**
  `apis/order.py` authenticated every start and signal on the internal key and validated the
  payload through `OrderSagaStartRequest`. Nothing replaces either: authorisation now rests on
  network isolation — anything that can reach `temporal-server:7233` can start or signal any
  workflow. That is the posture `docker-compose.yml` already documents for Kafka, RabbitMQ,
  Jaeger and the Temporal UI, and it is fine on a laptop, but it is a real reduction and the
  honest fix is a Temporal namespace with mTLS or an API key, which this does not do.
* **`amount` as a string now rests on a comment.** It has to survive JSON into workflow
  history exactly (D07); `OrderSagaStartRequest.amount: str` used to enforce that at the
  boundary, and the only thing enforcing it now is `str(order["total_amount"])` in
  `order/clients/orchestrator.py`.
* **Three requirements files pin `temporalio`** where one did before, so a version skew
  between a client and the worker whose workflows it starts is newly possible — with no local
  symptom, since it surfaces as a wire incompatibility.
* **The rider path is two calls where it was one**, and their order is load-bearing: record,
  then signal. Reversed, a signal that lands while the record fails leaves a timeout with
  nothing to read back — the gap D43 closed.

**Citation drift found while doing this, not introduced by it:** twelve places across
`services/`, `scripts/` and `readme/diagrams/erd.md` cite **D46** for the rider's durable
report. There is no D46; the record is
[D43](#d43--the-riders-report-becomes-a-durable-column-on-orders). Code written for this
decision cites D43 and says so inline; the pre-existing citations are left alone rather than
swept into an unrelated change.

---

## Week 4 — multi-role access control

### D48 — Multiple roles per user via a junction table, not an implicit "everyone is a customer" rule

**Decided:** `users.role_id` (a single `NOT NULL` foreign key — one role per user) is replaced
by `user_roles`, a `(user_id, role_id)` junction table (`db/user/init.sql`,
`scripts/init_bootstrap.sh`). The access token's `role` claim becomes `roles`, a list; every
guard in `services/common/auth.py` that compared against one value now checks membership —
`require_role` is a set intersection, and `assert_account_role` is renamed
`assert_account_has_role` to make the plural-aware check explicit rather than a silent
behavior change under the old name. Two new endpoints, `POST`/`DELETE
/api/v1/users/{id}/roles`, let an account grant or revoke a role on itself (or, for an admin,
on anyone) — self-service for `customer`/`restaurant_admin`/`rider`, but touching
`system_admin` on any account, including the caller's own, additionally requires the caller
to already be a `system_admin`. Full rationale, the RBAC decision table across every route,
and the worked scenario live in
[multi-role-rbac-design.md](multi-role-rbac-design.md); this entry is the changelog-style
record of what shipped.

**Instead of:**

- **Implicit "everyone is a customer,"** i.e. dropping the role gate on checkout entirely
  rather than requiring an explicit grant. Rejected because it doesn't generalise — it patches
  exactly the one scenario named (a restaurant_admin ordering for himself) and nothing else,
  where the junction table lets *any* two roles combine.
- **A role hierarchy** (`restaurant_admin` implying `customer`). Roles stay a flat set;
  `system_admin` is a distinct label that bypasses every gate it meets, not "every other role
  combined." A hierarchy is a bigger, separate decision not needed to solve this.
- **A versioned or dual-format JWT** for a gradual, mixed-version rollout. Rejected because
  there is no live traffic to protect here — a clean, one-time cutover of the claim shape
  costs nothing this environment can't absorb, where the dual-format version would be the
  correct call in production.
- **A second policy table naming which roles require elevation to grant.** A single `role ==
  "system_admin"` check in the two new route handlers covers the entire requirement; a
  separate schema concept for this one exception would be unused machinery.

**Why:** `services/common/auth.py` is copied into every service image at build time (D04), so
this is a breaking change to a contract seven services share — there is no way to make the
claim shape multi-role-aware for some services and not others. Accepting one atomic cutover
(named explicitly rather than glossed over) was cheaper than building the machinery a gradual
rollout would need, given nothing is currently depending on that gradualness.

**Cost:**

- **Every access token issued before the cutover is invalidated immediately** — `get_current_user`
  now requires `roles` in the claims, which no pre-cutover token carries. Short-lived access
  tokens make this a minor blip (a forced `/refresh` or `/login`), not a production incident,
  precisely because there is no production traffic yet.
- **All seven services rebuild and redeploy together.** No canary or rolling path exists
  without a versioned claim (rejected above), so this is a maintenance-window change.
- **`verify_customer` (`services/order/clients/user.py`) still only checks that the customer
  id exists — it was never converted to `assert_account_has_role`, unlike `verify_owner` and
  `verify_rider`.** This asymmetry predates this decision and is left as a known gap: a user
  who is demoted out of `customer` mid-session could still place one more order before their
  token expires or refreshes. Named here rather than silently carried forward.
- A temporary `CurrentUser.role` compatibility property (returns the first granted role)
  ships alongside `.roles` so a not-yet-audited call site fails soft rather than crashing.
  Tracked for deletion once `grep -rn '\.role\b' services/` (excluding `.roles`) comes back
  empty — not meant to be a permanent second name for the same thing.

### D49 — Rider live location moves from Postgres columns to a Redis GEO index

**Decided:** `riders.current_latitude`/`current_longitude` (`DECIMAL(9,6)`) are replaced by
one Redis GEO sorted set, `riders:geo` (logical database 2), member = `user_id`, written
through a new `RiderGeoStore` (`services/rider/repositories/geo.py`). `is_available`/
`current_order_id` and the `FOR UPDATE SKIP LOCKED` claim stay in Postgres unchanged.
Dispatch becomes two steps: `GEOSEARCH ... BYRADIUS ... ASC COUNT 50 WITHDIST` for a
distance-ordered candidate list (`RIDER_DISPATCH_CANDIDATE_CAP`), then a Postgres claim
restricted to `user_id = ANY(candidates)`, ordered by `array_position` to reproduce Redis's
distance order without recomputing distance in SQL. `haversine_km` and
`idx_riders_dispatchable` are dropped; `idx_riders_current_order` is untouched. Full
rationale and the exact request/response flow live in
[rider-location-redis-design.md](rider-location-redis-design.md); this entry is the
changelog-style record of what shipped.

**Instead of:**

- **All-Postgres, as before.** Rejected per the motivation that started this: in production,
  thousands of riders pinging every few seconds is write-hot, ephemeral, loseable-on-restart
  data that causes MVCC bloat/vacuum pressure with no transactional need, competing with the
  claim query's own lock on the same table.
- **All-Redis, including the availability claim.** Rejected: only Postgres's
  `FOR UPDATE SKIP LOCKED` plus the unique partial index on `current_order_id` gives the
  at-most-one-rider-per-order guarantee a retried Temporal dispatch activity depends on
  (D29); Redis has no equivalent primitive without reimplementing it.
- **A Lua-scripted atomic claim inside Redis**, mirroring `is_available` into Redis as a
  second copy. Rejected: it would duplicate state Postgres already owns authoritatively,
  reopening the two-writers-for-one-fact problem D01 exists to prevent.

**Why:** removes the actual bottleneck (per-ping Postgres writes) while keeping the one
place that already needs a serialisable, uniqueness-enforced claim exactly where it already
works — a minimal, targeted change rather than a rearchitecture of dispatch.

**Cost:**

- **A bounded, nameable approximation in "nearest available."** The `COUNT 50` cap on the
  Redis candidate list can return zero available riders even when one exists further out, if
  50+ strictly-closer riders are all busy — today's uncapped Postgres scan could never miss
  an eligible rider. Accepted as a bounded, tunable approximation rather than an unbounded
  one; not a race-window artifact, a structural cap.
- **No per-entry staleness/TTL on the geo set** — unchanged from before, where a reported
  position was also trusted indefinitely until overwritten. Not a new gap.
- **Redis becomes a hard, blocking dependency for dispatch and location reporting
  specifically** — a `503`, not a graceful degradation, unlike the Menu Service's
  cache-aside (falls back to Postgres) or the User Service's refresh store (forces
  re-login). This extends an already-operated dependency's blast radius into a service that
  previously had none on Redis at all, rather than adding new infrastructure to the stack.
- **The migration is not pure SQL** (cross-system) and needs
  `scripts/migrate_rider_locations_to_redis.sh` plus manual, epsilon-tolerant verification
  before `db/rider/drop_rider_location_columns.sql` is safe to run.

### D50 — Menu categories and order items move from JSONB to normalized relational tables

**Decided:** `menus.categories` becomes four tables — `menu_categories`, `menu_items`,
`menu_item_customization_groups`, `menu_item_customization_options` — and `orders.items`
becomes two — `order_line_items`, `order_line_item_options`. Both API contracts
(`MenuResponse`, `OrderResponse`, `KitchenOrderResponse`) are unchanged: the nested shape a
caller sees is reassembled from the relational rows on every read, in
`MenuRepository._load_categories` and `OrderRepository._attach_items`. A `position` column
at every level (independent of the client-supplied `display_order` field on categories)
round-trips whatever order a tree was submitted in. Publishing a menu stays a full-tree
replace — delete every category for that menu (cascading through items/groups/options) and
reinsert — inside one transaction, same as the single `ON CONFLICT` upsert it replaced.
Checkout stays one transaction too: the order row, its line items, and their chosen options
all commit together, the same guarantee `items` as a single JSONB column already gave.

**Instead of:** leaving both as JSONB, which is what
[D22](#d22--mongodb-was-dropped-menus-are-jsonb-in-postgres) already decided for the menu
side, on grounds this entry does not dispute — see below.

**Why:** reviewer-driven, not usage-driven. Two Explore passes over the codebase before this
shipped confirmed neither column was ever partially updated or queried inside via SQL
anywhere — every read and write was whole-document, exactly what D22 predicted. What
normalizing buys instead: database-enforced integrity (an item can't reference a
nonexistent category, `base_price`/`extra_price` can't go negative — previously only
Pydantic checked this) and future per-item queryability — sales-by-item, catalogue search —
without parsing JSON to get it. That is a real trade, not a correction of D22; D22's own
reasoning ("a menu is read whole, written whole, by exactly one key") is still true today,
and is the reason the read path costs more now than it argued for.

**Cost:**

- **A multi-statement transaction on every menu publish and every checkout**, in place of
  one `ON CONFLICT` upsert or one `INSERT ... Json(items)`. Checkout inserts one row per
  line item plus one per chosen option; a menu publish deletes and reinserts the whole tree.
  Neither is a hot-write path, so this is paid rarely, not per-request.
- **A join (or several grouped queries) to reconstruct the same nested response on every
  read**, where one column read used to suffice. On the menu side this is largely absorbed
  by the Redis cache-aside layer ([D23](#d23--menus-are-read-through-a-redis-cache-aside-layer)):
  only a cache miss pays the reconstruction cost, which is the main reason this plan is
  cheaper in practice than D22's cost estimate implied. The order side has no such cache and
  pays it on every read, though an order's own line-item count is small.
- **Any future change to either tree's shape now needs a schema migration**, where a
  Pydantic model change alone used to do it. This is the most durable cost, and the
  strongest argument D22 already made against doing this — it does not go away, it is
  accepted.
- **The migration is pure SQL, not cross-system** — unlike
  [D49](#d49--rider-live-location-moves-from-postgres-columns-to-a-redis-geo-index), moving
  Postgres JSONB into Postgres tables needs no bash orchestration, just
  `db/menu/migrate_categories_to_relational.sql` and
  `db/order/migrate_items_to_relational.sql`, each followed by a gated, manually-run
  `drop_*_column.sql` once row counts are verified.

---

## Open questions

Not yet decided, and worth settling before the code forces an answer:

- **Key rotation.** Rotating the RSA keypair invalidates every live token simultaneously.
  There is no overlap mechanism (`kid` header, multiple accepted public keys) yet.
- **Where the private key lives outside a laptop.** `.env` is right locally and wrong for
  anything shared; a secrets manager or mounted key file was flagged as "the Week 3 answer"
  when this question was written, but Week 3 did not touch it — the eventing and observability
  work took the available time. Still open, explicitly deferred rather than silently dropped.
- ~~**Whether `system_admin` should bypass ownership checks.**~~ Settled by
  [D33](#d33--the-order-services-kitchen-routes-now-honour-the-system_admin-bypass): it
  does, everywhere, via `require_role` and `require_self_or_admin` — the Order Service's
  kitchen routes were the one exception, and no longer are.
- ~~**Sweeping payments stranded at `pending`** (D10).~~ Settled by D30: the saga's refund
  endpoint resolves `pending` as well as `authorized`.
- ~~**Whether the audit trail stays best-effort** (D09).~~ Settled by D24 for the opening
  entry and by D31 for the saga's own transitions, which now commit with the status change
  they describe. The reported half was **partly** settled: a decision or delivery is
  committed locally and *then* relayed as a signal, and a failed relay is logged rather than
  retried — but the saga read the ticket back when its timeout fired, so a lost
  *kitchen decision* self-corrected (D27), while **a lost rider pickup or delivery signal did
  not**, because there was no equivalent record to read back. Closed by
  [D43](#d43--the-riders-report-becomes-a-durable-column-on-orders): the rider's report is
  now a durable column on `orders`, read back on the same two timeout branches the kitchen
  case already used. Note this was closed with a column, not the outbox — the outbox (D39)
  answers a different question (getting a fact to Kafka), not this one (having a fact to read
  back before compensating).
- **Per-service credentials for internal calls** (D15, D26). The shared `INTERNAL_API_KEY`
  unlocked seven endpoints across three services after D32, including refunds, and Week 3
  added an eighth — `GET /api/v1/users/{user_id}/internal` (D45), the Notification
  Service's only way to resolve a customer's contact details with no bearer token to
  forward. D15 named this threshold in advance; it keeps being crossed by accretion, which
  is an argument for deciding it deliberately rather than letting the count drift with each
  change.
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
