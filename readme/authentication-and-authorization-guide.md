# SmartFoodOps — How Authentication & Authorization Actually Work

This document explains **the code as it exists today** — `services/common/auth.py`,
`services/user/tokens.py`, `services/user/main.py`, and how every other service applies the
dependencies `auth.py` exports. It is the "how it runs" companion to
[key-decisions.md](key-decisions.md) (D11–D18 cover the "why"); read that when the question
is "why not the other way?".

---

## 1. The shape of it

Two credentials exist in this platform, and they are never interchangeable:

```
 END-USER IDENTITY                          SERVICE-TO-SERVICE IDENTITY
 ──────────────────                         ───────────────────────────
 an RS256 access token                       a shared secret, X-Internal-Key
 minted by the User Service only             known to every service
 says "I am user X with role Y"              says "this call came from a sibling,
 forwarded unchanged between services         not an end user"
 when one service calls another
```

A request never carries both, and no endpoint accepts either in place of the other.
`require_role(...)` and `require_internal` are mutually exclusive guards on any given route.

```
                          ┌───────────────────────┐
    Authorization: Bearer │    Nginx Gateway       │
   ─────────────────────▶ │  (no auth logic here — │
                          │   pure path routing)   │
                          └────────────┬───────────┘
                                        │
              ┌─────────────┬──────────┴─────────┬─────────────┬────────────┐
              ▼             ▼                    ▼             ▼            ▼
          user-svc     restaurant-svc         order-svc    payment-svc   rider-svc
          :8001            :8002                :8004        :8005        :8006
        (sole issuer)  each verifies the token itself — nginx never does auth
              │
              ▼
        JWT_PRIVATE_KEY_B64   (only this container has it)
              │
   every other container holds only JWT_PUBLIC_KEY_B64 — can verify, cannot mint
```

Tokens are verified **inside each service**, not at the gateway. `nginx.conf` has zero
`auth_request` directives and zero JWT logic — it is a pure reverse proxy. That means there
is no chokepoint that fails closed: every new route must explicitly apply a dependency, and
`scripts/smoke-test.sh` exists partly to catch a route that forgot to.

---

## 2. Where a token comes from

### 2.1 Registration

```
POST /api/v1/users/register
   │
   ├─ payload.role is a plain string, not an enum — an unknown name becomes a
   │  400 from a DB lookup ("valid roles: [...]"), not a 422 from schema validation
   ▼
UserRepository._resolve_role(cur, role_name)
   │  SELECT id, name FROM roles WHERE name = %s
   ▼
hash_password(plaintext)  →  bcrypt.hashpw(..., bcrypt.gensalt())
   │  a fresh random salt every call; the plaintext is never stored or logged
   ▼
INSERT INTO users (email, password_hash, full_name, phone, role_id)
   │
   ▼
201 UserResponse   (id, email, full_name, phone, role — never the hash)
```

No token is issued at registration. A client must log in afterward, exactly like any other
account.

### 2.2 Login — and the two things that stop it leaking information

```
POST /api/v1/users/login  {email, password}
   │
   ▼
users.find_credentials(email)   → id, password_hash, role  (the ONLY query that
   │                                reads password_hash — kept out of every other
   │                                query so it can never reach a response by accident)
   │
   ├─ no account found?  use _ABSENT_ACCOUNT_HASH instead of skipping bcrypt
   │
   ▼
bcrypt.checkpw(password, stored_hash)   ← runs unconditionally, real account or not
   │
   ├─ mismatch (wrong password OR no such account) ──▶ 401 "Invalid email or password"
   │                                                     (byte-identical either way)
   ▼
_issue_session(user_id, role)
   ├─ access_token  = issue_access_token(user_id, role)     RS256, 15 min
   └─ refresh_token = generate_refresh_token()              opaque, stored in Redis, 7 days
```

Two deliberate anti-enumeration details, both in `user/main.py`:

- **`_ABSENT_ACCOUNT_HASH`** is a real bcrypt hash of a fixed dummy string, computed once at
  import. When no account matches, `verify_password` still runs against it — so a login
  attempt costs the same bcrypt round whether or not the email exists. Skipping the hash
  check for an unknown email would make response *time* answer the question the shared
  error message already refuses to answer.
- **One message, always.** `"Invalid email or password"` covers a wrong password and a
  nonexistent account identically. `scripts/smoke-test.sh` asserts the two responses are
  byte-for-byte equal, so a future change that makes them diverge trips a failing test.

### 2.3 What's actually inside the token

```
issue_access_token(user_id, role)
   {
     "sub": "<user_id>",
     "role": "<role>",
     "iss": "smartfoodops-user-service",
     "iat": <now>,
     "exp": <now + 15 minutes>
   }
   signed RS256 with JWT_PRIVATE_KEY_B64, decoded from base64 on first use
```

That is the entire claim set — no email, no name, nothing else. `current_principal`
requires exactly `["sub", "role", "exp", "iss"]` to be present
(`common/auth.py:98-121`); anything missing, expired, or signed with the wrong key is a
`401` with no detail beyond "invalid" or "expired" — the specific reason is never returned,
so an attacker probing a token can't learn which part to fix.

---

## 3. Verifying a token, on every request

```
Authorization: Bearer <token>
        │
        ▼
current_principal(credentials = Depends(HTTPBearer(auto_error=False)))
        │
        ├─ credentials is None ──────────────────▶ 401  "Bearer token is required"
        │
        ▼
jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], issuer=ISSUER,
           options={"require": ["sub","role","exp","iss"]})
        │
        ├─ ExpiredSignatureError ────────────────▶ 401  "Access token has expired"
        ├─ InvalidTokenError (bad sig, wrong      ▶ 401  "Access token is invalid"
        │   issuer, malformed, wrong algorithm)      (one message for all of these)
        │
        ▼
Principal(user_id=sub, role=role, token=<the raw bearer string>)
```

`Principal.token` is kept specifically so a handler can **forward the caller's own
credential** to a sibling service (`common/auth.py::bearer`) — see §5.

`auto_error=False` on `HTTPBearer` is a small but load-bearing choice: FastAPI's own default
for a missing header raises `403`, and this platform's error contract (D05) reserves `403`
for "authenticated but not permitted". A missing token is `401`, always, with a
`WWW-Authenticate: Bearer` header attached (`common/errors.py::unauthorized`).

---

## 4. Authorization: three independent mechanisms, applied in three different ways

`auth.py` exports three guards, and no endpoint reaches for more than one of them at once:

```
 require_role(*allowed)          — a FastAPI dependency; wraps current_principal;
                                    401 unauthenticated, 403 wrong role.
                                    `system_admin` is ALWAYS admitted, everywhere.

 require_self_or_admin(p, id)    — a plain function, called inside the handler body,
                                    after the resource (or its owner column) is already
                                    known. Compares as STRINGS — psycopg2 returns UUID
                                    columns as strings, so comparing a parsed UUID object
                                    against one would silently never match.

 require_internal                — a FastAPI dependency reading X-Internal-Key;
                                    secrets.compare_digest against INTERNAL_API_KEY;
                                    401 (not 403) on anything wrong — an internal
                                    endpoint pretends not to exist to an outsider.
```

### 4.1 Where each one actually guards something today

| Service | Endpoint | Guard |
|---|---|---|
| User | `POST /users/logout` | `current_principal` |
| User | `GET /users/{id}` | `current_principal` + `require_self_or_admin` |
| Restaurant | `POST /restaurants/onboard` | `require_role("restaurant_admin")` |
| Restaurant | `GET /restaurants/{id}` | `current_principal` (any authenticated caller) |
| Restaurant | `GET /restaurants/{id}/tickets`, `accept`, `reject` | `require_role("restaurant_admin")` + `require_self_or_admin` on `owner_id` |
| Restaurant | `GET /restaurants/{id}/internal`, `POST /tickets`, `POST /tickets/{id}/expire` | `require_internal` |
| Menu | `POST /menus` | `require_role("restaurant_admin")` |
| Menu | `GET /menus/{id}` | `current_principal` (any authenticated caller) |
| Order | `POST /orders` | `require_role("customer")` |
| Order | `GET /orders/{id}`, `GET /orders/{id}/logs` | `current_principal` + `require_self_or_admin` on `customer_id` |
| Order | `GET /orders/{id}/internal`, `POST /orders/logs`, `POST /orders/{id}/signals` | `require_internal` |
| Payment | `POST /payments`, `GET /payments/{id}` | `require_role("customer")` (ownership settled by reading the order, §4.2) |
| Payment | `POST /payments/authorize`, `POST /payments/refund` | `require_internal` |
| Rider | `POST /riders`, `GET /riders/me`, `PATCH /riders/me/location`, `PATCH /riders/me/availability`, `POST /riders/me/orders/{id}/picked-up`, `/delivered` | `require_role("rider")` |
| Rider | `POST /riders/dispatch`, `POST /riders/release` | `require_internal` |

Every health endpoint (`GET /api/v1/{service}/health`) is deliberately unguarded — a probe
must not need a credential.

### 4.2 "Each authorisation decision lives in exactly one place"

The Payment Service never checks who owns an order. `POST /api/v1/payments` reads the order
by forwarding the caller's own token to `GET /api/v1/orders/{id}`, and *that* endpoint's
`require_self_or_admin` check is what actually decides ownership:

```
customer ──Bearer token──▶ Payment Service ──same Bearer token──▶ Order Service
                                                                       │
                                                          require_self_or_admin(
                                                            principal, order.customer_id)
                                                                       │
                                                          403 if it's not their order
```

If that check fails, the Order Service's `403` passes straight through to the customer —
D05's error-contract table maps a downstream `401`/`403` to `403`, not to a `502`, precisely
so this refusal doesn't get flattened into "something broke". Restaurant tickets and
`riders/me/*` follow the identical pattern: ownership is decided once, by the service that
owns the fact, never re-decided by a caller.

### 4.3 Why a role check happens *again* over HTTP, even though the token has one

`RestaurantServiceClient.verify_owner` (called from `restaurant/main.py`) and
`RiderServiceClient` (`rider/main.py`) both re-fetch the account from the User Service and
check its **current** role — even though `principal.role` already came out of the token.

```
 token minted at 09:00, role="restaurant_admin", exp=09:15
      │
      │  09:05 — an admin demotes this account to "customer"
      ▼
 09:10 — the same still-valid token is presented to POST /restaurants/onboard
      │
      ▼
 require_role("restaurant_admin")  → PASSES (the token still says restaurant_admin)
      │
      ▼
 verify_owner() → GET /api/v1/users/{id} → role is NOW "customer" → 403
```

The token's role claim is a fact about the moment it was signed, not a fact about now. Any
endpoint that only checked the claim would honor a role for up to 15 minutes after it was
revoked. The extra HTTP round trip is what closes that window — deliberately kept even
though it looks redundant next to `require_role`.

---

## 5. How services identify themselves to each other

Two distinct mechanisms, never mixed on a single call:

```
 ON BEHALF OF A USER                          AS A SIBLING SERVICE
 ────────────────────                         ─────────────────────
 bearer(principal.token)                      internal_headers()
   → {"Authorization": "Bearer <same JWT>"}     → {"X-Internal-Key": <shared secret>}

 used for: any lookup the caller could have    used for: writes and reads no end user
 made themselves — verifying a customer,       may trigger directly — the audit log,
 an owner, a restaurant, fetching a menu       ticket creation, payment authorize/refund,
                                                rider dispatch/release, signal relay

 a service using this can NEVER do more        proves only "this came from inside the
 than the forwarded user could                 platform" — carries no user identity at all
```

`order-worker` — the process that runs the Temporal saga's activities — is the clearest case
for why the second mechanism has to exist at all: an activity has no user session behind it,
and even if it did, a bearer token is the wrong thing to put inside a workflow argument,
because Temporal persists that argument as durable, UI-visible history. `internal_headers()`
is what every saga activity uses instead (see
[order-saga-orchestration-guide.md](order-saga-orchestration-guide.md) §1 and
`key-decisions.md` D26).

---

## 6. Sessions: why there are two tokens, not one

```
                    LOGIN
                      │
        ┌─────────────┴─────────────┐
        ▼                           ▼
  ACCESS TOKEN                REFRESH TOKEN
  RS256 JWT                   opaque, secrets.token_urlsafe(32)
  15 minutes                  7 days
  stateless — cannot be       stored SHA-256-hashed in Redis DB 1
  revoked before it expires   (DB 1, not DB 0 — kept apart from the
                               Menu Service's cache; an accidental
                               FLUSHDB on the cache must not sign out
                               every user in the platform)
```

An access token, once signed, is valid until `exp` no matter what happens afterward — RS256
has no built-in revocation. So "logout" cannot mean anything if there is only one token: the
whole reason a second, *stateful* token exists is that Redis can be told to forget it.

### 6.1 Refresh — read-and-delete in one step

```
POST /api/v1/users/refresh  {refresh_token}
        │
        ▼
RefreshTokenStore.consume(token)
        │  GETDEL refresh:<sha256(token)>      — read the value AND delete the key,
        │                                        atomically, in one Redis round trip
        ▼
   found? ──no──▶ 401 "Refresh token is invalid, expired or already used"
        │
       yes
        ▼
users.find_role(user_id)     ← re-read from Postgres, not carried in the old token
        │
        ▼
_issue_session(user_id, role)   → a NEW access token + a NEW refresh token
```

`GETDEL` is what makes this **rotation**, not reuse: the presented refresh token is consumed
in the exact same round trip that reads it, so it cannot be replayed a second time — by the
legitimate client or by anyone who intercepted it in transit. Whoever refreshes next simply
gets a token the old one can no longer redeem.

### 6.2 Logout — revoke, but only your own

```
POST /api/v1/users/logout  {refresh_token}      Authorization: Bearer <access token>
        │
        ▼
current_principal(access_token)   → who is asking
        │
        ▼
RefreshTokenStore.revoke_owned(refresh_token, principal.user_id)
        │  GET refresh:<hash>  →  does the stored value == this user_id?
        │       no  → return False (logged, not raised — logout is still 204)
        │       yes → DELETE refresh:<hash>
        ▼
204 No Content — always, whether or not the token was actually live
```

Logout needs *both* tokens: the access token proves who is asking, and the refresh token is
what actually gets revoked. The ownership check (`GET` before `DELETE`) is what stops a
logout call from ending someone else's session with a guessed or stolen refresh token — it's
not atomic, but the only thing that can race it is a token that was about to be deleted
anyway.

**What logout does *not* do:** the access token already issued keeps working until its own
15-minute `exp`. That gap is the direct cost of statelessness, and it is why the access
token's lifetime is kept short — 15 minutes is the longest a "logged out" session can still
act.

---

## 7. Secrets: what's where, and why

```
 JWT_PRIVATE_KEY_B64   →  user-service ONLY
                          (the ability to mint any identity — including system_admin —
                           so only the issuer may hold it)

 JWT_PUBLIC_KEY_B64    →  every service
                          (verification needs it; it cannot be used to forge a token)

 INTERNAL_API_KEY      →  every service
                          (symmetric — anything internal-only, in either direction)
```

`common/auth.py` loads `PUBLIC_KEY` and `INTERNAL_API_KEY` **at import time**
(`common/auth.py:50-53`) via `required(...)`, so a service that is missing either one fails
to start rather than serving traffic it cannot actually authenticate. The private key is the
exception — loaded lazily on first use (`_signing_key()`) — so every container except
`user-service` never has to have it configured at all, and the smoke test's assertion that
`printenv JWT_PRIVATE_KEY_B64` is empty everywhere else is checking something real.

`scripts/smoke-test.sh` asserts this split directly, not just by convention:

```bash
docker exec sfo-order-service printenv JWT_PRIVATE_KEY_B64   # must print nothing
```

---

## 8. What is *not* handled yet

- **No key rotation path.** Rotating the RSA keypair invalidates every live access token at
  once — there is no `kid` header or multiple-accepted-public-keys mechanism.
- **`system_admin` bypasses every ownership check**, in every service that has one
  (`require_role` always admits it; `require_self_or_admin` always passes it). Convenient,
  and currently unaudited — no log records when an admin used the bypass versus acted as
  themselves.
- **The internal key is one shared secret across an increasingly long list of endpoints** —
  **eleven** of them, as of the Week 2 saga, across four services, including refunds
  (`grep -c 'Depends(require_internal)' services/*/main.py`). `key-decisions.md` D15/D26 name
  this explicitly: per-service keypairs are the better answer once that list grows, and it
  has grown every time the saga did.
- **No brute-force throttling on `/login`.** The constant-time comparison stops the endpoint
  from *leaking which half was wrong*; it does nothing to slow down repeated guessing.
