# SmartFoodOps — Manual API Testing Guide

Copy-pasteable `curl` commands for exercising every service by hand, grouped by service and
ordered so that IDs captured early feed the calls that follow.

For an automated pass instead, run [`scripts/smoke-test.sh`](../scripts/smoke-test.sh) — it
covers everything below with assertions and an exit code. Use this document when you want to
poke at one endpoint, see a raw response, or demo the flow.

All traffic goes through the Nginx gateway on port **80**; no service port is published by
default.

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

---

## 0. Health

Every service exposes a health endpoint that also round-trips its datastore.

```bash
for p in users restaurants menus orders; do
  printf '%-14s ' "$p"
  curl -s -w ' [%{http_code}]\n' "$BASE/api/v1/$p/health"
done
```

All four return `200`. A `200` carrying `"database_reachable": false` means the app is up but
its store is not — check `docker compose ps`.

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

### 1.3 Fetch a user → `200`

This is the endpoint the Restaurant Service calls instead of reading the `users` table.

```bash
curl -s "$BASE/api/v1/users/$OWNER_ID" | pretty
```

### Edge cases

| Scenario | Expected |
|---|---|
| Unknown role name | `400` |
| Email already registered | `409` |
| Phone already registered | `409` |
| Password shorter than 8 chars | `422` |
| User id not in the database | `404` |

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

# Unknown user -> 404
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/users/00000000-0000-0000-0000-000000000000"
```

---

## 2. Restaurant Service (`:8002`)

### 2.1 Onboard a restaurant → `201`

The owner is verified over HTTP against the User Service first.

```bash
export REST_ID=$(curl -s -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d "{
    \"owner_id\": \"$OWNER_ID\",
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
curl -s "$BASE/api/v1/restaurants/$REST_ID" | pretty
```

### Edge cases

| Scenario | Expected |
|---|---|
| `owner_id` not a known user | `404` |
| Owner exists but is not `restaurant_admin` | `403` |
| Latitude outside −90…90 (or longitude −180…180) | `422` |
| `capacity` of 0 or less | `422` |
| Restaurant id not in the database | `404` |
| User Service is down | `503` |

```bash
# Unknown owner -> 404
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d '{"owner_id":"00000000-0000-0000-0000-000000000000","name":"Ghost Diner","address":"1 Nowhere Road","latitude":10,"longitude":10,"capacity":5}'

# Customer trying to onboard -> 403
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d "{\"owner_id\":\"$CUST_ID\",\"name\":\"Customer Diner\",\"address\":\"2 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":5}"

# Latitude out of range -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d "{\"owner_id\":\"$OWNER_ID\",\"name\":\"Bad Coords\",\"address\":\"3 Nowhere Road\",\"latitude\":999,\"longitude\":10,\"capacity\":5}"

# Non-positive capacity -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d "{\"owner_id\":\"$OWNER_ID\",\"name\":\"Zero Capacity\",\"address\":\"4 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":0}"

# Unknown restaurant -> 404
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/restaurants/00000000-0000-0000-0000-000000000000"
```

---

## 3. Menu Service (`:8003`)

### 3.1 Publish a menu → `200`

An upsert: calling it again replaces the whole category tree for that restaurant.

```bash
curl -s -X POST "$BASE/api/v1/menus" \
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
curl -s "$BASE/api/v1/menus/$REST_ID" | pretty
```

### 3.3 Append an order audit log → `201`

The Order Service posts here rather than writing to MongoDB itself.

```bash
export LOG_ORDER=$(python3 -c 'import uuid; print(uuid.uuid4())')

curl -s -X POST "$BASE/api/v1/menus/logs" \
  -H 'Content-Type: application/json' \
  -d "{\"order_id\":\"$LOG_ORDER\",\"status\":\"created\",\"service\":\"manual-test\",\"raw_log\":\"{}\",\"updated_by\":\"tester\",\"metadata\":{\"note\":\"first\"}}" | pretty
```

`created_document` is `true` the first time. Post again with a different `status` and it
turns `false` — the entry is appended to the same document's `status_history`:

```bash
curl -s -X POST "$BASE/api/v1/menus/logs" \
  -H 'Content-Type: application/json' \
  -d "{\"order_id\":\"$LOG_ORDER\",\"status\":\"confirmed\",\"service\":\"manual-test\",\"raw_log\":\"{}\"}" | pretty
```

### Edge cases

| Scenario | Expected |
|---|---|
| `restaurant_id` unknown or inactive | `404` |
| `min_selection` greater than `max_selection` | `422` |
| `base_price` of 0 or less | `422` |
| No menu published for that restaurant | `404` |
| Restaurant Service is down | `503` |
| MongoDB is down | `503` |

```bash
# Unknown restaurant -> 404 (verified via the Restaurant Service, not the database)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/menus" \
  -H 'Content-Type: application/json' \
  -d '{"restaurant_id":"00000000-0000-0000-0000-000000000000","categories":[]}'

# Unsatisfiable customization group -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/menus" \
  -H 'Content-Type: application/json' \
  -d "{\"restaurant_id\":\"$REST_ID\",\"categories\":[{\"category_id\":\"c\",\"category_name\":\"C\",\"items\":[{\"item_id\":\"i\",\"name\":\"I\",\"description\":\"d\",\"base_price\":1.0,\"customization_groups\":[{\"group_id\":\"g\",\"group_name\":\"G\",\"min_selection\":3,\"max_selection\":1,\"options\":[{\"name\":\"a\"}]}]}]}]}"

# No menu published -> 404
curl -s -w '\n[%{http_code}]\n' "$BASE/api/v1/menus/11111111-1111-1111-1111-111111111111"
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
  -H 'Content-Type: application/json' \
  -H "X-Idempotency-Key: $IDEM" \
  -d "{
    \"customer_id\": \"$CUST_ID\",
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
  -H 'Content-Type: application/json' \
  -H "X-Idempotency-Key: $IDEM" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":2,\"customizations\":{\"cheese\":\"cheddar\",\"extras\":[\"bacon\"]}}],\"total_amount\":27.00}"
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
| `customer_id` unknown to the User Service | `422` |
| `restaurant_id` unknown to the Restaurant Service | `422` |
| Restaurant has no published menu | `404` |
| Menu, User or Restaurant Service is down | `503` |

```bash
# Missing idempotency key -> 400
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"

# Under-claimed total -> 422, and the message shows both numbers
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-mismatch" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"cheddar\"}}],\"total_amount\":1.00}"

# Unavailable item -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-soldout" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"soldout\",\"quantity\":1}],\"total_amount\":5.00}"

# Item not on the menu -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-unknown" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"pizza\",\"quantity\":1}],\"total_amount\":5.00}"

# Unknown customization group -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-badgroup" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"sauce\":\"ketchup\"}}],\"total_amount\":10.00}"

# Option not offered -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-badoption" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"gouda\"}}],\"total_amount\":10.00}"

# Required group omitted -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-missing" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{}}],\"total_amount\":10.00}"

# Two selections in a max_selection:1 group -> 422
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-toomany" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":[\"cheddar\",\"none\"]}}],\"total_amount\":11.50}"

# Customer unknown to the User Service -> 422
# (the Order Service has its own database, so this is an HTTP check, not a foreign key)
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-badcust" \
  -d "{\"customer_id\":\"33333333-3333-3333-3333-333333333333\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"

# Restaurant with no menu -> 404
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-nomenu" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"11111111-1111-1111-1111-111111111111\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1}],\"total_amount\":10.00}"
```

A customization can be given three ways — all equivalent:

```bash
"customizations": {"cheese": "cheddar"}                  # bare name
"customizations": {"cheese": ["cheddar"]}                # list of names
"customizations": {"cheese": {"name": "cheddar"}}        # option object
```

---

## 5. Service-boundary behaviour

These prove the services talk over HTTP rather than sharing tables. Stop a dependency and
the caller degrades to `503` instead of failing opaquely.

```bash
# Restaurant Service depends on the User Service
docker compose stop user-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/restaurants/onboard" \
  -H 'Content-Type: application/json' \
  -d '{"owner_id":"00000000-0000-0000-0000-000000000000","name":"Offline Diner","address":"9 Offline Road","latitude":1,"longitude":1,"capacity":5}'
docker compose start user-service

# Menu Service depends on the Restaurant Service
docker compose stop restaurant-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/menus" \
  -H 'Content-Type: application/json' \
  -d '{"restaurant_id":"00000000-0000-0000-0000-000000000000","categories":[]}'
docker compose start restaurant-service

# Order Service depends on the Menu Service
docker compose stop menu-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H 'X-Idempotency-Key: offline-test' \
  -d '{"customer_id":"00000000-0000-0000-0000-000000000000","restaurant_id":"00000000-0000-0000-0000-000000000000","items":[{"item_id":"x","quantity":1}],"total_amount":1.0}'
docker compose start menu-service

# Order Service depends on the User Service to verify the customer, because the customer
# lives in a database it cannot read. Needs a real cart so pricing gets past the Menu Service.
docker compose stop user-service
curl -s -w '\n[%{http_code}]\n' -X POST "$BASE/api/v1/orders" \
  -H 'Content-Type: application/json' -H "X-Idempotency-Key: $IDEM-nouser" \
  -d "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}"
docker compose start user-service
```

Each returns `503` with a message naming the unreachable service.

> After stopping and starting containers, Nginx may keep serving `502` because it cached the
> old upstream IP. Fix with `docker compose restart api-gateway`.

---

## 6. Verifying what was actually stored

The order audit log is written by the Order Service **through** the Menu Service, so it
should be in MongoDB even though `order-service` never opens a Mongo connection:

```bash
docker exec sfo-mongodb mongosh smartfoodops_menus --quiet \
  --eval "JSON.stringify(db.order_tracking_logs.findOne({order_id:'$ORDER_ID'}), null, 2)"
```

The order itself, with its priced snapshot, lives in the Order Service's own database —
`sfo-order-db`, which holds `orders` and `payments` and nothing else:

```bash
docker exec -it sfo-order-db psql -U sfo_order_admin -d sfo_order_core \
  -c "SELECT id, status, total_amount, idempotency_key FROM orders ORDER BY created_at DESC LIMIT 5;"
```

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

## Status code reference

| Code | Meaning in this system |
|---|---|
| `200` | Read succeeded, or an idempotent replay returned the stored order |
| `201` | Resource created |
| `400` | Missing required header, or a role name absent from the `roles` table |
| `403` | Authenticated subject exists but lacks the required role |
| `404` | Resource does not exist, or a restaurant is inactive |
| `409` | Unique constraint hit — duplicate email, phone, or idempotency key race |
| `422` | Well-formed but unsatisfiable: schema violation, pricing mismatch, unavailable item |
| `500` | This service failed its own job (e.g. connection pool starved) |
| `502` | A dependency replied with something unusable |
| `503` | A dependency is unreachable — safe to retry |
