#!/usr/bin/env bash
#
# End-to-end smoke test for the SmartFoodOps stack.
#
# Drives every service through the API gateway exactly as a client would: the full
# happy path (register -> log in -> onboard -> publish menu -> checkout -> pay) plus every
# edge case named in the Week 1 contract. Asserts status codes and key response fields, and
# exits non-zero if anything regresses — so it is safe to wire into CI.
#
# Every call except health, register, login and refresh carries a bearer token. Identity is
# never sent in a request body: who you are is whatever your access token says.
#
# Usage:
#   ./scripts/smoke-test.sh              # run against http://localhost
#   ./scripts/smoke-test.sh --wait       # wait for services to come up first
#   ./scripts/smoke-test.sh --verbose    # print every response body
#   BASE_URL=http://host:8080 ./scripts/smoke-test.sh
#
# Requires: bash, curl, python3. Nothing needs to be installed in the containers.

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
VERBOSE=0
WAIT=0

for arg in "$@"; do
  case "$arg" in
    --verbose|-v) VERBOSE=1 ;;
    --wait|-w)    WAIT=1 ;;
    --help|-h)    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  GREEN=''; RED=''; DIM=''; BOLD=''; RESET=''
fi

PASSED=0
FAILED=0
FAILURES=()
BODY=""

section() { printf '\n%s%s%s\n' "$BOLD" "$1" "$RESET"; }
ok()      { PASSED=$((PASSED + 1)); printf '  %sPASS%s  %s\n' "$GREEN" "$RESET" "$1"; }
bad()     {
  FAILED=$((FAILED + 1))
  FAILURES+=("$1 — $2")
  printf '  %sFAIL%s  %s\n        %s%s%s\n' "$RED" "$RESET" "$1" "$DIM" "$2" "$RESET"
}

# Extract a field from the last response body, e.g. jfield "['id']"
jfield() {
  python3 -c "import json,sys; print(json.load(sys.stdin)$1)" <<<"$BODY" 2>/dev/null
}

# expect <name> <want-code> <method> <path> [json-body] [extra curl args...]
expect() {
  local name="$1" want="$2" method="$3" path="$4" body="${5:-}"
  if [[ $# -ge 5 ]]; then shift 5; else shift 4; fi

  local out code
  if [[ -n "$body" ]]; then
    out=$(curl -sS -m 20 -w $'\n%{http_code}' -X "$method" "$BASE_URL$path" \
          -H 'Content-Type: application/json' -d "$body" "$@" 2>&1)
  else
    out=$(curl -sS -m 20 -w $'\n%{http_code}' -X "$method" "$BASE_URL$path" "$@" 2>&1)
  fi

  code=$(tail -n1 <<<"$out")
  BODY=$(sed '$d' <<<"$out")

  if [[ "$code" == "$want" ]]; then
    ok "$name"
  else
    bad "$name" "expected HTTP $want, got $code: ${BODY:0:200}"
  fi
  (( VERBOSE )) && printf '        %s%s%s\n' "$DIM" "${BODY:0:400}" "$RESET"
  return 0
}

# assert <name> <actual> <expected>
assert() {
  if [[ "$2" == "$3" ]]; then
    ok "$1"
  else
    bad "$1" "expected '$3', got '$2'"
  fi
}

# Log in and publish the pair into ACCESS_TOKEN / REFRESH_TOKEN.
do_login() {
  local out
  out=$(curl -sS -m 20 -X POST "$BASE_URL/api/v1/users/login" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$1\",\"password\":\"$2\"}" 2>&1)
  ACCESS_TOKEN=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" <<<"$out" 2>/dev/null)
  REFRESH_TOKEN=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('refresh_token',''))" <<<"$out" 2>/dev/null)
}

wait_for_stack() {
  printf 'Waiting for services'
  for _ in $(seq 1 40); do
    local up=0
    for p in users restaurants menus orders payments; do
      [[ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$BASE_URL/api/v1/$p/health")" == "200" ]] && up=$((up + 1))
    done
    if [[ $up -eq 5 ]]; then printf ' ready.\n'; return 0; fi
    printf '.'; sleep 2
  done
  printf '\n%sServices did not become ready.%s Try: docker compose ps\n' "$RED" "$RESET"
  exit 1
}

(( WAIT )) && wait_for_stack

# Unique identities per run so repeated runs never trip the unique constraints.
TAG="smoke$(date +%s)$$"
PASSWORD="Passw0rd!"
OWNER_EMAIL="owner_${TAG}@example.com"
CUST_EMAIL="cust_${TAG}@example.com"
OTHER_EMAIL="other_${TAG}@example.com"
OWNER_PHONE="+1555${RANDOM}${RANDOM}"
CUST_PHONE="+1666${RANDOM}${RANDOM}"
OTHER_PHONE="+1444${RANDOM}${RANDOM}"

# The audit-log endpoint is service-to-service, so exercising it needs the shared key that
# docker-compose hands the Order Service. Read it from the same .env compose reads.
ENV_FILE="$(dirname "$0")/../.env"
INTERNAL_KEY=""
[[ -f "$ENV_FILE" ]] && INTERNAL_KEY=$(grep -m1 '^INTERNAL_API_KEY=' "$ENV_FILE" | cut -d= -f2-)

printf '%sSmartFoodOps smoke test%s  ->  %s\n' "$BOLD" "$RESET" "$BASE_URL"

# ---------------------------------------------------------------- health
# Health endpoints stay public: a probe must not need a credential.
section "Health"
for p in users restaurants menus orders payments; do
  expect "$p health" 200 GET "/api/v1/$p/health"
done

# ---------------------------------------------------------- authentication
section "Authentication"

expect "register restaurant owner" 201 POST /api/v1/users/register \
  "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$PASSWORD\",\"full_name\":\"Smoke Owner\",\"phone\":\"$OWNER_PHONE\",\"role\":\"restaurant_admin\"}"
OWNER_ID=$(jfield "['id']")
assert "  owner role resolved from roles table" "$(jfield "['role']")" "restaurant_admin"

expect "register customer" 201 POST /api/v1/users/register \
  "{\"email\":\"$CUST_EMAIL\",\"password\":\"$PASSWORD\",\"full_name\":\"Smoke Customer\",\"phone\":\"$CUST_PHONE\",\"role\":\"customer\"}"
CUST_ID=$(jfield "['id']")

expect "register second customer" 201 POST /api/v1/users/register \
  "{\"email\":\"$OTHER_EMAIL\",\"password\":\"$PASSWORD\",\"full_name\":\"Other Customer\",\"phone\":\"$OTHER_PHONE\",\"role\":\"customer\"}"
OTHER_ID=$(jfield "['id']")

expect "login with correct credentials" 200 POST /api/v1/users/login \
  "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$PASSWORD\"}"
assert "  token type is bearer" "$(jfield "['token_type']")" "bearer"

expect "wrong password -> 401" 401 POST /api/v1/users/login \
  "{\"email\":\"$OWNER_EMAIL\",\"password\":\"WrongPassword!\"}"
WRONG_PASS_MSG=$(jfield "['detail']")
expect "unknown email -> 401" 401 POST /api/v1/users/login \
  "{\"email\":\"nobody_${TAG}@example.com\",\"password\":\"$PASSWORD\"}"
assert "  same message either way (no user enumeration)" "$(jfield "['detail']")" "$WRONG_PASS_MSG"

do_login "$OWNER_EMAIL" "$PASSWORD"; OWNER_TOKEN="$ACCESS_TOKEN"
do_login "$CUST_EMAIL" "$PASSWORD";  CUST_TOKEN="$ACCESS_TOKEN"; CUST_REFRESH="$REFRESH_TOKEN"
do_login "$OTHER_EMAIL" "$PASSWORD"; OTHER_TOKEN="$ACCESS_TOKEN"

OWNER_AUTH=(-H "Authorization: Bearer $OWNER_TOKEN")
CUST_AUTH=(-H "Authorization: Bearer $CUST_TOKEN")
OTHER_AUTH=(-H "Authorization: Bearer $OTHER_TOKEN")

expect "no token -> 401" 401 GET "/api/v1/users/$OWNER_ID"
expect "malformed token -> 401" 401 GET "/api/v1/users/$OWNER_ID" "" \
  -H "Authorization: Bearer not-a-real-token"
expect "token with a broken signature -> 401" 401 GET "/api/v1/users/$OWNER_ID" "" \
  -H "Authorization: Bearer ${OWNER_TOKEN}tampered"

# ------------------------------------------------------------ happy path
section "Happy path"

expect "fetch own profile" 200 GET "/api/v1/users/$OWNER_ID" "" "${OWNER_AUTH[@]}"
assert "  returned the same user" "$(jfield "['id']")" "$OWNER_ID"

expect "onboard restaurant" 201 POST /api/v1/restaurants/onboard \
  "{\"name\":\"Smoke Diner\",\"address\":\"12 Test Street\",\"latitude\":33.68,\"longitude\":73.04,\"capacity\":40}" \
  "${OWNER_AUTH[@]}"
REST_ID=$(jfield "['id']")
assert "  restaurant defaults to active" "$(jfield "['is_active']")" "True"
assert "  owner taken from the token, not the body" "$(jfield "['owner_id']")" "$OWNER_ID"

expect "fetch restaurant by id" 200 GET "/api/v1/restaurants/$REST_ID" "" "${CUST_AUTH[@]}"

MENU=$(cat <<JSON
{"restaurant_id":"$REST_ID","categories":[{"category_id":"c1","category_name":"Mains","display_order":1,
"items":[
 {"item_id":"burger","name":"Burger","description":"Beef burger","base_price":10.00,"is_available":true,
  "customization_groups":[
    {"group_id":"cheese","group_name":"Cheese","min_selection":1,"max_selection":1,
     "options":[{"name":"cheddar","extra_price":1.50},{"name":"none","extra_price":0.0}]},
    {"group_id":"extras","group_name":"Extras","min_selection":0,"max_selection":2,
     "options":[{"name":"bacon","extra_price":2.00},{"name":"egg","extra_price":1.00}]}]},
 {"item_id":"soldout","name":"Sold Out Dish","description":"Unavailable","base_price":5.00,"is_available":false}
]}]}
JSON
)
expect "publish menu" 200 POST /api/v1/menus "$MENU" "${OWNER_AUTH[@]}"
expect "fetch published menu" 200 GET "/api/v1/menus/$REST_ID" "" "${CUST_AUTH[@]}"

# base 10.00 + cheddar 1.50 + bacon 2.00 = 13.50, x2 = 27.00
ORDER="{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":2,\"customizations\":{\"cheese\":\"cheddar\",\"extras\":[\"bacon\"]}}],\"total_amount\":27.00}"
IDEM="idem-$TAG"

expect "create order" 201 POST /api/v1/orders "$ORDER" \
  -H "X-Idempotency-Key: $IDEM" "${CUST_AUTH[@]}"
ORDER_ID=$(jfield "['id']")
assert "  customer taken from the token, not the body" "$(jfield "['customer_id']")" "$CUST_ID"
assert "  server-recalculated unit price" "$(jfield "['items'][0]['unit_price']")" "13.5"
assert "  server-recalculated total"      "$(jfield "['total_amount']")"          "27.0"
assert "  initial status is 'created'"    "$(jfield "['status']")"                "created"

expect "replay same idempotency key -> 200" 200 POST /api/v1/orders "$ORDER" \
  -H "X-Idempotency-Key: $IDEM" "${CUST_AUTH[@]}"
assert "  replay returned the SAME order (no duplicate)" "$(jfield "['id']")" "$ORDER_ID"

# The Payment Service reads the order over HTTP, so this endpoint is part of the contract.
expect "fetch order by id" 200 GET "/api/v1/orders/$ORDER_ID" "" "${CUST_AUTH[@]}"
assert "  returned the same order" "$(jfield "['id']")" "$ORDER_ID"

# Pay for it — a separate service, a separate database, verified over HTTP.
PAY_IDEM="pay-$TAG"
PAYMENT="{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM\"}"

expect "authorise payment" 201 POST /api/v1/payments "$PAYMENT" \
  -H "X-Idempotency-Key: $PAY_IDEM" "${CUST_AUTH[@]}"
PAYMENT_ID=$(jfield "['id']")
assert "  payment reached 'authorized'" "$(jfield "['status']")" "authorized"
assert "  amount settles the order"     "$(jfield "['amount']")" "27.0"
assert "  gateway reference recorded"   "$(jfield "['transaction_reference'][:8]")" "ch_mock_"

expect "fetch payment by id" 200 GET "/api/v1/payments/$PAYMENT_ID" "" "${CUST_AUTH[@]}"
assert "  returned the same payment" "$(jfield "['id']")" "$PAYMENT_ID"

expect "replay same payment key -> 200" 200 POST /api/v1/payments "$PAYMENT" \
  -H "X-Idempotency-Key: $PAY_IDEM" "${CUST_AUTH[@]}"
assert "  replay returned the SAME payment (no double charge)" "$(jfield "['id']")" "$PAYMENT_ID"

# ------------------------------------------------------- authorisation
# One user must not be able to reach another's records, and a role must not be able to do
# another role's job. These are the checks that used to be impossible: identity arrived in
# the request body, so anyone could claim to be anyone.
section "Authorisation boundaries"

expect "reading another user's profile -> 403" 403 GET "/api/v1/users/$CUST_ID" "" "${OWNER_AUTH[@]}"
expect "customer cannot onboard a restaurant -> 403" 403 POST /api/v1/restaurants/onboard \
  "{\"name\":\"Customer Diner\",\"address\":\"2 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":5}" \
  "${CUST_AUTH[@]}"
expect "customer cannot publish a menu -> 403" 403 POST /api/v1/menus "$MENU" "${CUST_AUTH[@]}"
expect "restaurant owner cannot place an order -> 403" 403 POST /api/v1/orders "$ORDER" \
  -H "X-Idempotency-Key: $IDEM-ownerorder" "${OWNER_AUTH[@]}"
expect "reading another customer's order -> 403" 403 GET "/api/v1/orders/$ORDER_ID" "" "${OTHER_AUTH[@]}"
expect "paying for another customer's order -> 403" 403 POST /api/v1/payments \
  "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-steal\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-steal" "${OTHER_AUTH[@]}"
expect "reading another customer's payment -> 403" 403 GET "/api/v1/payments/$PAYMENT_ID" "" "${OTHER_AUTH[@]}"

# ------------------------------------------------------- session lifecycle
section "Session lifecycle"

expect "refresh returns a new pair" 200 POST /api/v1/users/refresh \
  "{\"refresh_token\":\"$CUST_REFRESH\"}"
ROTATED_REFRESH=$(jfield "['refresh_token']")
ROTATED_ACCESS=$(jfield "['access_token']")
assert "  refresh token was rotated" "$([[ "$ROTATED_REFRESH" != "$CUST_REFRESH" ]] && echo yes)" "yes"

expect "the consumed refresh token is dead -> 401" 401 POST /api/v1/users/refresh \
  "{\"refresh_token\":\"$CUST_REFRESH\"}"
expect "the rotated access token works" 200 GET "/api/v1/users/$CUST_ID" "" \
  -H "Authorization: Bearer $ROTATED_ACCESS"

expect "logout" 204 POST /api/v1/users/logout \
  "{\"refresh_token\":\"$ROTATED_REFRESH\"}" -H "Authorization: Bearer $ROTATED_ACCESS"
expect "refreshing after logout -> 401" 401 POST /api/v1/users/refresh \
  "{\"refresh_token\":\"$ROTATED_REFRESH\"}"
expect "logout without a token -> 401" 401 POST /api/v1/users/logout \
  "{\"refresh_token\":\"$ROTATED_REFRESH\"}"

# --------------------------------------------------------- user service
section "User Service edge cases"
expect "unknown role -> 400" 400 POST /api/v1/users/register \
  "{\"email\":\"x_${TAG}@example.com\",\"password\":\"$PASSWORD\",\"full_name\":\"Bad Role\",\"phone\":\"+1777${RANDOM}\",\"role\":\"wizard\"}"
expect "duplicate email -> 409" 409 POST /api/v1/users/register \
  "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$PASSWORD\",\"full_name\":\"Dup Email\",\"phone\":\"+1888${RANDOM}${RANDOM}\",\"role\":\"customer\"}"
expect "duplicate phone -> 409" 409 POST /api/v1/users/register \
  "{\"email\":\"dup_${TAG}@example.com\",\"password\":\"$PASSWORD\",\"full_name\":\"Dup Phone\",\"phone\":\"$OWNER_PHONE\",\"role\":\"customer\"}"
expect "short password -> 422" 422 POST /api/v1/users/register \
  "{\"email\":\"short_${TAG}@example.com\",\"password\":\"abc\",\"full_name\":\"Short Pass\",\"phone\":\"+1999${RANDOM}\",\"role\":\"customer\"}"
# 403 rather than 404: the ownership check runs before the lookup, so a stranger's id and a
# nonexistent id are indistinguishable from outside. That is deliberate.
expect "someone else's user id -> 403" 403 GET /api/v1/users/00000000-0000-0000-0000-000000000000 \
  "" "${OWNER_AUTH[@]}"

# --------------------------------------------------- restaurant service
section "Restaurant Service edge cases"
expect "latitude out of range -> 422" 422 POST /api/v1/restaurants/onboard \
  "{\"name\":\"Bad Coords\",\"address\":\"3 Nowhere Road\",\"latitude\":999,\"longitude\":10,\"capacity\":5}" \
  "${OWNER_AUTH[@]}"
expect "capacity must be positive -> 422" 422 POST /api/v1/restaurants/onboard \
  "{\"name\":\"Zero Cap\",\"address\":\"4 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":0}" \
  "${OWNER_AUTH[@]}"
expect "unknown restaurant id -> 404" 404 GET /api/v1/restaurants/00000000-0000-0000-0000-000000000000 \
  "" "${CUST_AUTH[@]}"

# --------------------------------------------------------- menu service
section "Menu Service edge cases"
expect "menu for unknown restaurant -> 404" 404 POST /api/v1/menus \
  "{\"restaurant_id\":\"00000000-0000-0000-0000-000000000000\",\"categories\":[]}" \
  "${OWNER_AUTH[@]}"
expect "min_selection > max_selection -> 422" 422 POST /api/v1/menus \
  "{\"restaurant_id\":\"$REST_ID\",\"categories\":[{\"category_id\":\"c\",\"category_name\":\"C\",\"items\":[{\"item_id\":\"i\",\"name\":\"I\",\"description\":\"d\",\"base_price\":1.0,\"customization_groups\":[{\"group_id\":\"g\",\"group_name\":\"G\",\"min_selection\":3,\"max_selection\":1,\"options\":[{\"name\":\"a\"}]}]}]}]}" \
  "${OWNER_AUTH[@]}"
expect "no menu published -> 404" 404 GET /api/v1/menus/11111111-1111-1111-1111-111111111111 \
  "" "${CUST_AUTH[@]}"

# -------------------------------------------------------- order service
section "Order Service edge cases"
expect "missing idempotency key -> 400" 400 POST /api/v1/orders "$ORDER" "${CUST_AUTH[@]}"
expect "total_amount mismatch -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"cheddar\"}}],\"total_amount\":1.00}" \
  -H "X-Idempotency-Key: $IDEM-mismatch" "${CUST_AUTH[@]}"
expect "unavailable item -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"soldout\",\"quantity\":1}],\"total_amount\":5.00}" \
  -H "X-Idempotency-Key: $IDEM-soldout" "${CUST_AUTH[@]}"
expect "item not on menu -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"nope\",\"quantity\":1}],\"total_amount\":5.00}" \
  -H "X-Idempotency-Key: $IDEM-unknown" "${CUST_AUTH[@]}"
expect "unknown customization group -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"ghost\":\"x\"}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-badgroup" "${CUST_AUTH[@]}"
expect "option not offered -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"gouda\"}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-badoption" "${CUST_AUTH[@]}"
expect "required group omitted -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-missinggrp" "${CUST_AUTH[@]}"
expect "too many selections -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":[\"cheddar\",\"none\"]}}],\"total_amount\":11.50}" \
  -H "X-Idempotency-Key: $IDEM-toomany" "${CUST_AUTH[@]}"
expect "restaurant has no menu -> 404" 404 POST /api/v1/orders \
  "{\"restaurant_id\":\"11111111-1111-1111-1111-111111111111\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-nomenu" "${CUST_AUTH[@]}"
expect "empty item list -> 422" 422 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-empty" "${CUST_AUTH[@]}"
expect "option object form is accepted" 201 POST /api/v1/orders \
  "{\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":{\"name\":\"cheddar\"}}}],\"total_amount\":11.50}" \
  -H "X-Idempotency-Key: $IDEM-dictform" "${CUST_AUTH[@]}"
SECOND_ORDER_ID=$(jfield "['id']")
expect "unknown order id -> 404" 404 GET /api/v1/orders/00000000-0000-0000-0000-000000000000 \
  "" "${CUST_AUTH[@]}"

# `order_tracking_logs` moved out of MongoDB and into this service's own database, so the
# audit endpoint moved with it: POST /api/v1/menus/logs is now POST /api/v1/orders/logs.
# It stays service-to-service — a customer holding a bearer token must not be able to write
# the record of their own order.
expect "audit log rejects an end-user token -> 401" 401 POST /api/v1/orders/logs \
  "{\"order_id\":\"$ORDER_ID\",\"status\":\"confirmed\",\"service\":\"smoke-test\",\"raw_log\":\"{}\",\"updated_by\":\"tester\"}" \
  "${CUST_AUTH[@]}"

if [[ -n "$INTERNAL_KEY" ]]; then
  expect "audit log appends a transition -> 201" 201 POST /api/v1/orders/logs \
    "{\"order_id\":\"$ORDER_ID\",\"status\":\"confirmed\",\"service\":\"smoke-test\",\"raw_log\":\"{}\",\"updated_by\":\"tester\"}" \
    -H "X-Internal-Key: $INTERNAL_KEY"
  # Checkout opened the trail in the same transaction as the order, so the entry before
  # this one is the 'created' the Order Service wrote for itself.
  assert "  previous status read from the preceding entry" "$(jfield "['previous_status']")" "created"
  # Both rejections below come from the engine, not from application checks: the foreign
  # key knows which orders exist and the order_status enum knows which statuses do. Neither
  # was possible while this was a MongoDB collection.
  expect "log against an unknown order -> 422" 422 POST /api/v1/orders/logs \
    "{\"order_id\":\"00000000-0000-0000-0000-000000000000\",\"status\":\"confirmed\",\"service\":\"smoke-test\",\"raw_log\":\"{}\"}" \
    -H "X-Internal-Key: $INTERNAL_KEY"
  expect "log with a status the enum does not define -> 422" 422 POST /api/v1/orders/logs \
    "{\"order_id\":\"$ORDER_ID\",\"status\":\"teleported\",\"service\":\"smoke-test\",\"raw_log\":\"{}\"}" \
    -H "X-Internal-Key: $INTERNAL_KEY"
else
  printf '  %sSKIP%s  audit-log writes (INTERNAL_API_KEY not readable from .env)\n' "$DIM" "$RESET"
fi

expect "read the order's tracking timeline" 200 GET "/api/v1/orders/$ORDER_ID/logs" "" "${CUST_AUTH[@]}"
assert "  trail opens with the checkout transition" "$(jfield "[0]['status']")" "created"
assert "  the first entry has no predecessor" "$(jfield "[0]['previous_status']")" "None"
expect "another customer cannot read the timeline -> 403" 403 GET "/api/v1/orders/$ORDER_ID/logs" \
  "" "${OTHER_AUTH[@]}"
expect "timeline for an unknown order -> 404" 404 \
  GET /api/v1/orders/00000000-0000-0000-0000-000000000000/logs "" "${CUST_AUTH[@]}"

# ------------------------------------------------------ payment service
section "Payment Service edge cases"
expect "missing idempotency header -> 400" 400 POST /api/v1/payments "$PAYMENT" "${CUST_AUTH[@]}"
expect "header disagrees with body -> 400" 400 POST /api/v1/payments "$PAYMENT" \
  -H "X-Idempotency-Key: $PAY_IDEM-other" "${CUST_AUTH[@]}"
expect "unknown order -> 422" 422 POST /api/v1/payments \
  "{\"order_id\":\"44444444-4444-4444-4444-444444444444\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-badorder\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-badorder" "${CUST_AUTH[@]}"
expect "amount does not settle the order -> 422" 422 POST /api/v1/payments \
  "{\"order_id\":\"$SECOND_ORDER_ID\",\"amount\":1.00,\"idempotency_key\":\"$PAY_IDEM-short\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-short" "${CUST_AUTH[@]}"
expect "non-positive amount -> 422" 422 POST /api/v1/payments \
  "{\"order_id\":\"$SECOND_ORDER_ID\",\"amount\":0,\"idempotency_key\":\"$PAY_IDEM-zero\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-zero" "${CUST_AUTH[@]}"
expect "second payment for a paid order -> 409" 409 POST /api/v1/payments \
  "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-again\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-again" "${CUST_AUTH[@]}"
expect "unknown payment id -> 404" 404 GET /api/v1/payments/00000000-0000-0000-0000-000000000000 \
  "" "${CUST_AUTH[@]}"

# ---------------------------------------------- cross-service integration
section "Cross-service integration"

# MongoDB is gone. The `menus` collection became a table in the Menu Service's own
# database, and `order_tracking_logs` went the other way, into the Order Service's — prove
# each one landed where it was supposed to, in the database only its owner can reach.
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^sfo-menu-db$'; then
  STORED_ITEMS=$(docker exec sfo-menu-db psql -U sfo_menu_admin -d sfo_menu_core -tA \
    -c "SELECT jsonb_array_length(categories -> 0 -> 'items') FROM menus WHERE restaurant_id = '$REST_ID';" 2>/dev/null | tr -d '\r')
  assert "  menu stored in sfo_menu_core as a JSONB tree" "$STORED_ITEMS" "2"
else
  printf '  %sSKIP%s  menu database check (docker/sfo-menu-db not reachable)\n' "$DIM" "$RESET"
fi

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^sfo-order-db$'; then
  # Checkout writes the order and its opening entry in one transaction, so 'created' is
  # there whether or not the internal key was available to append the second one.
  EXPECTED_TRAIL="created"
  [[ -n "$INTERNAL_KEY" ]] && EXPECTED_TRAIL="created,confirmed"
  TRAIL=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA \
    -c "SELECT string_agg(new_status::text, ',' ORDER BY seq) FROM order_tracking_logs WHERE order_id = '$ORDER_ID';" 2>/dev/null | tr -d '\r')
  assert "  tracking trail stored in sfo_order_core" "$TRAIL" "$EXPECTED_TRAIL"
  # The foreign key the MongoDB collection could not have. Rejected by the engine, not by
  # any check the application makes.
  ORPHAN=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA \
    -c "INSERT INTO order_tracking_logs (order_id, new_status, service) VALUES (uuid_generate_v4(), 'created', 'smoke');" 2>&1 | grep -c 'violates foreign key constraint')
  assert "  a log against a nonexistent order is refused by the engine" "$ORPHAN" "1"
else
  printf '  %sSKIP%s  tracking-log database checks (docker/sfo-order-db not reachable)\n' "$DIM" "$RESET"
fi

# Cache-aside: reads populate Redis, and publishing drops the copy. Without the second
# half, customers keep being quoted the previous prices until the TTL lapses.
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^sfo-redis$'; then
  expect "fetch the menu again (populates the cache)" 200 GET "/api/v1/menus/$REST_ID" "" "${CUST_AUTH[@]}"
  CACHED=$(docker exec sfo-redis redis-cli EXISTS "menu:$REST_ID" 2>/dev/null | tr -d '\r')
  assert "  the read populated the Redis cache" "$CACHED" "1"
  expect "republish the menu" 200 POST /api/v1/menus "$MENU" "${OWNER_AUTH[@]}"
  CACHED_AFTER=$(docker exec sfo-redis redis-cli EXISTS "menu:$REST_ID" 2>/dev/null | tr -d '\r')
  assert "  publishing invalidated the cached copy" "$CACHED_AFTER" "0"
  expect "the menu still serves with the cache cold" 200 GET "/api/v1/menus/$REST_ID" "" "${CUST_AUTH[@]}"
else
  printf '  %sSKIP%s  menu cache checks (docker/sfo-redis not reachable)\n' "$DIM" "$RESET"
fi

# The payments table moved out of the order database entirely — prove both halves of that.
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^sfo-payment-db$'; then
  STORED=$(docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core -tA \
    -c "SELECT status FROM payments WHERE order_id = '$ORDER_ID';" 2>/dev/null | tr -d '\r')
  assert "  payment stored in sfo_payment_core" "$STORED" "authorized"
  ABSENT=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA \
    -c "SELECT to_regclass('public.payments') IS NULL;" 2>/dev/null | tr -d '\r')
  assert "  order database no longer holds a payments table" "$ABSENT" "t"
else
  printf '  %sSKIP%s  payment database checks (docker/sfo-payment-db not reachable)\n' "$DIM" "$RESET"
fi

# Only the User Service may hold the signing key. If that stops being true, tokens can be
# minted anywhere and the asymmetric split has quietly become decorative.
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^sfo-order-service$'; then
  LEAKED=$(docker exec sfo-order-service printenv JWT_PRIVATE_KEY_B64 2>/dev/null)
  assert "  private key is absent from the Order Service" "${LEAKED:-absent}" "absent"
else
  printf '  %sSKIP%s  signing-key isolation check (sfo-order-service not reachable)\n' "$DIM" "$RESET"
fi

# ------------------------------------------------------------- summary
section "Summary"
printf '  %s%d passed%s' "$GREEN" "$PASSED" "$RESET"
if [[ $FAILED -gt 0 ]]; then
  printf ', %s%d failed%s\n\n' "$RED" "$FAILED" "$RESET"
  for f in "${FAILURES[@]}"; do printf '  %s- %s%s\n' "$RED" "$f" "$RESET"; done
  echo
  exit 1
fi
printf ', %s0 failed%s\n\n' "$GREEN" "$RESET"
exit 0
