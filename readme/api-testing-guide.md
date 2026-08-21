# SmartFoodOps — Manual API Testing Guide

Copy-pasteable `curl` commands for exercising every service by hand, grouped by service and
ordered so that IDs captured early feed the calls that follow.

For an automated pass instead, run [`scripts/smoke-test.sh`](../scripts/smoke-test.sh) — it
covers everything below with assertions and an exit code, and
[`scripts/saga-resilience-test.sh`](../scripts/saga-resilience-test.sh) adds the concurrency
and worker-restart cases. Use this document when you want to poke at one endpoint, see a raw
response, or demo the flow.

All traffic goes through the Nginx gateway on port **80**; no service port is published by
default. The one exception is the **Temporal Web UI** on <http://localhost:8233>, which is
reached directly — it has no authentication of its own, so putting it behind the public
gateway would publish every workflow's history.

Sections 1–7 are Week 1: one request at a time. **Section 8 is the order saga**, where the
lifecycle is driven by a durable workflow rather than by the caller — which is also where the
Week 1 payment flow changed (see the note at section 5).

---

## Setup

Start the stack and confirm it is reachable:

```bash
docker compose up -d
export BASE=http://localhost
```

Two helpers used throughout — `pretty` formats a JSON response, `field` pulls one value out:

```bash
pretty() { python3 -m json.tool; }
field()  { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
```

Registration enforces unique email **and** phone, so re-running section 1 verbatim returns
`409`. Generate a fresh suffix each session:

```bash
export RUN=$(date +%s)
```

### Authentication

Everything except the health probes, `register`, `login` and `refresh` needs a bearer token,
and **identity is never sent in a request body** — no `owner_id`, no `customer_id`. Section 1
registers two accounts and logs both in; the rest of the guide uses the resulting tokens.

A third helper logs in and echoes the access token:

```bash
login() {
  curl -s -X POST "$BASE/api/v1/users/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\"}" | field "['access_token']"
}
```

Tokens last 15 minutes. If calls suddenly return `401`, run `login` again (or use
`POST /api/v1/users/refresh`, section 1.4).

One endpoint — `POST /api/v1/orders/logs` — is service-to-service and takes a shared key
instead of a bearer token, so that customers cannot write the audit trail describing their
own orders. Export it from the same `.env` compose reads:

```bash
export INTERNAL_KEY=$(grep -m1 '^INTERNAL_API_KEY=' .env | cut -d= -f2-)
```

---

## 0. Health

Health endpoints stay public — a probe must not need a credential.

Every service exposes a health endpoint that also round-trips its datastore.

```bash
for p in users restaurants menus orders payments riders; do
  printf '%-14s ' "$p"
  curl -s -w ' [%{http_code}]\n' "$BASE/api/v1/$p/health"
done
```

All six return `200`. A `200` carrying `"database_reachable": false` means the app is up but
its store is not — check `docker compose ps`.

Two flags are softer than the rest, deliberately. The Menu Service's
`"cache_reachable": false` still serves menus, straight from Postgres, because the cache is a
copy and never the source of truth. The Order Service's `"temporal_reachable": false` still
accepts and serves orders; what stops is sagas *advancing* them.

---

## 1. User Service (`:8001`)

### 1.1 Register a restaurant owner → `201`

```bash
curl -s -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{
    \"email\": \"owner_$RUN@example.com\",
    \"password\": \"Passw0rd!\",
    \"full_name\": \"Aisha Khan\",
    \"phone\": \"+15551$RUN\",
    \"role\": \"restaurant_admin\"
  }" | pretty
```

Capture the id — sections 2 and 4 need it:

```bash
export OWNER_ID=$(curl -s -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"owner2_$RUN@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Aisha Khan\",\"phone\":\"+15552$RUN\",\"role\":\"restaurant_admin\"}" \
  | field "['id']")
echo "OWNER_ID=$OWNER_ID"
```

The response never contains the password or its hash. Roles are resolved against the
`roles` table, so `role` comes back as a name rather than an id.

### 1.2 Register a customer → `201`

```bash
export CUST_ID=$(curl -s -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"cust_$RUN@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Bilal Ahmed\",\"phone\":\"+15553$RUN\",\"role\":\"customer\"}" \
  | field "['id']")
echo "CUST_ID=$CUST_ID"
```

### 1.3 Log both accounts in → `200`

```bash
export OWNER=$(login "owner2_$RUN@example.com" "Passw0rd!")
export CUSTOMER=$(login "cust_$RUN@example.com" "Passw0rd!")
```

The full response also carries a `refresh_token` and `expires_in` (900 seconds). Capture the
refresh token too if you want to exercise 1.4:

```bash
export REFRESH=$(curl -s -X POST "$BASE/api/v1/users/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"cust_$RUN@example.com\",\"password\":\"Passw0rd!\"}" | field "['refresh_token']")
```

### 1.4 Refresh and log out

Refreshing **rotates**: the token you present is consumed, so the same value never works
twice. That is what makes a captured refresh token short-lived in practice.

```bash
# Trade it for a fresh pair -> 200
export REFRESH=$(curl -s -X POST "$BASE/api/v1/users/refresh" -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$REFRESH\"}" | field "['refresh_token']")

# End the session -> 204 (needs the access token as well: you may only end your own)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/logout" \
  -H "Authorization: Bearer $CUSTOMER" -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$REFRESH\"}"
```

### 1.5 Fetch a user → `200`

This is the endpoint the Restaurant and Order Services call instead of reading the `users`
table. Restricted to the subject and `system_admin` — those services reach it while
forwarding the caller's own token, so their lookups are self-reads.

```bash
curl -s "$BASE/api/v1/users/$OWNER_ID" -H "Authorization: Bearer $OWNER" | pretty
```

### Edge cases

| Scenario | Expected |
|---|---|
| Unknown role name | `400` |
| Email already registered | `409` |
| Phone already registered | `409` |
| Password shorter than 8 chars | `422` |
| Wrong password, or an email with no account | `401`, same message for both |
| Reading somebody else's profile — or an id that does not exist | `403` |
| Refresh token already used, or logged out | `401` |

```bash
# Unknown role -> 400 (the roles table is the source of truth, so this is not a 422)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"x_$RUN@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Bad Role\",\"phone\":\"+15554$RUN\",\"role\":\"wizard\"}"

# Duplicate email -> 409
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"owner2_$RUN@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Dup Email\",\"phone\":\"+15555$RUN\",\"role\":\"customer\"}"

# Duplicate phone -> 409 (message names the phone, not the email)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"dup_$RUN@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Dup Phone\",\"phone\":\"+15552$RUN\",\"role\":\"customer\"}"

# Password too short -> 422 (rejected by Pydantic before any DB call)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"short_$RUN@example.com\",\"password\":\"abc\",\"full_name\":\"Short Pass\",\"phone\":\"+15556$RUN\",\"role\":\"customer\"}"

# Wrong password -> 401
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"owner2_$RUN@example.com\",\"password\":\"NotThePassword!\"}"

# Email with no account -> 401, byte-identical to the line above. Neither the message nor
# the response time reveals whether the address is registered.
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/users/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"ghost_$RUN@example.com\",\"password\":\"Passw0rd!\"}"

# Somebody else's profile -> 403
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/users/$CUST_ID" -H "Authorization: Bearer $OWNER"

# Unknown user -> 403, not 404: the ownership check runs before the lookup, so this endpoint
# cannot be used to discover which user ids exist.
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/users/00000000-0000-0000-0000-000000000000" \
  -H "Authorization: Bearer $OWNER"

# No token at all -> 401
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/users/$OWNER_ID"
```

---

## 2. Restaurant Service (`:8002`)

### 2.1 Onboard a restaurant → `201`

Requires a `restaurant_admin` token. The owner is the token's subject and is verified over
HTTP against the User Service — there is no `owner_id` field to send.

```bash
export REST_ID=$(curl -s -X POST "$BASE/api/v1/restaurants/onboard" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d "{
    \"name\": \"Karachi Grill\",
    \"address\": \"12 Jinnah Avenue, Islamabad\",
    \"latitude\": 33.6844,
    \"longitude\": 73.0479,
    \"capacity\": 40
  }" | field "['id']")
echo "REST_ID=$REST_ID"
```

### 2.2 Fetch a restaurant → `200`

The Menu Service calls this to confirm a restaurant exists and is active.

```bash
curl -s "$BASE/api/v1/restaurants/$REST_ID" -H "Authorization: Bearer $CUSTOMER" | pretty
```

### Edge cases

| Scenario | Expected |
|---|---|
| No token | `401` |
| Token whose role is not `restaurant_admin` | `403` |
| Latitude outside −90…90 (or longitude −180…180) | `422` |
| `capacity` of 0 or less | `422` |
| Restaurant id not in the database | `404` |
| User Service is down | `503` |

There is no "unknown owner" case any more: the owner is the authenticated subject, so it
always exists. Impersonating a different owner is not a request the API can express.

```bash
# No token -> 401
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Anonymous Diner","address":"1 Nowhere Road","latitude":10,"longitude":10,"capacity":5}'

# Customer trying to onboard -> 403 (wrong role in a valid token)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Customer Diner","address":"2 Nowhere Road","latitude":10,"longitude":10,"capacity":5}'

# Latitude out of range -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Bad Coords","address":"3 Nowhere Road","latitude":999,"longitude":10,"capacity":5}'

# Non-positive capacity -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Zero Capacity","address":"4 Nowhere Road","latitude":10,"longitude":10,"capacity":0}'

# Unknown restaurant -> 404
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/restaurants/00000000-0000-0000-0000-000000000000" \
  -H "Authorization: Bearer $CUSTOMER"
```

---

## 3. Menu Service (`:8003`)

### 3.1 Publish a menu → `200`

An upsert: calling it again replaces the whole category tree for that restaurant.

```bash
curl -s -X POST "$BASE/api/v1/menus" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d "{
    \"restaurant_id\": \"$REST_ID\",
    \"categories\": [{
      \"category_id\": \"mains\",
      \"category_name\": \"Mains\",
      \"display_order\": 1,
      \"items\": [
        {
          \"item_id\": \"burger\",
          \"name\": \"Beef Burger\",
          \"description\": \"Char-grilled patty\",
          \"base_price\": 10.00,
          \"is_available\": true,
          \"dietary_flags\": [\"halal\"],
          \"customization_groups\": [
            {
              \"group_id\": \"cheese\",
              \"group_name\": \"Cheese\",
              \"min_selection\": 1,
              \"max_selection\": 1,
              \"options\": [
                {\"name\": \"cheddar\", \"extra_price\": 1.50},
                {\"name\": \"none\", \"extra_price\": 0.0}
              ]
            },
            {
              \"group_id\": \"extras\",
              \"group_name\": \"Extras\",
              \"min_selection\": 0,
              \"max_selection\": 2,
              \"options\": [
                {\"name\": \"bacon\", \"extra_price\": 2.00},
                {\"name\": \"egg\", \"extra_price\": 1.00}
              ]
            }
          ]
        },
        {
          \"item_id\": \"soldout\",
          \"name\": \"Sold Out Dish\",
          \"description\": \"Currently unavailable\",
          \"base_price\": 5.00,
          \"is_available\": false
        }
      ]
    }]
  }" | pretty
```

`min_selection: 1` on the cheese group makes it **required** — an order that omits it is
rejected in section 4.

### 3.2 Fetch the menu → `200`

This is what the Order Service reads to price a checkout.

```bash
curl -s "$BASE/api/v1/menus/$REST_ID" -H "Authorization: Bearer $CUSTOMER" | pretty
```

### Edge cases

| Scenario | Expected |
|---|---|
| `restaurant_id` unknown or inactive | `404` |
| `min_selection` greater than `max_selection` | `422` |
| `base_price` of 0 or less | `422` |
| No menu published for that restaurant | `404` |
| Restaurant Service is down | `503` |
| The menu database is down | `503` |
| Redis is down | `200` — the read falls through to Postgres; the cache is a copy, not the source of truth |

```bash
# Unknown restaurant -> 404 (verified via the Restaurant Service, not the database)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/menus" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"restaurant_id":"00000000-0000-0000-0000-000000000000","categories":[]}'

# Unsatisfiable customization group -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/menus" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d "{\"restaurant_id\":\"$REST_ID\",\"categories\":[{\"category_id\":\"c\",\"category_name\":\"C\",\"items\":[{\"item_id\":\"i\",\"name\":\"I\",\"description\":\"d\",\"base_price\":1.0,\"customization_groups\":[{\"group_id\":\"g\",\"group_name\":\"G\",\"min_selection\":3,\"max_selection\":1,\"options\":[{\"name\":\"a\"}]}]}]}]}"

# No menu published -> 404
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/menus/11111111-1111-1111-1111-111111111111" \
  -H "Authorization: Bearer $CUSTOMER"
```

---

## 4. Order Service (`:8004`)

Prices are **always** recalculated server-side from the live menu. The `total_amount` you
send is treated as a claim to verify, not a value to trust.

For the menu above: `10.00` base `+ 1.50` cheddar `+ 2.00` bacon `= 13.50` per burger,
`× 2 = 27.00`.

### 4.1 Create an order → `201`

`X-Idempotency-Key` is mandatory.

```bash
export IDEM="order-$RUN"

export ORDER_ID=$(curl -s -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -H "X-Idempotency-Key: $IDEM" \
  -d "{
    \"restaurant_id\": \"$REST_ID\",
    \"items\": [{
      \"item_id\": \"burger\",
      \"quantity\": 2,
      \"customizations\": {\"cheese\": \"cheddar\", \"extras\": [\"bacon\"]}
    }],
    \"total_amount\": 27.00
  }" | field "['id']")
echo "ORDER_ID=$ORDER_ID"
```

The stored `items` are a priced snapshot — each line carries `unit_price`, `line_total`, and
the `selected_options` that were applied, so the order stays meaningful after the menu changes.

### 4.2 Replay the same key → `200`

Same request, same key. Note the status code is `200`, not `201`, and the id is unchanged:

```bash
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -H "X-Idempotency-Key: $IDEM" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":2,\"customizations\":{\"cheese\":\"cheddar\",\"extras\":[\"bacon\"]}}],\"total_amount\":27.00}"
```

No second order is created — that is the double-charge protection.

### Edge cases

| Scenario | Expected |
|---|---|
| `X-Idempotency-Key` header missing | `400` |
| `total_amount` disagrees with the recalculated total | `422` |
| Item has `is_available: false` | `422` |
| Item id not on the menu | `422` |
| Customization group not defined on the item | `422` |
| Option not offered by that group | `422` |
| Required group omitted | `422` |
| More selections than `max_selection` | `422` |
| No token | `401` |
| Token whose role is not `customer` | `403` |
| `restaurant_id` unknown to the Restaurant Service | `422` |
| Restaurant has no published menu | `404` |
| Menu, User or Restaurant Service is down | `503` |

```bash
# Missing idempotency key -> 400
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"

# Under-claimed total -> 422, and the message shows both numbers
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-mismatch" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"cheddar\"}}],\"total_amount\":1.00}"

# Unavailable item -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-soldout" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"soldout\",\"quantity\":1}],\"total_amount\":5.00}"

# Item not on the menu -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-unknown" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"pizza\",\"quantity\":1}],\"total_amount\":5.00}"

# Unknown customization group -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-badgroup" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"sauce\":\"ketchup\"}}],\"total_amount\":10.00}"

# Option not offered -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-badoption" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"gouda\"}}],\"total_amount\":10.00}"

# Required group omitted -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-missing" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{}}],\"total_amount\":10.00}"

# Two selections in a max_selection:1 group -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-toomany" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":[\"cheddar\",\"none\"]}}],\"total_amount\":11.50}"

# No token -> 401
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-anon" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"

# Restaurant owner trying to place an order -> 403 (valid token, wrong role)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-ownerorder" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"

# Restaurant with no menu -> 404
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-nomenu" \
  -d "{\"restaurant_id\":\"11111111-1111-1111-1111-111111111111\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1}],\"total_amount\":10.00}"
```

A customization can be given three ways — all equivalent:

```bash
"customizations": {"cheese": "cheddar"}                  # bare name
"customizations": {"cheese": ["cheddar"]}                # list of names
"customizations": {"cheese": {"name": "cheddar"}}        # option object
```

### 4.3 Fetch an order → `200`

The Payment Service reads an order this way — it cannot open `sfo_order_core` — so the
`total_amount` this returns is the figure a payment has to match:

```bash
curl -s "$BASE/api/v1/orders/$ORDER_ID" | field "['total_amount']"
```

Expect `27.0`. An unknown id is a `404`.

### 4.4 Read the tracking timeline → `200`

`order_tracking_logs` lives in this service's own database, beside `orders`. Creating the
order wrote its first entry in the same transaction, so the trail is never empty:

```bash
curl -s "$BASE/api/v1/orders/$ORDER_ID/logs" -H "Authorization: Bearer $CUSTOMER" | pretty
```

The first entry has `"status": "created"` and a null `previous_status`. Same rule as the
order itself: the customer who placed it, or a `system_admin`. An unknown id is a `404`.

### 4.5 Append a transition → `201`

Service-to-service, on the internal key rather than a bearer token — a customer must not be
able to write the record of their own order. The Order Service records its own transitions
in-process, so this endpoint is for siblings reporting one they observed:

```bash
curl -s -X POST "$BASE/api/v1/orders/logs" \
  -H "X-Internal-Key: $INTERNAL_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"order_id\":\"$ORDER_ID\",\"status\":\"confirmed\",\"service\":\"manual-test\",\"raw_log\":\"{}\",\"updated_by\":\"tester\",\"metadata\":{\"note\":\"first\"}}" | pretty
```

`previous_status` comes back as `created` — derived server-side from the preceding entry, so
a caller cannot report a transition that contradicts the recorded history.

| Scenario | Expected |
|---|---|
| Bearer token instead of `X-Internal-Key` | `401` |
| `order_id` that no order matches | `422` — the foreign key refuses it |
| A `status` the `order_status` enum does not define | `422` — the enum refuses it |

---

## 5. Payment Service (`:8005`)

A separate service with a separate database (`sfo_payment_core` on host port `5435`) and no
credentials for any other. It fetches the order over HTTP and refuses to charge an amount
that does not settle it exactly, so the authority on what an order costs stays with the
Order Service.

The card gateway is simulated in Week 1 — an authorised payment comes back with a
`ch_mock_…` reference rather than a real charge id.

> **This section changed in Week 2.** The saga now authorises payment as its first step, so
> there is normally nothing to call here. `POST /api/v1/payments` still exists for direct and
> manual use, but for an order the saga owns it returns `409` — `payments.order_id` is
> `UNIQUE`, and the workflow got there first. D30 records why.

### 5.1 Watch the saga authorise it

```bash
sleep 3
curl -s "$BASE/api/v1/orders/$ORDER_ID" -H "Authorization: Bearer $CUSTOMER" | pretty
# -> "status": "confirmed"
```

The stored payment reads `"status": "authorized"` with a `ch_mock_…`
`transaction_reference`, and its idempotency key is `wf-pay-$ORDER_ID` — derived from the
order rather than client-chosen, which is what makes a retried activity collide on the unique
index instead of charging twice. The row is written `pending` *before* the gateway is called
and moved to `authorized` after it answers, so a gateway failure leaves a `pending` row and
nothing charged. Since Week 2 those stranded rows are swept: the compensation path refunds
`pending` as well as `authorized`.

The customer no longer learns the payment id from any response, so read it from the database
when you need it by hand:

```bash
export PAYMENT_ID=$(docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core \
  -tA -c "SELECT id FROM payments WHERE order_id = '$ORDER_ID';" | tr -d '[:space:]')
echo "PAYMENT_ID=$PAYMENT_ID"
```

### 5.2 Paying by hand for a saga-owned order → `409`

```bash
export PAY_IDEM="pay-$RUN"
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -H "X-Idempotency-Key: $PAY_IDEM" \
  -d "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM\"}"
# -> 409 {"detail":"Order ... has already been paid for"}
```

One payment per order is a unique constraint, not a convention. `X-Idempotency-Key` is still
mandatory here **and** must equal the `idempotency_key` in the body, so the two `400`s in the
edge-case table below are unchanged.

### 5.3 Fetch a payment → `200`

```bash
curl -s "$BASE/api/v1/payments/$PAYMENT_ID" -H "Authorization: Bearer $CUSTOMER" | pretty
```

Ownership is settled by reading the order as the caller, so this answers `403` for someone
else's payment without the Payment Service holding an opinion about who owns what.

### Edge cases

| Scenario | Expected |
|---|---|
| `X-Idempotency-Key` header missing | `400` |
| Header disagrees with `idempotency_key` in the body | `400` |
| `order_id` unknown to the Order Service | `422` |
| `amount` does not equal the order's `total_amount` | `422` |
| `amount` not greater than zero | `422` |
| A different key against an order that is already paid | `409` |
| Unknown `payment_id` | `404` |
| Order Service is down | `503` |

```bash
# Missing header -> 400
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' \
  -d "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-x\"}"

# Header disagrees with the body -> 400
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $PAY_IDEM-header" \
  -d "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-body\"}"

# Unknown order -> 422 (an HTTP check, not a foreign key)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $PAY_IDEM-badorder" \
  -d "{\"order_id\":\"44444444-4444-4444-4444-444444444444\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-badorder\"}"

# Amount does not settle the order -> 422, and the message shows both figures
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $PAY_IDEM-short" \
  -d "{\"order_id\":\"$ORDER_ID\",\"amount\":1.00,\"idempotency_key\":\"$PAY_IDEM-short\"}"

# Order already paid, new key -> 409
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $PAY_IDEM-again" \
  -d "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-again\"}"

# Unknown payment -> 404
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/payments/00000000-0000-0000-0000-000000000000" \
  -H "Authorization: Bearer $CUSTOMER"
```

---

## 6. Service-boundary behaviour

These prove the services talk over HTTP rather than sharing tables. Stop a dependency and
the caller degrades to `503` instead of failing opaquely.

```bash
# Restaurant Service depends on the User Service
docker compose stop user-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Offline Diner","address":"9 Offline Road","latitude":1,"longitude":1,"capacity":5}'
docker compose start user-service

# Menu Service depends on the Restaurant Service
docker compose stop restaurant-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/menus" \
  -H "Authorization: Bearer $OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"restaurant_id":"00000000-0000-0000-0000-000000000000","categories":[]}'
docker compose start restaurant-service

# Order Service depends on the Menu Service
docker compose stop menu-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H 'X-Idempotency-Key: offline-test' \
  -d '{"restaurant_id":"00000000-0000-0000-0000-000000000000","items":[{"item_id":"x","quantity":1}],"total_amount":1.0}'
docker compose start menu-service

# Order Service depends on the User Service to verify the customer, because the customer
# lives in a database it cannot read. Needs a real cart so pricing gets past the Menu Service.
docker compose stop user-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H "Authorization: Bearer $CUSTOMER" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-nouser" \
  -d "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"
docker compose start user-service

# Payment Service depends on the Order Service, because the order it is settling lives in a
# database it cannot read — and it will not charge a card it cannot check the amount against.
docker compose stop order-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/payments" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $PAY_IDEM-nosvc" \
  -d "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-nosvc\"}"
docker compose start order-service
```

Each returns `503` with a message naming the unreachable service.

> After stopping and starting containers, Nginx may keep serving `502` because it cached the
> old upstream IP. Fix with `docker compose restart api-gateway`.

---

## 7. Verifying what was actually stored

The audit trail is written by the Order Service into its own database, in the same
transaction as the order — so a committed order always has a `created` entry, and the
foreign key guarantees no entry can point at an order that does not exist:

```bash
docker exec -it sfo-order-db psql -U sfo_order_admin -d sfo_order_core \
  -c "SELECT seq, old_status, new_status, service, updated_by FROM order_tracking_logs
      WHERE order_id = '$ORDER_ID' ORDER BY seq;"
```

The same trail over HTTP, for the customer who placed the order:

```bash
curl -s "$BASE/api/v1/orders/$ORDER_ID/logs" -H "Authorization: Bearer $CUSTOMER" | pretty
```

The order itself, with its priced snapshot, lives in the same database beside it:

```bash
docker exec -it sfo-order-db psql -U sfo_order_admin -d sfo_order_core \
  -c "SELECT id, status, total_amount, idempotency_key FROM orders ORDER BY created_at DESC LIMIT 5;"
```

The payment is one container over, in a database the Order Service has no credentials for:

```bash
docker exec -it sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core \
  -c "SELECT order_id, amount, status, transaction_reference FROM payments ORDER BY created_at DESC LIMIT 5;"
```

And the split is visible in the negative too — the `payments` table is simply not in the
order database any more:

```bash
# Fails: relation "payments" does not exist
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core \
  -c "SELECT count(*) FROM payments;"
```

If that query *succeeds*, the volume predates the split: `init.sql` only runs on an empty
volume, so the old table is still sitting there unused. `docker compose down -v` clears it.

Confirm the idempotent replay created no duplicate:

```bash
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA \
  -c "SELECT count(*) FROM orders WHERE idempotency_key = '$IDEM';"
```

Expect `1`.

Database-per-service is visible from the shell too — the customer named by that order is not
in this database at all, and the query that would have joined them cannot run:

```bash
# Fails: relation "users" does not exist
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core \
  -c "SELECT count(*) FROM users;"

# The customer lives here instead, one container over
docker exec -it sfo-user-db psql -U sfo_user_admin -d sfo_user_core \
  -c "SELECT id, email FROM users WHERE id = '$CUST_ID';"
```

---

## 8. The order saga (Week 2)

Everything above is one request at a time. This section drives a *process*: a Temporal
workflow that starts when the order commits and runs until the food arrives. It waits on
real people, so the commands below are interleaved with waiting — and every status check
needs a moment, because a saga is eventually consistent by design.

Watch it visually while you go: <http://localhost:8233>, workflow id `order-$ORDER_ID`. Every
activity, retry, timer and signal is in the history.

### 8.1 Set up a rider

A rider is a user with the `rider` role who then enrols in the fleet. Both steps take
identity from the token, so neither takes an id:

```bash
export RIDER_EMAIL="rider_$RUN@example.com"

curl -s -X POST "$BASE/api/v1/users/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$RIDER_EMAIL\",\"password\":\"Passw0rd!\",\"full_name\":\"Test Rider\",
       \"phone\":\"+1777$RANDOM\",\"role\":\"rider\"}" | pretty

export RIDER=$(curl -s -X POST "$BASE/api/v1/users/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$RIDER_EMAIL\",\"password\":\"Passw0rd!\"}" | field "['access_token']")

# Coordinates matter: dispatch measures distance from the restaurant, and a 10km radius
# applies. Use the same lat/long the restaurant was onboarded with.
export RIDER_ID=$(curl -s -X POST "$BASE/api/v1/riders" \
  -H "Authorization: Bearer $RIDER" -H 'Content-Type: application/json' \
  -d '{"vehicle_type":"motorbike","vehicle_number":"ISB-'$RANDOM'",
       "current_latitude":33.68,"current_longitude":73.04}' | field "['id']")
echo "RIDER_ID=$RIDER_ID"
```

| Scenario | Expected |
|---|---|
| A `customer` calling `POST /api/v1/riders` | `403` — wrong role in a valid token |
| The same account enrolling twice | `409` — names the account, not the vehicle |
| A duplicate `vehicle_number` | `409` — names the vehicle |
| `current_latitude` of `120` | `422` |
| Registering with no coordinates | `201`, but invisible to dispatch until a location is reported |

A rider who has never reported a location cannot be dispatched: the partial index behind the
search excludes null coordinates, because a rider whose position is unknown cannot be
measured against a restaurant.

```bash
curl -s -X PATCH "$BASE/api/v1/riders/me/location" \
  -H "Authorization: Bearer $RIDER" -H 'Content-Type: application/json' \
  -d '{"current_latitude":33.68,"current_longitude":73.04}' | pretty

curl -s "$BASE/api/v1/riders/me" -H "Authorization: Bearer $RIDER" | pretty
```

### 8.2 The kitchen's rail

Placing an order (section 4) puts a ticket on the restaurant's rail and parks the saga on a
durable timer. The owner reads their queue:

```bash
curl -s "$BASE/api/v1/orders/kitchen/$REST_ID" -H "Authorization: Bearer $OWNER" | pretty
```

On the **Order** Service, not the Restaurant Service: since D32 the kitchen's queue is a
query over `orders` rather than a table of its own. Note what the response does *not* carry —
no `total_amount`, no `customer_id`, no `idempotency_key`. Moving the queue deliberately did
not widen what a restaurant can read about a customer's order.

```bash
# Accept -> the saga starts looking for a rider
curl -s -X POST "$BASE/api/v1/orders/$ORDER_ID/accept" \
  -H "Authorization: Bearer $OWNER" | pretty

sleep 5
curl -s "$BASE/api/v1/orders/$ORDER_ID" -H "Authorization: Bearer $CUSTOMER" | pretty
# -> "status": "assigned", "rider_id": "..."
```

| Scenario | Expected |
|---|---|
| A `customer` accepting a ticket | `403` |
| An owner accepting *another* owner's ticket | `403` — checked before anything is revealed |
| Accepting twice | `200` with `"changed": false`, and the saga is **not** signalled again |
| Accepting after the 120s window | `200` on the ticket, but the order is already `cancelled` |
| Accepting *just before* the window closes, with the relay failing | The saga reads the ticket back on timeout and continues anyway — see 8.6 |
| Deciding an unknown order | `404` |
| Deciding an order the saga already cancelled | `200` with `"changed": false` — it stays cancelled |

**Capacity.** `restaurants.capacity` bounds how many orders may sit on one kitchen's rail —
that is, `status = 'confirmed' AND kitchen_decision IS NULL`. It is checked in the same
transaction that puts an order there, so two orders can never both take the last slot.
Onboard a restaurant with `"capacity": 1`, place two orders, and the second is refunded and
cancelled with `kitchen_at_capacity` in its trail — without ever reaching `confirmed`:

```bash
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -c \
  "SELECT new_status, metadata->>'reason' FROM order_tracking_logs
    WHERE order_id = '<SECOND_ORDER>' ORDER BY seq;"
# -> created | (null)
#    cancelled | kitchen_at_capacity
```

### 8.3 Riding it out

Only the rider whose own row records this order may report on it, which is why neither
endpoint takes a rider id:

```bash
curl -s -w '[%{http_code}]\n' -X POST "$BASE/api/v1/riders/me/orders/$ORDER_ID/picked-up" \
  -H "Authorization: Bearer $RIDER"          # -> 204

sleep 3
curl -s "$BASE/api/v1/orders/$ORDER_ID" -H "Authorization: Bearer $CUSTOMER" | field "['status']"
# -> picked_up

curl -s -w '[%{http_code}]\n' -X POST "$BASE/api/v1/riders/me/orders/$ORDER_ID/delivered" \
  -H "Authorization: Bearer $RIDER"          # -> 204

sleep 3
curl -s "$BASE/api/v1/orders/$ORDER_ID/logs" -H "Authorization: Bearer $CUSTOMER" | pretty
```

The trail shows the whole lifecycle, with each `previous_status` derived from the entry
before it rather than reported by anyone:

```
created -> confirmed -> assigned -> picked_up -> delivered
```

| Scenario | Expected |
|---|---|
| A different rider reporting this delivery | `403` — "you are not carrying order …" |
| Going off shift while carrying an order | `409` |
| Reporting on an order whose saga already finished | `404` from the signal relay |

The rider is released by the *saga*, in the same step that records `delivered` — not by the
delivery endpoint. That way availability and order state can never disagree:

```bash
docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core \
  -c "SELECT vehicle_number, is_available, current_order_id FROM riders WHERE id = '$RIDER_ID';"
```

### 8.4 Making it fail on purpose

The compensation paths are the interesting half. Each ends `cancelled` with the payment
`refunded`, the rider released and the ticket retired.

```bash
# Place a fresh order first (section 4), then:

# (a) The kitchen declines
curl -s -X POST "$BASE/api/v1/orders/$ORDER_ID/reject" \
  -H "Authorization: Bearer $OWNER" | pretty

# (b) The kitchen says nothing — wait out the 120s window and do nothing at all

# (c) Nobody is free to deliver: ground the fleet, then accept the ticket
docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core \
  -c "UPDATE riders SET is_available = FALSE;"
# ...the saga tries 6 times, 10s apart, then gives up (~60s)
```

Check the outcome of any of them:

```bash
curl -s "$BASE/api/v1/orders/$ORDER_ID" -H "Authorization: Bearer $CUSTOMER" | field "['status']"
# -> cancelled

docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core \
  -c "SELECT status, transaction_reference FROM payments WHERE order_id = '$ORDER_ID';"
# -> refunded, re_mock_...   (a distinct prefix, so a refund is never read as a charge)

docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core \
  -c "SELECT count(*) FROM riders WHERE current_order_id IS NOT NULL;"
# -> 0. A non-zero count after every saga has finished means a rider leaked.
```

Remember to put the fleet back after (c):

```bash
docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core \
  -c "UPDATE riders SET is_available = TRUE WHERE current_order_id IS NULL;"
```

### 8.5 The internal boundary

The saga's endpoints take `X-Internal-Key`, never a bearer token — a workflow has no user
behind it, and a token in a workflow argument would be written into durable, UI-visible
history (D26). Every one of these answers `401` with a user token, however privileged:

```bash
for p in "riders/dispatch" "riders/release" "payments/refund" "payments/authorize" \
         "orders/$ORDER_ID/signals"; do
  printf '%-34s ' "$p"
  curl -s -o /dev/null -w '[%{http_code}]\n' -X POST "$BASE/api/v1/$p" \
    -H "Authorization: Bearer $CUSTOMER" -H 'Content-Type: application/json' -d '{}'
done
```

With the key, the signal relay is the one door into a running saga:

```bash
export INTERNAL_KEY=$(grep -m1 '^INTERNAL_API_KEY=' .env | cut -d= -f2-)

# An invented signal name -> 422, rather than a signal Temporal accepts and nothing reads
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders/$ORDER_ID/signals" \
  -H "X-Internal-Key: $INTERNAL_KEY" -H 'Content-Type: application/json' \
  -d '{"signal":"teleported","payload":{}}'

# An order whose saga has finished -> 404, distinct from "no such order"
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders/$ORDER_ID/signals" \
  -H "X-Internal-Key: $INTERNAL_KEY" -H 'Content-Type: application/json' \
  -d '{"signal":"rider_pickup","payload":{}}'
```

### 8.6 Simulating a lost decision signal

The decision is committed *before* the saga is signalled, so a signal lost in flight leaves
an order whose `kitchen_decision` says `accepted` and a workflow still waiting. You can
reproduce that exactly by writing the decision straight into the database — no endpoint, no
signal:

```bash
# Place an order and let it reach 'confirmed' (sections 4 and 5), then:
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core \
  -c "UPDATE orders SET kitchen_decision='accepted', kitchen_decided_at=NOW()
       WHERE id='$ORDER_ID';"

# The saga has no idea. Watch it sit, then recover when its 120s timer fires:
docker exec sfo-temporal-server temporal workflow query \
  --address 127.0.0.1:7233 --workflow-id "order-$ORDER_ID" --type stage
# -> "stage":"awaiting_kitchen"     ...then, after the timeout:
# -> "stage":"dispatching_rider"    (recovered — NOT cancelled)

docker compose logs order-worker | grep "Recovered a lost kitchen decision"
```

The order proceeds to `assigned` and the payment stays `authorized` — no refund. Do the same
with `kitchen_decision='rejected'` and it cancels and refunds instead. Only a `NULL`
decision is treated as genuine silence.

### 8.7 Asking the saga where it is

A workflow query reads live state without touching the database — useful for "why is this
order stuck?":

```bash
docker exec sfo-temporal-server temporal workflow query \
  --address 127.0.0.1:7233 --workflow-id "order-$ORDER_ID" --type stage
# -> {"stage":"awaiting_kitchen","rider_id":null,...}

# The full history, including every retry and timer
docker exec sfo-temporal-server temporal workflow show \
  --address 127.0.0.1:7233 --workflow-id "order-$ORDER_ID"
```

---

## Status code reference

| Code | Meaning in this system |
|---|---|
| `200` | Read succeeded, or an idempotent replay returned the stored order or payment. Also a *business* answer the saga acts on rather than retries — a full kitchen (`queued: false`) or an empty fleet (`assigned: false`) |
| `201` | Resource created |
| `202` | A signal was delivered to a running saga — told, not necessarily acted on |
| `204` | Done, nothing to return: logout, and a rider reporting a pickup or delivery |
| `400` | Missing required header, or a role name absent from the `roles` table |
| `401` | No usable identity: no bearer token, or one malformed, expired or badly signed; a failed login; a spent refresh token; an internal endpoint reached without `X-Internal-Key`. Carries `WWW-Authenticate: Bearer` |
| `403` | Authenticated, but not permitted: the wrong role for the action, or someone else's user, order, payment, restaurant queue or delivery. Also what a downstream refusal becomes when a forwarded token is rejected |
| `404` | Resource does not exist, a restaurant is inactive, or an order has no running saga to signal |
| `409` | Unique constraint or state conflict — duplicate email, phone or vehicle number; an idempotency key race; an order that already has a payment (including one the saga paid for); a rider trying to go off shift mid-delivery |
| `422` | Well-formed but unsatisfiable: schema violation, pricing mismatch, unavailable item, payment that does not settle its order, an undefined order status, or an invented signal name |
| `500` | This service failed its own job (e.g. connection pool starved) |
| `502` | A dependency replied with something unusable |
| `503` | A dependency is unreachable — safe to retry |
