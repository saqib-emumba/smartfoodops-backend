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

## Open questions

Not yet decided, and worth settling before the code forces an answer:

- **Key rotation.** Rotating the RSA keypair invalidates every live token simultaneously.
  There is no overlap mechanism (`kid` header, multiple accepted public keys) yet.
- **Where the private key lives outside a laptop.** `.env` is right locally and wrong for
  anything shared; a secrets manager or mounted key file is the Week 3 answer.
- **Whether `system_admin` should bypass ownership checks.** It currently does, everywhere,
  via `require_role` and `require_self_or_admin`. Convenient, and unaudited.
- **Sweeping payments stranded at `pending`** (D10). Week 2's compensation workflow is the
  intended answer; nothing does it today.
- ~~**Whether the audit trail stays best-effort** (D09).~~ Settled by D24: the opening
  entry now commits with the order. Transitions reported by *other* services still arrive
  over HTTP and can still be lost in flight — that half is open.
