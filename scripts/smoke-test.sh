#!/usr/bin/env bash
#
# End-to-end smoke test for the SmartFoodOps stack.
#
# Drives every service through the API gateway exactly as a client would: the full
# happy path (register -> onboard -> publish menu -> checkout -> pay) plus every edge
# case named in the Week 1 contract. Asserts status codes and key response fields, and
# exits non-zero if anything regresses — so it is safe to wire into CI.
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
OWNER_EMAIL="owner_${TAG}@example.com"
CUST_EMAIL="cust_${TAG}@example.com"
OWNER_PHONE="+1555${RANDOM}${RANDOM}"
CUST_PHONE="+1666${RANDOM}${RANDOM}"

printf '%sSmartFoodOps smoke test%s  ->  %s\n' "$BOLD" "$RESET" "$BASE_URL"

# ---------------------------------------------------------------- health
section "Health"
for p in users restaurants menus orders payments; do
  expect "$p health" 200 GET "/api/v1/$p/health"
done

# ------------------------------------------------------------ happy path
section "Happy path"

expect "register restaurant owner" 201 POST /api/v1/users/register \
  "{\"email\":\"$OWNER_EMAIL\",\"password\":\"Passw0rd!\",\"full_name\":\"Smoke Owner\",\"phone\":\"$OWNER_PHONE\",\"role\":\"restaurant_admin\"}"
OWNER_ID=$(jfield "['id']")
assert "  owner role resolved from roles table" "$(jfield "['role']")" "restaurant_admin"

expect "register customer" 201 POST /api/v1/users/register \
  "{\"email\":\"$CUST_EMAIL\",\"password\":\"Passw0rd!\",\"full_name\":\"Smoke Customer\",\"phone\":\"$CUST_PHONE\",\"role\":\"customer\"}"
CUST_ID=$(jfield "['id']")

expect "fetch user by id" 200 GET "/api/v1/users/$OWNER_ID"
assert "  returned the same user" "$(jfield "['id']")" "$OWNER_ID"

expect "onboard restaurant" 201 POST /api/v1/restaurants/onboard \
  "{\"owner_id\":\"$OWNER_ID\",\"name\":\"Smoke Diner\",\"address\":\"12 Test Street\",\"latitude\":33.68,\"longitude\":73.04,\"capacity\":40}"
REST_ID=$(jfield "['id']")
assert "  restaurant defaults to active" "$(jfield "['is_active']")" "True"

expect "fetch restaurant by id" 200 GET "/api/v1/restaurants/$REST_ID"

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
expect "publish menu" 200 POST /api/v1/menus "$MENU"
expect "fetch published menu" 200 GET "/api/v1/menus/$REST_ID"

# base 10.00 + cheddar 1.50 + bacon 2.00 = 13.50, x2 = 27.00
ORDER="{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":2,\"customizations\":{\"cheese\":\"cheddar\",\"extras\":[\"bacon\"]}}],\"total_amount\":27.00}"
IDEM="idem-$TAG"

expect "create order" 201 POST /api/v1/orders "$ORDER" -H "X-Idempotency-Key: $IDEM"
ORDER_ID=$(jfield "['id']")
assert "  server-recalculated unit price" "$(jfield "['items'][0]['unit_price']")" "13.5"
assert "  server-recalculated total"      "$(jfield "['total_amount']")"          "27.0"
assert "  initial status is 'created'"    "$(jfield "['status']")"                "created"

expect "replay same idempotency key -> 200" 200 POST /api/v1/orders "$ORDER" -H "X-Idempotency-Key: $IDEM"
assert "  replay returned the SAME order (no duplicate)" "$(jfield "['id']")" "$ORDER_ID"

# The Payment Service reads the order over HTTP, so this endpoint is part of the contract.
expect "fetch order by id" 200 GET "/api/v1/orders/$ORDER_ID"
assert "  returned the same order" "$(jfield "['id']")" "$ORDER_ID"

# Pay for it — a separate service, a separate database, verified over HTTP.
PAY_IDEM="pay-$TAG"
PAYMENT="{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM\"}"

expect "authorise payment" 201 POST /api/v1/payments "$PAYMENT" -H "X-Idempotency-Key: $PAY_IDEM"
PAYMENT_ID=$(jfield "['id']")
assert "  payment reached 'authorized'" "$(jfield "['status']")" "authorized"
assert "  amount settles the order"     "$(jfield "['amount']")" "27.0"
assert "  gateway reference recorded"   "$(jfield "['transaction_reference'][:8]")" "ch_mock_"

expect "fetch payment by id" 200 GET "/api/v1/payments/$PAYMENT_ID"
assert "  returned the same payment" "$(jfield "['id']")" "$PAYMENT_ID"

expect "replay same payment key -> 200" 200 POST /api/v1/payments "$PAYMENT" -H "X-Idempotency-Key: $PAY_IDEM"
assert "  replay returned the SAME payment (no double charge)" "$(jfield "['id']")" "$PAYMENT_ID"

# --------------------------------------------------------- user service
section "User Service edge cases"
expect "unknown role -> 400" 400 POST /api/v1/users/register \
  "{\"email\":\"x_${TAG}@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Bad Role\",\"phone\":\"+1777${RANDOM}\",\"role\":\"wizard\"}"
expect "duplicate email -> 409" 409 POST /api/v1/users/register \
  "{\"email\":\"$OWNER_EMAIL\",\"password\":\"Passw0rd!\",\"full_name\":\"Dup Email\",\"phone\":\"+1888${RANDOM}${RANDOM}\",\"role\":\"customer\"}"
expect "duplicate phone -> 409" 409 POST /api/v1/users/register \
  "{\"email\":\"dup_${TAG}@example.com\",\"password\":\"Passw0rd!\",\"full_name\":\"Dup Phone\",\"phone\":\"$OWNER_PHONE\",\"role\":\"customer\"}"
expect "short password -> 422" 422 POST /api/v1/users/register \
  "{\"email\":\"short_${TAG}@example.com\",\"password\":\"abc\",\"full_name\":\"Short Pass\",\"phone\":\"+1999${RANDOM}\",\"role\":\"customer\"}"
expect "unknown user id -> 404" 404 GET /api/v1/users/00000000-0000-0000-0000-000000000000

# --------------------------------------------------- restaurant service
section "Restaurant Service edge cases"
expect "unknown owner -> 404" 404 POST /api/v1/restaurants/onboard \
  "{\"owner_id\":\"00000000-0000-0000-0000-000000000000\",\"name\":\"Ghost Diner\",\"address\":\"1 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":5}"
expect "owner lacking restaurant_admin -> 403" 403 POST /api/v1/restaurants/onboard \
  "{\"owner_id\":\"$CUST_ID\",\"name\":\"Customer Diner\",\"address\":\"2 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":5}"
expect "latitude out of range -> 422" 422 POST /api/v1/restaurants/onboard \
  "{\"owner_id\":\"$OWNER_ID\",\"name\":\"Bad Coords\",\"address\":\"3 Nowhere Road\",\"latitude\":999,\"longitude\":10,\"capacity\":5}"
expect "capacity must be positive -> 422" 422 POST /api/v1/restaurants/onboard \
  "{\"owner_id\":\"$OWNER_ID\",\"name\":\"Zero Cap\",\"address\":\"4 Nowhere Road\",\"latitude\":10,\"longitude\":10,\"capacity\":0}"
expect "unknown restaurant id -> 404" 404 GET /api/v1/restaurants/00000000-0000-0000-0000-000000000000

# --------------------------------------------------------- menu service
section "Menu Service edge cases"
expect "menu for unknown restaurant -> 404" 404 POST /api/v1/menus \
  "{\"restaurant_id\":\"00000000-0000-0000-0000-000000000000\",\"categories\":[]}"
expect "min_selection > max_selection -> 422" 422 POST /api/v1/menus \
  "{\"restaurant_id\":\"$REST_ID\",\"categories\":[{\"category_id\":\"c\",\"category_name\":\"C\",\"items\":[{\"item_id\":\"i\",\"name\":\"I\",\"description\":\"d\",\"base_price\":1.0,\"customization_groups\":[{\"group_id\":\"g\",\"group_name\":\"G\",\"min_selection\":3,\"max_selection\":1,\"options\":[{\"name\":\"a\"}]}]}]}]}"
expect "no menu published -> 404" 404 GET /api/v1/menus/11111111-1111-1111-1111-111111111111

AUDIT_ORDER=$(python3 -c 'import uuid; print(uuid.uuid4())')
expect "audit log creates document -> 201" 201 POST /api/v1/menus/logs \
  "{\"order_id\":\"$AUDIT_ORDER\",\"status\":\"created\",\"service\":\"smoke-test\",\"raw_log\":\"{}\",\"updated_by\":\"tester\"}"
assert "  first write created the document" "$(jfield "['created_document']")" "True"
expect "audit log appends to document -> 201" 201 POST /api/v1/menus/logs \
  "{\"order_id\":\"$AUDIT_ORDER\",\"status\":\"confirmed\",\"service\":\"smoke-test\",\"raw_log\":\"{}\",\"updated_by\":\"tester\"}"
assert "  second write appended, did not recreate" "$(jfield "['created_document']")" "False"

# -------------------------------------------------------- order service
section "Order Service edge cases"
expect "missing idempotency key -> 400" 400 POST /api/v1/orders "$ORDER"
expect "total_amount mismatch -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"cheddar\"}}],\"total_amount\":1.00}" \
  -H "X-Idempotency-Key: $IDEM-mismatch"
expect "unavailable item -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"soldout\",\"quantity\":1}],\"total_amount\":5.00}" \
  -H "X-Idempotency-Key: $IDEM-soldout"
expect "item not on menu -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"nope\",\"quantity\":1}],\"total_amount\":5.00}" \
  -H "X-Idempotency-Key: $IDEM-unknown"
expect "unknown customization group -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"ghost\":\"x\"}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-badgroup"
expect "option not offered -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"gouda\"}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-badoption"
expect "required group omitted -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-missinggrp"
expect "too many selections -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":[\"cheddar\",\"none\"]}}],\"total_amount\":11.50}" \
  -H "X-Idempotency-Key: $IDEM-toomany"
expect "unknown customer -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"33333333-3333-3333-3333-333333333333\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":\"none\"}}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-badcust"
expect "restaurant has no menu -> 404" 404 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"11111111-1111-1111-1111-111111111111\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1}],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-nomenu"
expect "empty item list -> 422" 422 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[],\"total_amount\":10.00}" \
  -H "X-Idempotency-Key: $IDEM-empty"
expect "option object form is accepted" 201 POST /api/v1/orders \
  "{\"customer_id\":\"$CUST_ID\",\"restaurant_id\":\"$REST_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1,\"customizations\":{\"cheese\":{\"name\":\"cheddar\"}}}],\"total_amount\":11.50}" \
  -H "X-Idempotency-Key: $IDEM-dictform"
SECOND_ORDER_ID=$(jfield "['id']")
expect "unknown order id -> 404" 404 GET /api/v1/orders/00000000-0000-0000-0000-000000000000

# ------------------------------------------------------ payment service
section "Payment Service edge cases"
expect "missing idempotency header -> 400" 400 POST /api/v1/payments "$PAYMENT"
expect "header disagrees with body -> 400" 400 POST /api/v1/payments "$PAYMENT" \
  -H "X-Idempotency-Key: $PAY_IDEM-other"
expect "unknown order -> 422" 422 POST /api/v1/payments \
  "{\"order_id\":\"44444444-4444-4444-4444-444444444444\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-badorder\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-badorder"
expect "amount does not settle the order -> 422" 422 POST /api/v1/payments \
  "{\"order_id\":\"$SECOND_ORDER_ID\",\"amount\":1.00,\"idempotency_key\":\"$PAY_IDEM-short\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-short"
expect "non-positive amount -> 422" 422 POST /api/v1/payments \
  "{\"order_id\":\"$SECOND_ORDER_ID\",\"amount\":0,\"idempotency_key\":\"$PAY_IDEM-zero\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-zero"
expect "second payment for a paid order -> 409" 409 POST /api/v1/payments \
  "{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM-again\"}" \
  -H "X-Idempotency-Key: $PAY_IDEM-again"
expect "unknown payment id -> 404" 404 GET /api/v1/payments/00000000-0000-0000-0000-000000000000

# ---------------------------------------------- cross-service integration
section "Cross-service integration"
expect "order audit trail reached the Menu Service" 200 GET "/api/v1/menus/$REST_ID"
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^sfo-mongodb$'; then
  HISTORY=$(docker exec sfo-mongodb mongosh smartfoodops_menus --quiet --eval \
    "const d=db.order_tracking_logs.findOne({order_id:'$ORDER_ID'}); print(d ? d.status_history.map(e=>e.status).join(',') : 'MISSING')" 2>/dev/null | tr -d '\r')
  assert "  order-service wrote 'created' log via Menu Service" "$HISTORY" "created"
else
  printf '  %sSKIP%s  MongoDB audit-trail check (docker/sfo-mongodb not reachable)\n' "$DIM" "$RESET"
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
