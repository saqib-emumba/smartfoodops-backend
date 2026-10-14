# Multi-Role Support & Role-Based Access Control — Design

Status: **implemented** — see [D48](key-decisions.md#d48--multiple-roles-per-user-via-a-junction-table-not-an-implicit-everyone-is-a-customer-rule)
for the changelog-style record of what shipped. This doc maps how a user gains more than one
role, and how every authorization decision in the platform behaves once that's possible.

> **Partly superseded by [D57](key-decisions.md#d57--rbac-moves-into-the-database-and-is-enforced-at-the-gateway)
> on 2026-10-06.** The multi-role model below is unchanged. What changed is where the rules
> live and who applies them: roles are now granted *permissions* in three new tables in
> `sfo_user_core`, a route demands a permission rather than naming a role, and the API gateway
> refuses a wrong-role request before any backend service sees it. §3.1 and §3.7 are updated;
> read `require_role` elsewhere in this document as `require_permission`.

## 1. The problem

Today `users.role_id` is a single `NOT NULL` foreign key — one role per user, enforced at the
schema level. The access token carries that one role as a single string claim, and every guard
in the system (`require_role`, `assert_account_role`) compares against that one value. A
`restaurant_admin` cannot also act as a `customer` — not because of a deliberate business rule,
but because the identity model has no way to represent it.

**Trigger scenario:** a restaurant owner wants to place an order for himself, the same way any
customer would. `services/order/apis/checkout.py:27` gates order creation with
`Depends(require_role("customer"))`, and the smoke suite pins this down explicitly —
`scripts/smoke-test.sh:463-464`:
```
expect "restaurant owner cannot place an order -> 403" 403 POST /api/v1/orders "$ORDER" \
  -H "X-Idempotency-Key: $IDEM-ownerorder" "${OWNER_AUTH[@]}"
```

## 2. Schema: a junction table, not a wider column

```sql
CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, role_id)
);
```

- **Composite PK, no surrogate id.** A grant has no identity of its own beyond "this user holds
  this role" — the pair *is* the fact being recorded, and the PK gives duplicate-grant
  protection for free.
- **Asymmetric delete behavior**, matching the existing convention on `users.role_id`:
  `ON DELETE CASCADE` on `user_id` (delete a user, their grants go with them) but
  `ON DELETE RESTRICT` on `role_id` (a role is reference data — you can't delete `customer`
  out from under every user who holds it).
- **`users.role_id` is dropped outright**, not kept as a "primary role" column. Two sources of
  truth for "what role does this user have" is worse than one column moving. If a "primary /
  default role" concept is wanted later (e.g. for picking a default UI), it's a cheap addition
  — `is_primary boolean` on `user_roles`, or `MIN(granted_at)` — not a reason to keep `role_id`
  around now.
- **No `revoked_at` / soft delete.** A revoked grant is a deleted row, consistent with how the
  rest of the schema treats deletion. An audit trail of grants/revocations is a separate,
  later decision if it's ever needed.

ERD delta (`readme/diagrams/erd.md`):

```mermaid
%% before
ROLES ||--o{ USERS : "defines_role_of"

%% after
ROLES }o--o{ USERS : "grants_role_to (via USER_ROLES)"
```

`USERS.role_id` disappears from the entity block; a new `USER_ROLES { user_id, role_id,
granted_at }` block is added, mirroring the join-table style already used for the `roles`
lookup itself.

## 3. Role-based access control — how it works once a user can hold more than one role

This is the part reviewers specifically asked to see mapped out. The platform already has a
small, consistent set of guard primitives (`services/common/auth.py`) that every service reuses
— the RBAC model isn't a new system bolted on, it's these same primitives made
multi-role-aware, plus one new pair of endpoints to manage the grants themselves.

### 3.1 The four guard primitives, and what changes in each

| Primitive | Today (single role) | Under multi-role | Changes? |
|---|---|---|---|
| `require_role(*allowed)` | `current_user.role not in allowed` | `not (set(current_user.roles) & set(allowed))` | **Yes** — membership becomes set-intersection |
| `require_permission(*allowed)` *(D57)* | did not exist | `not (set(current_user.permissions) & set(allowed))` | **Replaces `require_role`** — same intersection, over capabilities resolved from the caller's roles by the gateway rather than role names hardcoded at the call site |
| `require_self_or_admin(current_user, subject_id)` | string-compares the caller's id to a resource's owning id; admin bypasses | unchanged — never compared roles, only identity | No |
| `assert_account_role(account, required)` → `assert_account_has_role` | `account["role"] != required` | `required not in account["roles"]` | **Yes**, plus a rename so the plural-aware version can't silently be confused with the old one |
| `require_internal(x_internal_key)` | shared secret header, no user identity involved at all | unchanged | No |

The reason only two of the four need to change is that only two of them ever look at a *role
value*. `require_self_or_admin` and `require_internal` gate on *identity* (is this you, or is
this a sibling service?), not on *what role you hold* — ownership and service-to-service trust
are orthogonal to how many roles a user has. This is worth stating plainly because it means the
blast radius of "add multi-role support" is much smaller than "touch every authorization check
in the platform": it is exactly the two role-comparing primitives, plus every place that reads
the User Service's role field off an HTTP response.

### 3.2 `is_admin` under a role set

`system_admin` bypasses every `require_role` gate and every ownership check
today (decision D33 made this bypass consistent across ownership checks specifically, after a
bug where one endpoint's role gate honored it but the ownership check underneath didn't). Under
multi-role, `is_admin` becomes `ADMIN_ROLE in current_user.roles` — a user who holds
`system_admin` *among other roles* is still fully an admin. There is no partial-admin state:
holding `system_admin` alongside `customer` doesn't create some hybrid permission level, it just
means both role's capabilities are available, and the admin bypass still applies to everything.

### 3.3 Why `require_role` becomes set-intersection, not "any one active role"

An alternative design exists and is worth naming as rejected: a user could hold multiple roles
in the database but have only one "active" role per session (like switching organizations in
many SaaS products), with the JWT still carrying a single scalar and an explicit
switch-context action to change which one is active. That model fits a use case like "act as
a different tenant" — it does not fit this one. The reviewers' own trigger scenario is a
restaurant owner who wants to place an order *without stepping out of* being a restaurant
owner — he's not switching identities, he's exercising a second capability concurrently. A flat
array of currently-held roles, checked by intersection, is the direct fit: `require_role`
answers "does this caller currently hold at least one of these roles," and a caller can satisfy
any number of different route's requirements in the same request session without a context
switch.

### 3.4 Ownership checks stay independent of role checks — and that's deliberate

A `require_role("restaurant_admin")` gate answers "is this a restaurant admin at all," never
"is this restaurant admin's own restaurant." That second question is always a separate,
explicit ownership check layered underneath — e.g. `services/order/apis/kitchen.py`
gates on `require_role("restaurant_admin")` and then, inside the handler, calls
`verify_owner(restaurant_id, current_user)`, which does `require_self_or_admin(current_user,
restaurant["owner_id"])`. Multi-role doesn't change this layering at all — a user holding
`["restaurant_admin", "customer"]` who tries to manage a restaurant they don't own is still
refused by the ownership check, exactly as a single-role `restaurant_admin` would be. Holding
more roles never grants more access to a *specific resource* — it only ever grants access to
more *route categories*. This distinction (role gate vs. ownership gate, always both, never
one standing in for the other) is decision D16's "each authorization decision lives in exactly
one place," and multi-role support must preserve it rather than collapse the two checks
together.

### 3.5 Stale tokens and role changes mid-session

An access token's role claim was true when the token was signed (D18). Today, a small number of
call sites (`verify_owner`, `verify_rider`) defend against a role having changed since then by
re-fetching the account over HTTP and checking its *current* role, rather than trusting the
token. Under multi-role this becomes `assert_account_has_role`, checking membership in the
freshly-fetched `roles` array instead of equality against a single value — same defense,
plural-aware. Separately, refresh already re-reads the role from Postgres on every
`/refresh` call rather than carrying it forward in the refresh token (so a demotion takes
effect at the next refresh, not immediately, but also not never) — this becomes "re-read the
full role *set*," same mechanism, same timing guarantee. Both of these are existing, deliberate
properties of the system that multi-role support must carry forward unchanged, not properties
being introduced by this design.

One gap is visible and worth naming rather than quietly fixing as a drive-by:
`services/order/clients/user.py`'s `verify_customer` only checks that the caller's id exists as
an account — it never calls `assert_account_has_role`, unlike `verify_owner`. That means order
creation's "is this really a customer" question is answered entirely by the token's role claim
via `require_role("customer")`, not re-verified fresh. This asymmetry predates multi-role and
is out of scope here, but it's now slightly more visible: a user who is demoted out of
`customer` mid-session (if that ever becomes a real workflow — e.g. an admin revoking a role
for abuse) could still place one more order before their token expires or refreshes. Flagging
it as a known, pre-existing gap for a future decision, not something this change silently papers
over.

**D57 adds an asymmetry to this section that is worth stating plainly.** Roles still come
from the token's claims, so everything above holds unchanged: a newly granted role waits for a
refresh or re-login. But *permissions* are no longer carried anywhere near a token — the
gateway resolves them from the caller's roles against `role_permissions` on every request. So
broadening what a role can do reaches tokens already in circulation immediately, while
granting a role to a user still does not. Both halves are asserted in `smoke-test.sh`: one
case re-logs-in after a role grant because it must, and one case does not after a permission
grant because it need not. Note also that `require_role("customer")` no longer appears in the
`verify_customer` gap described above — it is `require_permission("order:create")` now, which
changes the literal but not the gap: the token's role claim is still what answers it.

### 3.6 Granting and revoking roles — the new surface, and who may use it

New endpoints, `services/user/apis/roles.py`, under the existing `/api/v1/users` prefix:

| Route | Guard | Notes |
|---|---|---|
| `POST /api/v1/users/{id}/roles` `{"role": "..."}` | `require_self_or_admin(current_user, id)`, **plus**: if `role == "system_admin"`, also requires `current_user.is_admin` | Idempotent — granting an already-held role is a no-op, matching the junction table's PK |
| `DELETE /api/v1/users/{id}/roles/{role_name}` | `require_self_or_admin(current_user, id)`, **plus**: if `role_name == "system_admin"`, also requires `current_user.is_admin` | 409 if it would leave the user with zero roles |

No separate `GET .../roles` — `GET /api/v1/users/{id}` already returns the full `roles` array as
part of the profile, so a parallel read path would just be a second way to ask the same
question.

**The deliberate policy call here: self-grant is unrestricted for every role except
`system_admin`.** Registration is unrestricted too — a new account can register directly as
`customer`, `restaurant_admin`, or `rider` (this was already true and doesn't change). After
registration, an existing account may add any of those same three roles to itself at will. The
one role carved out on both paths that matter here — grant and revoke — is `system_admin`:
assigning or removing it, on any account including the caller's own, requires the caller to
already be a `system_admin`. This is worth stating explicitly because it's the kind of thing a
reviewer might assume was overlooked rather than decided:

- The alternative — every role fully self-service, `system_admin` included — was considered and
  rejected specifically for `system_admin`, because unlike `customer`/`restaurant_admin`/`rider`,
  it bypasses every ownership check in the system (§3.2, §3.4). For the other three roles,
  holding the label only ever unlocks a *category* of routes — a specific resource is still
  gated by an ownership check underneath (§3.4), so self-service costs nothing a second
  registration couldn't already buy. `system_admin` is the one label that *is* the access, with
  no further check downstream, so it's the one exception where "is the label scarce" actually
  matters.
- The check applies uniformly to *any* target account, not just "others" — an admin granting or
  revoking their own `system_admin` still passes only because they already are one, not because
  self-target is special-cased. This keeps the rule a single condition (`role == system_admin
  implies current_user.is_admin`) rather than a matrix of self-vs-other cases.
- Revoke mirrors grant on purpose, including for self-revoke: an admin removing their own
  `system_admin` role still needs to already be an admin to do it (trivially true) — the
  symmetry means there's exactly one rule to audit for this role ("touching `system_admin`
  requires already holding it"), not a separate, easier-to-miss rule for the delete path.
- A second, separate policy table (which roles require elevation to grant/revoke) was considered
  and rejected as unnecessary machinery — a single `role == ADMIN_ROLE` check in the two handlers
  covers the entire requirement without a new schema concept.

### 3.7 Full RBAC decision table

> **Superseded by [D57](key-decisions.md#d57--rbac-moves-into-the-database-and-is-enforced-at-the-gateway)
> on 2026-10-06.** This section used to *be* the decision table, maintained by hand. It is now
> a description of where that table lives: three tables in `sfo_user_core`, seeded by
> `db/user/init.sql` sections 1d–1f. The prose version is kept below as the rationale for each
> row, but the database is the source of truth, and `scripts/smoke-test.sh` asserts the two
> agree rather than trusting them to.

The artifact a reviewer or auditor asks for first is now one query:

```sql
SELECT r.name AS role, p.name AS permission
  FROM roles r
  JOIN role_permissions rp ON rp.role_id = r.id
  JOIN permissions p ON p.id = rp.permission_id
 ORDER BY r.name, p.name;
```

**The capability vocabulary** (`permissions`), and which role holds each
(`role_permissions`). `system_admin` additionally holds every permission, seeded by a
`CROSS JOIN`, so the table answers the question without a reader needing to know about the
`is_admin` bypass in code:

| Permission | Held by | What it admits |
|---|---|---|
| `order:create` | `customer` | Checkout |
| `order:read_any` | `system_admin` | Listing any order by status, regardless of ownership |
| `payment:create` | `customer` | Paying directly, outside the saga |
| `payment:read` | `customer` | Reading a payment, ownership still settled by reading its order |
| `restaurant:onboard` | `restaurant_admin` | Registering a restaurant |
| `menu:write` | `restaurant_admin` | Publishing or replacing a menu |
| `kitchen:read` | `restaurant_admin` | Reading a kitchen queue |
| `kitchen:decide` | `restaurant_admin` | Accepting or rejecting an order |
| `rider:profile` | `rider` | Joining the fleet; own profile, location, availability |
| `delivery:report` | `rider` | Reporting pickup and delivery |
| `ai:search` | `customer` | Semantic search over dishes and restaurants (Week 4, D59) |
| `ai:rag_context` | `customer` | Assembling RAG context for the caller's own account (Week 4, D62) |

**Which permission each route demands** (`route_permissions`). Ownership checks are unchanged
and still in the handlers — they need the resource row, so they can never move to the gateway:

| Method + path | Permission demanded | Ownership check (in the handler) | Ref |
|---|---|---|---|
| `POST /api/v1/users/register` \| `/login` \| `/refresh` | *absent from the table* | — | public; nginx exempts them from `auth_request` |
| `POST /api/v1/users/logout` | `NULL` (authenticated) | the refresh token is the credential | |
| `GET /api/v1/users/{id}` | `NULL` (authenticated) | `require_self_or_admin` | |
| `POST /api/v1/users/{id}/roles` | `NULL` (authenticated) | `require_self_or_admin` + `system_admin` carve-out | §3.6 |
| `DELETE /api/v1/users/{id}/roles/{role}` | `NULL` (authenticated) | `require_self_or_admin` + last-role guard + carve-out | §3.6 |
| `POST /api/v1/restaurants/onboard` | `restaurant:onboard` | — (creates own restaurant) | |
| `GET /api/v1/restaurants/{id}` | `NULL` (authenticated) | — | |
| `POST /api/v1/menus` | `menu:write` | manual `owner_id` compare | |
| `GET /api/v1/menus/{restaurant_id}` | `NULL` (authenticated) | — | |
| `POST /api/v1/orders` (checkout) | `order:create` | — (`customer_id` = caller, D13) | |
| `GET /api/v1/orders` (by status) | `order:read_any` | — | |
| `GET /api/v1/orders/{id}` | `NULL` (authenticated) | `require_self_or_admin` on `customer_id` | |
| `GET /api/v1/orders/{id}/logs` | `NULL` (authenticated) | `require_self_or_admin` on `customer_id` | |
| `GET /api/v1/orders/kitchen/{restaurant_id}` | `kitchen:read` | `verify_owner` → `require_self_or_admin` on `owner_id` | D33 |
| `POST /api/v1/orders/{id}/accept` \| `/reject` | `kitchen:decide` | `verify_owner` on the order's restaurant | D33 |
| `POST /api/v1/payments` | `payment:create` | — (own payment) | |
| `GET /api/v1/payments/{id}` | `payment:read` | settled by reading the order as the caller | |
| `POST /api/v1/riders` | `rider:profile` | self-scoped by construction | |
| `GET /api/v1/riders/me`, `PATCH /me/location`, `/me/availability` | `rider:profile` | self-scoped by construction | |
| `POST /api/v1/riders/me/orders/{id}/picked-up` \| `/delivered` | `delivery:report` | self-scoped by construction | |
| `POST /api/v1/ai/search` | `ai:search` | — (reads only published, available dishes) | D59 |
| `POST /api/v1/ai/rag-context` | `ai:rag_context` | `require_self_or_admin` on the body's `customer_id` | D62 |
| `/api/v1/payments/authorize` \| `/refund` \| `/manual`, `/api/v1/orders/*/internal*`, `/transitions`, `/rider-report`, `/logs`, `/api/v1/riders/dispatch` \| `/release` \| `/internal/nearby`, `/api/v1/menus/*/internal` \| `/internal/restaurant-ids`, `/api/v1/restaurants/*/internal`, `/api/v1/analytics/internal/*` | *absent from the table* | — | `require_internal` only (D15); the gateway refuses them (no row, or no route at all) |

Three things to read carefully in that table:

- **`NULL` is a decision, not a gap.** It says the gateway admits any authenticated caller
  and the handler's ownership check is the real guard. Nine routes are like this, and
  "tidying" a `NULL` into a permission would silently replace an ownership rule with a role
  rule. A smoke-test case asserts that `GET /api/v1/orders/{id}` is still refused by the
  *service*, not the gateway, for a non-owner.
- **Absent is also a decision, and it is the opposite one.** Public and internal-key routes
  have no rows because nginx never sends them through `auth_request`, so the policy decision
  point never sees them. For anything that *is* gated, absence means refusal: matching is
  fail-closed, including for `system_admin`.
- **Method is part of the match.** `DELETE /api/v1/menus` is refused even though
  `POST /api/v1/menus` is a row, because the policy keys on both.

## 4. Worked scenario: restaurant owner places his own order

1. Register as `restaurant_admin` → `POST /api/v1/users/register {"role":"restaurant_admin", ...}`.
   Signup still grants exactly one role — the `user_roles` row for `restaurant_admin` is
   created at registration, same transaction as the `users` insert.
2. Login → token carries `roles: ["restaurant_admin"]`.
3. `POST /api/v1/orders` with that token → **403**. `require_role("customer")` fails: the set
   `{"restaurant_admin"}` doesn't intersect `{"customer"}`, and this user isn't `system_admin`.
   This is the existing, unchanged smoke-test assertion — nothing about opting in changes the
   *default*.
4. Self-grant → `POST /api/v1/users/{own_id}/roles {"role":"customer"}` → **200**. A new
   `(user_id, customer_role_id)` row is inserted into `user_roles`.
5. **Still using the original (pre-grant) access token** → `POST /api/v1/orders` → **still
   403**. The token's `roles` claim was fixed at login and doesn't know about the grant yet —
   this is §3.5's stale-token behavior, made concrete: the same reason a demotion doesn't take
   effect until the next refresh means a *promotion* doesn't either. The grant is real in the
   database immediately; the token just hasn't caught up.
6. Refresh (or re-login) → `POST /api/v1/refresh` re-reads the role set from Postgres → new
   token carries `roles: ["restaurant_admin", "customer"]`.
7. `POST /api/v1/orders` with the refreshed token → **201**. `require_role("customer")` now
   passes (intersection is non-empty). `customer_id` on the created order is set from
   `current_user.user_id` — the token's `sub` claim — never from the request body (D13), so
   even though this caller also holds `restaurant_admin`, the order can only ever be placed as
   *himself*, never on behalf of another user.
8. He can still do everything `restaurant_admin` could before — manage his restaurant's menu,
   accept/reject orders in his kitchen queue — because holding an additional role only adds
   capabilities, it never removes or shadows the ones already held.

## 5. What this design deliberately does not do

- **No implicit "everyone is a customer."** The reviewers asked for the junction-table /
  explicit-grant model, and that's what's designed here. The cheaper alternative — treat
  order-placement as available to any authenticated user regardless of role, with no grant step
  at all — was considered (see the prior conversation) and set aside in favor of this explicit
  model, which is the one that generalizes to *any* two roles a user might want to combine, not
  just "restaurant_admin plus implicit customer."
- **No role hierarchy / inheritance.** Roles are a flat set; `system_admin` is not "customer +
  restaurant_admin + rider + more," it's a distinct label that happens to bypass every gate it
  meets. Adding a real hierarchy (e.g. `restaurant_admin` implies `customer`) is a bigger,
  separate decision with its own trade-offs and isn't needed to solve the trigger scenario.
- **No versioned/dual-format JWT for a gradual rollout.** Because this is pre-production with
  no live traffic to protect, the plan is a clean, one-time cutover of the claim shape
  (`role` → `roles`) rather than a token format that speaks both for a transition period. A
  production system with real sessions would need that; this one doesn't, and building it
  anyway would be speculative complexity with no user to protect.

## 6. Open follow-ups (not blocking this design, but worth tracking)

- Delete the temporary `CurrentUser.role` compatibility shim once a repo-wide search confirms
  no remaining call site reads the singular form.
- Decide whether `verify_customer`'s pre-existing "existence only, no fresh role check" gap
  (§3.5) is worth closing, independent of this change.
- Decide whether a `user_roles.is_primary` flag (or `MIN(granted_at)`) is ever needed for a
  "default role" UI affordance — not needed today, cheap to add later.
- **Admin endpoints for editing grants** (`POST`/`DELETE /api/v1/users/roles/{role}/permissions`,
  `system_admin`-only, plus an immediate reload hook). Deliberately deferred by
  [D57](key-decisions.md#d57--rbac-moves-into-the-database-and-is-enforced-at-the-gateway):
  they are endpoints that hand out privilege, and a SQL edit already takes effect within the
  snapshot TTL without a restart. Worth adding when editing the policy becomes a routine
  operator task rather than a deployment-time one.
- **A shared invalidation channel for the policy snapshot** (Redis pub/sub, or simply dropping
  the TTL). Needed the moment the User Service runs more than one replica: today a refresh
  reaches only the process that performs it, which is correct at one replica and wrong at two.
- **Resource-scoped grants** — `restaurant_admin` *of restaurant X* rather than of all
  restaurants. This is the point at which a route→permission table stops being expressive
  enough and the ownership checks in the handlers would fold into the policy model itself.
  Today those two layers are cleanly separate (§3.4) and that is worth keeping until it isn't.
