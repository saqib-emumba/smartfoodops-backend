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

# Since Week 2 the order lifecycle is driven by a Temporal saga, so a status is reached
# *eventually* rather than by the time the request that triggered it returns. Every
# assertion about a post-checkout status has to poll; asserting immediately would be a race
# that passes on a fast machine and fails on a loaded one.
#   poll_status <order-id> <expected-status> [timeout-seconds] [auth-args...]
poll_status() {
  local oid="$1" want="$2" limit="${3:-30}" ; shift 3
  local waited=0 got=""
  while (( waited < limit )); do
    got=$(curl -sS -m 10 "$BASE_URL/api/v1/orders/$oid" "$@" 2>/dev/null \
          | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    [[ "$got" == "$want" ]] && { ok "order reaches '$want' (${waited}s)"; return 0; }
    sleep 2; waited=$((waited + 2))
  done
  bad "order reaches '$want'" "still '$got' after ${limit}s"
  return 1
}

# Poll one field of an order until it is non-empty; used for rider_id, which appears only
# once the saga's dispatch step has claimed somebody.
#   poll_field <order-id> <py-index-expr> [timeout] [auth-args...]
poll_field() {
  local oid="$1" expr="$2" limit="${3:-30}" ; shift 3
  local waited=0 got=""
  while (( waited < limit )); do
    got=$(curl -sS -m 10 "$BASE_URL/api/v1/orders/$oid" "$@" 2>/dev/null \
          | python3 -c "import json,sys; print(json.load(sys.stdin)$expr or '')" 2>/dev/null)
    [[ -n "$got" && "$got" != "None" ]] && { POLLED="$got"; return 0; }
    sleep 2; waited=$((waited + 2))
  done
  POLLED=""
  return 1
}

# Is a named container available to exec into? Several assertions can only be made from
# inside a database, and the suite has to stay runnable against a remote BASE_URL where
# there are no local containers at all.
have_container() {
  command -v docker >/dev/null 2>&1 || return 1
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^$1$"
}

# The payment row the saga wrote, as `status|amount|reference|key`, or empty if unreadable.
# Read from the database rather than the API because the customer-facing payment endpoints
# are keyed by payment id, and the client no longer learns that id — the saga does.
docker_payment_row() {
  have_container sfo-payment-db || return 0
  docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core -tA -c \
    "SELECT status||'|'||amount||'|'||COALESCE(transaction_reference,'')||'|'||idempotency_key||'|'||id
       FROM payments WHERE order_id='$1';" 2>/dev/null | tr -d '[:space:]'
}

# Whether any rider is left holding an order — the leak the first revision of the Week 2
# blueprint would have produced on every compensation path.
riders_stuck_unavailable() {
  have_container sfo-rider-db || { echo "-"; return 0; }
  # `current_order_id IS NOT NULL` is the real signal: a rider still bound to an order whose
  # saga has finished. Availability alone is not, since a rider may legitimately be off
  # shift, and the no-rider test grounds the fleet on purpose.
  docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -tA -c \
    "SELECT count(*) FROM riders WHERE current_order_id IS NOT NULL;" \
    2>/dev/null | tr -d '[:space:]'
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
    for p in users restaurants menus orders payments riders; do
      [[ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$BASE_URL/api/v1/$p/health")" == "200" ]] && up=$((up + 1))
    done
    if [[ $up -eq 6 ]]; then printf ' ready.\n'; return 0; fi
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
RIDER_EMAIL="rider_${TAG}@example.com"
RIDER2_EMAIL="rider2_${TAG}@example.com"
OWNER_PHONE="+1555${RANDOM}${RANDOM}"
CUST_PHONE="+1666${RANDOM}${RANDOM}"
OTHER_PHONE="+1444${RANDOM}${RANDOM}"
RIDER_PHONE="+1777${RANDOM}${RANDOM}"
RIDER2_PHONE="+1888${RANDOM}${RANDOM}"

# The restaurant every order in this run is placed against, and the coordinates dispatch
# measures from. Riders are seeded close to it so the 10km radius is satisfied.
REST_LAT=33.68
REST_LON=73.04

# The audit-log endpoint is service-to-service, so exercising it needs the shared key that
# docker-compose hands the Order Service. Read it from the same .env compose reads.
ENV_FILE="$(dirname "$0")/../.env"
INTERNAL_KEY=""
[[ -f "$ENV_FILE" ]] && INTERNAL_KEY=$(grep -m1 '^INTERNAL_API_KEY=' "$ENV_FILE" | cut -d= -f2-)

printf '%sSmartFoodOps smoke test%s  ->  %s\n' "$BOLD" "$RESET" "$BASE_URL"

# ---------------------------------------------------------------- health
# Health endpoints stay public: a probe must not need a credential.
section "Health"
for p in users restaurants menus orders payments riders; do
  expect "$p health" 200 GET "/api/v1/$p/health"
done
# The saga cannot advance an order without the orchestrator, so this is worth asserting
# rather than discovering later as a timeout in the lifecycle section.
expect "order service reports Temporal" 200 GET /api/v1/orders/health
assert "  temporal reachable" "$(jfield "['temporal_reachable']")" "True"

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

# Two riders, because one of the properties worth proving is that concurrent dispatch
# hands different orders to different riders rather than double-booking one.
expect "register rider" 201 POST /api/v1/users/register \
  "{\"email\":\"$RIDER_EMAIL\",\"password\":\"$PASSWORD\",\"full_name\":\"Smoke Rider\",\"phone\":\"$RIDER_PHONE\",\"role\":\"rider\"}"
RIDER_USER_ID=$(jfield "['id']")
assert "  rider role resolved from roles table" "$(jfield "['role']")" "rider"

expect "register second rider" 201 POST /api/v1/users/register \
  "{\"email\":\"$RIDER2_EMAIL\",\"password\":\"$PASSWORD\",\"full_name\":\"Second Rider\",\"phone\":\"$RIDER2_PHONE\",\"role\":\"rider\"}"
RIDER2_USER_ID=$(jfield "['id']")

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
do_login "$RIDER_EMAIL" "$PASSWORD"; RIDER_TOKEN="$ACCESS_TOKEN"
do_login "$RIDER2_EMAIL" "$PASSWORD"; RIDER2_TOKEN="$ACCESS_TOKEN"

OWNER_AUTH=(-H "Authorization: Bearer $OWNER_TOKEN")
CUST_AUTH=(-H "Authorization: Bearer $CUST_TOKEN")
OTHER_AUTH=(-H "Authorization: Bearer $OTHER_TOKEN")
RIDER_AUTH=(-H "Authorization: Bearer $RIDER_TOKEN")
RIDER2_AUTH=(-H "Authorization: Bearer $RIDER2_TOKEN")
INTERNAL=(-H "X-Internal-Key: $INTERNAL_KEY")

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
  "{\"name\":\"Smoke Diner\",\"address\":\"12 Test Street\",\"latitude\":$REST_LAT,\"longitude\":$REST_LON,\"capacity\":40}" \
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
# Still 'created' in this response, and that is not a race: the row is built from the
# INSERT's RETURNING clause, so it is the state at commit time. The saga advances it after.
assert "  initial status is 'created'"    "$(jfield "['status']")"                "created"

expect "replay same idempotency key -> 200" 200 POST /api/v1/orders "$ORDER" \
  -H "X-Idempotency-Key: $IDEM" "${CUST_AUTH[@]}"
assert "  replay returned the SAME order (no duplicate)" "$(jfield "['id']")" "$ORDER_ID"

# The Payment Service reads the order over HTTP, so this endpoint is part of the contract.
expect "fetch order by id" 200 GET "/api/v1/orders/$ORDER_ID" "" "${CUST_AUTH[@]}"
assert "  returned the same order" "$(jfield "['id']")" "$ORDER_ID"

# --- payment is now the saga's job, not the client's -------------------------------------
#
# Week 1 had the customer POST /api/v1/payments themselves. Since the Week 2 saga owns
# authorisation (D30) that call would race the workflow and lose on UNIQUE (order_id), so
# what is asserted here changed from "create a payment" to "observe the one the saga made".
poll_status "$ORDER_ID" confirmed 40 "${CUST_AUTH[@]}"

PAYMENT_ID=""
if [[ -n "$INTERNAL_KEY" ]]; then
  # Read the payment the workflow created. Its idempotency key is derived from the order id
  # rather than client-chosen, which is what makes a retried activity collapse onto the
  # unique index instead of charging twice.
  # The read the saga's payment activity uses, which has no bearer token to forward.
  expect "internal order read serves the saga" 200 GET "/api/v1/orders/$ORDER_ID/internal" "" "${INTERNAL[@]}"
  assert "  same authoritative total as the bearer path" "$(jfield "['total_amount']")" "27.0"
fi

SAGA_PAY=$(docker_payment_row "$ORDER_ID")
if [[ -n "$SAGA_PAY" ]]; then
  assert "  payment reached 'authorized'" "$(cut -d'|' -f1 <<<"$SAGA_PAY")" "authorized"
  assert "  amount settles the order"     "$(cut -d'|' -f2 <<<"$SAGA_PAY")" "27.00"
  assert "  gateway reference recorded"   "$(cut -d'|' -f3 <<<"$SAGA_PAY" | cut -c1-8)" "ch_mock_"
  assert "  idempotency key derived from the order" \
    "$(cut -d'|' -f4 <<<"$SAGA_PAY")" "wf-pay-$ORDER_ID"
  # The customer never learns this id from a response any more — the saga created the
  # payment — so it comes from the database to keep the read-path assertions below alive.
  PAYMENT_ID=$(cut -d'|' -f5 <<<"$SAGA_PAY")
  expect "customer reads the saga's payment" 200 GET "/api/v1/payments/$PAYMENT_ID" "" "${CUST_AUTH[@]}"
  assert "  returned the same payment" "$(jfield "['id']")" "$PAYMENT_ID"
fi

# The Week 1 contract change, asserted explicitly so a future "fix" that re-enables
# client-driven payment trips a failing test.
PAY_IDEM="pay-$TAG"
PAYMENT="{\"order_id\":\"$ORDER_ID\",\"amount\":27.00,\"idempotency_key\":\"$PAY_IDEM\"}"
expect "client payment for a saga-owned order -> 409" 409 POST /api/v1/payments "$PAYMENT" \
  -H "X-Idempotency-Key: $PAY_IDEM" "${CUST_AUTH[@]}"

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
  # `previous_status` is derived from whatever entry precedes this one, never supplied by
  # the caller (D24). That predecessor used to be the 'created' checkout wrote; since the
  # saga also records its own transitions it is now the saga's 'confirmed'. The property
  # under test is that the value is *derived*, so the expectation follows the real trail.
  assert "  previous status read from the preceding entry" "$(jfield "['previous_status']")" "confirmed"
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

# =========================================================== order saga
# Week 2. Everything above tests one request at a time; this section tests a *process* that
# outlives any single request — which is why every status assertion here polls.
section "Order saga — fleet setup"

expect "rider registers a profile" 201 POST /api/v1/riders \
  "{\"vehicle_type\":\"motorbike\",\"vehicle_number\":\"SMOKE-$RANDOM\",\"current_latitude\":$REST_LAT,\"current_longitude\":$REST_LON}" \
  "${RIDER_AUTH[@]}"
RIDER_ID=$(jfield "['id']")
assert "  rider taken from the token, not the body" "$(jfield "['user_id']")" "$RIDER_USER_ID"
assert "  rider starts available" "$(jfield "['is_available']")" "True"

expect "second rider registers" 201 POST /api/v1/riders \
  "{\"vehicle_type\":\"bicycle\",\"vehicle_number\":\"SMOKE2-$RANDOM\",\"current_latitude\":$REST_LAT,\"current_longitude\":$REST_LON}" \
  "${RIDER2_AUTH[@]}"
RIDER2_ID=$(jfield "['id']")

expect "a customer cannot join the fleet -> 403" 403 POST /api/v1/riders \
  "{\"vehicle_type\":\"car\",\"vehicle_number\":\"NOPE-$RANDOM\"}" "${CUST_AUTH[@]}"
expect "the same account cannot register twice -> 409" 409 POST /api/v1/riders \
  "{\"vehicle_type\":\"van\",\"vehicle_number\":\"DUPE-$RANDOM\"}" "${RIDER_AUTH[@]}"

expect "rider reports a location" 200 PATCH /api/v1/riders/me/location \
  "{\"current_latitude\":$REST_LAT,\"current_longitude\":$REST_LON}" "${RIDER_AUTH[@]}"
expect "rider reads own profile" 200 GET /api/v1/riders/me "" "${RIDER_AUTH[@]}"

# There is no delete endpoint for a rider — correctly, since riders are permanent fleet
# members, not test fixtures — so every prior run of this script has left its own two
# riders behind, at these exact coordinates. Left alone, a dispatch below could pick a
# stranger from an earlier run rather than one of the two this run just registered, purely
# because both are tied at distance zero. Grounding everyone else is what makes the
# happy-path assertions about *which* rider was assigned deterministic.
if have_container sfo-rider-db; then
  docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -q -c \
    "UPDATE riders SET is_available = FALSE WHERE id NOT IN ('$RIDER_ID', '$RIDER2_ID');" \
    >/dev/null 2>&1
fi

# --- internal-only surfaces --------------------------------------------------------------
# The saga's endpoints must be unreachable with a user token, however privileged. These are
# the checks that keep "the workflow may do this" from becoming "anyone may do this".
section "Order saga — internal boundaries"

expect "dispatch without the internal key -> 401" 401 POST /api/v1/riders/dispatch \
  "{\"order_id\":\"$ORDER_ID\",\"restaurant_latitude\":$REST_LAT,\"restaurant_longitude\":$REST_LON}"
expect "dispatch with a customer token -> 401" 401 POST /api/v1/riders/dispatch \
  "{\"order_id\":\"$ORDER_ID\",\"restaurant_latitude\":$REST_LAT,\"restaurant_longitude\":$REST_LON}" \
  "${CUST_AUTH[@]}"
expect "release without the internal key -> 401" 401 POST /api/v1/riders/release \
  "{\"order_id\":\"$ORDER_ID\"}"
expect "refund without the internal key -> 401" 401 POST /api/v1/payments/refund \
  "{\"order_id\":\"$ORDER_ID\"}"
expect "authorize without the internal key -> 401" 401 POST /api/v1/payments/authorize \
  "{\"order_id\":\"$ORDER_ID\",\"amount\":\"27.00\",\"idempotency_key\":\"nope\"}"
expect "signal relay without the internal key -> 401" 401 POST "/api/v1/orders/$ORDER_ID/signals" \
  "{\"signal\":\"rider_pickup\",\"payload\":{}}"
expect "internal order read without the key -> 401" 401 GET "/api/v1/orders/$ORDER_ID/internal"

# The kitchen path is no longer internal at all. Since D32 an admin decides an order
# directly on the Order Service, authenticated as themselves — so what has to be proven
# here is the *authorisation*, not an internal key: the wrong role, and the right role on
# somebody else's restaurant, must both be refused.
expect "deciding an order with no token -> 401" 401 POST "/api/v1/orders/$ORDER_ID/accept"
expect "a customer cannot accept an order -> 403" 403 \
  POST "/api/v1/orders/$ORDER_ID/accept" "" "${CUST_AUTH[@]}"
expect "a rider cannot accept an order -> 403" 403 \
  POST "/api/v1/orders/$ORDER_ID/accept" "" "${RIDER_AUTH[@]}"
expect "the kitchen queue needs the admin role -> 403" 403 \
  GET "/api/v1/orders/kitchen/$REST_ID" "" "${CUST_AUTH[@]}"
expect "deciding an order that does not exist -> 404" 404 \
  POST /api/v1/orders/00000000-0000-0000-0000-000000000000/accept "" "${OWNER_AUTH[@]}"

if [[ -n "$INTERNAL_KEY" ]]; then
  # The relay now carries rider events only: a kitchen decision has its own authenticated
  # endpoint, and leaving it reachable here too would be a second way to do one thing.
  expect "the relay no longer accepts a kitchen decision -> 422" 422 \
    POST "/api/v1/orders/$ORDER_ID/signals" \
    '{"signal":"restaurant_decision","payload":{"decision":"accepted"}}' "${INTERNAL[@]}"
fi

# --- happy path all the way to delivered -------------------------------------------------
section "Order saga — happy path to 'delivered'"

# ORDER_ID is already 'confirmed' and parked waiting for the kitchen (asserted earlier).
expect "kitchen queue shows the order" 200 GET "/api/v1/orders/kitchen/$REST_ID" "" "${OWNER_AUTH[@]}"
assert "  the order awaiting a decision is this one" "$(jfield "[0]['id']")" "$ORDER_ID"
# The kitchen projection is narrower than OrderResponse on purpose: moving the queue onto
# `orders` must not widen what a restaurant can see about a customer's order.
assert "  the kitchen is not shown what was paid" \
  "$(python3 -c "import json,sys; print('total_amount' in json.load(sys.stdin)[0])" <<<"$BODY")" "False"
assert "  nor who ordered it" \
  "$(python3 -c "import json,sys; print('customer_id' in json.load(sys.stdin)[0])" <<<"$BODY")" "False"

expect "kitchen accepts the order" 200 POST "/api/v1/orders/$ORDER_ID/accept" \
  "" "${OWNER_AUTH[@]}"
assert "  decision recorded as accepted" "$(jfield "['decision']")" "accepted"
assert "  and it actually changed something" "$(jfield "['changed']")" "True"

expect "accepting twice does not signal the saga again" 200 \
  POST "/api/v1/orders/$ORDER_ID/accept" "" "${OWNER_AUTH[@]}"
assert "  second accept is a no-op" "$(jfield "['changed']")" "False"

# Acceptance releases the saga to find a rider.
poll_status "$ORDER_ID" assigned 60 "${CUST_AUTH[@]}"
if poll_field "$ORDER_ID" "['rider_id']" 20 "${CUST_AUTH[@]}"; then
  ok "  order records the assigned rider"
  ASSIGNED_RIDER="$POLLED"
else
  bad "  order records the assigned rider" "rider_id stayed empty"
  ASSIGNED_RIDER=""
fi

# Whichever rider was claimed has to be the one that reports the pickup. Dispatch picks by
# proximity and both riders are at the same coordinates, so which one wins is not fixed.
if [[ "$ASSIGNED_RIDER" == "$RIDER_ID" ]]; then
  CARRIER=("${RIDER_AUTH[@]}"); OTHER_CARRIER=("${RIDER2_AUTH[@]}")
else
  CARRIER=("${RIDER2_AUTH[@]}"); OTHER_CARRIER=("${RIDER_AUTH[@]}")
fi

expect "the other rider cannot report this delivery -> 403" 403 \
  POST "/api/v1/riders/me/orders/$ORDER_ID/picked-up" "" "${OTHER_CARRIER[@]}"
expect "a rider cannot go off shift mid-order -> 409" 409 \
  PATCH /api/v1/riders/me/availability '{"is_available":true}' "${CARRIER[@]}"

expect "rider reports the pickup" 204 POST "/api/v1/riders/me/orders/$ORDER_ID/picked-up" \
  "" "${CARRIER[@]}"
poll_status "$ORDER_ID" picked_up 40 "${CUST_AUTH[@]}"

expect "rider reports the delivery" 204 POST "/api/v1/riders/me/orders/$ORDER_ID/delivered" \
  "" "${CARRIER[@]}"
poll_status "$ORDER_ID" delivered 40 "${CUST_AUTH[@]}"

# The whole lifecycle, in order, derived rather than asserted by the caller.
expect "the trail records the full lifecycle" 200 GET "/api/v1/orders/$ORDER_ID/logs" "" "${CUST_AUTH[@]}"
LIFECYCLE=$(python3 -c "
import json,sys
seen, out = None, []
for e in json.load(sys.stdin):
    if e['status'] != seen: out.append(e['status']); seen = e['status']
print(','.join(out))" <<<"$BODY" 2>/dev/null)
assert "  created -> confirmed -> assigned -> picked_up -> delivered" \
  "$LIFECYCLE" "created,confirmed,assigned,picked_up,delivered"

# The rider must be back in the pool. This is the assertion the first revision of the
# blueprint would have failed: it never released anybody.
sleep 3
if have_container sfo-rider-db; then
  FREED=$(docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -tA -c \
    "SELECT is_available::text||','||COALESCE(current_order_id::text,'null') FROM riders WHERE id='$ASSIGNED_RIDER';" \
    2>/dev/null | tr -d '[:space:]')
  assert "  the rider was released after delivery" "$FREED" "true,null"
fi

# --- compensation: the kitchen says no ---------------------------------------------------
section "Order saga — compensation on rejection"

REJ_IDEM="idem-rej-$TAG"
expect "place an order to be rejected" 201 POST /api/v1/orders "$ORDER" \
  -H "X-Idempotency-Key: $REJ_IDEM" "${CUST_AUTH[@]}"
REJ_ORDER=$(jfield "['id']")
poll_status "$REJ_ORDER" confirmed 40 "${CUST_AUTH[@]}"

expect "kitchen rejects the order" 200 POST "/api/v1/orders/$REJ_ORDER/reject" \
  "" "${OWNER_AUTH[@]}"
assert "  decision recorded as rejected" "$(jfield "['decision']")" "rejected"

poll_status "$REJ_ORDER" cancelled 60 "${CUST_AUTH[@]}"
REJ_PAY=$(docker_payment_row "$REJ_ORDER")
[[ -n "$REJ_PAY" ]] && assert "  the payment was refunded" "$(cut -d'|' -f1 <<<"$REJ_PAY")" "refunded"
[[ -n "$REJ_PAY" ]] && assert "  the refund has its own gateway reference" \
  "$(cut -d'|' -f3 <<<"$REJ_PAY" | cut -c1-8)" "re_mock_"

# No rider was ever claimed for this order, so none should be held by it.
if have_container sfo-rider-db; then
  HELD=$(docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -tA -c \
    "SELECT count(*) FROM riders WHERE current_order_id='$REJ_ORDER';" 2>/dev/null | tr -d '[:space:]')
  assert "  no rider was claimed for a rejected order" "$HELD" "0"
fi

# A second reject must not signal the saga again.
expect "re-rejecting an already-decided order is a no-op" 200 \
  POST "/api/v1/orders/$REJ_ORDER/reject" "" "${OWNER_AUTH[@]}"
assert "  decision unchanged" "$(jfield "['decision']")" "rejected"

# The capacity slot must come back. Since D32 the kitchen's rail is defined as
# `status = 'confirmed' AND kitchen_decision IS NULL`, so a cancelled order leaves it as a
# side effect of being cancelled — there is no expiry step, and no way for a slot to stay
# occupied by an order that no longer exists. That whole class of leak is gone by
# construction, and this asserts it.
if have_container sfo-order-db; then
  ON_RAIL=""
  for _ in $(seq 1 15); do
    ON_RAIL=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
      "SELECT count(*) FROM orders WHERE id = '$REJ_ORDER'
         AND status = 'confirmed' AND kitchen_decision IS NULL;" 2>/dev/null | tr -d '[:space:]')
    [[ "$ON_RAIL" == "0" ]] && break
    sleep 2
  done
  assert "  the cancelled order left the kitchen's rail" "$ON_RAIL" "0"
fi

# --- compensation: nobody is free to deliver ---------------------------------------------
section "Order saga — compensation when no rider is available"

if have_container sfo-rider-db; then
  # Take the whole fleet off the road, so dispatch exhausts its attempts and the saga has
  # to refund. Restored afterwards.
  docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -q -c \
    "UPDATE riders SET is_available = FALSE;" >/dev/null 2>&1
  ok "grounded the fleet"

  NR_IDEM="idem-norider-$TAG"
  expect "place an order with no riders on the road" 201 POST /api/v1/orders "$ORDER" \
    -H "X-Idempotency-Key: $NR_IDEM" "${CUST_AUTH[@]}"
  NR_ORDER=$(jfield "['id']")
  poll_status "$NR_ORDER" confirmed 40 "${CUST_AUTH[@]}"
  expect "kitchen accepts it" 200 POST "/api/v1/orders/$NR_ORDER/accept" "" "${OWNER_AUTH[@]}"

  # RIDER_SEARCH_ATTEMPTS x RIDER_SEARCH_INTERVAL_SECONDS = 6 x 10s, so allow ~90s.
  poll_status "$NR_ORDER" cancelled 120 "${CUST_AUTH[@]}"
  NR_PAY=$(docker_payment_row "$NR_ORDER")
  [[ -n "$NR_PAY" ]] && assert "  the payment was refunded" "$(cut -d'|' -f1 <<<"$NR_PAY")" "refunded"

  # Restore availability only for riders holding nothing. Clearing `current_order_id` here
  # would erase the very evidence the leak check below looks for.
  docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -q -c \
    "UPDATE riders SET is_available = TRUE WHERE current_order_id IS NULL;" >/dev/null 2>&1
  ok "returned the fleet to the road"
else
  printf '  %sSKIP%s  no-rider compensation (docker/sfo-rider-db not reachable)\n' "$DIM" "$RESET"
fi

# --- capacity ----------------------------------------------------------------------------
# `restaurants.capacity` went unread by anything until Week 2, and since D32 it is enforced
# in the same local transaction that puts an order on the rail — so two orders can never
# both take the last slot. Onboard a kitchen with room for exactly one and prove the second
# order is refused, refunded, and never reaches 'confirmed'.
section "Order saga — capacity is enforced"

expect "onboard a one-slot kitchen" 201 POST /api/v1/restaurants/onboard \
  "{\"name\":\"One Slot Diner\",\"address\":\"1 Tight Street\",\"latitude\":$REST_LAT,\"longitude\":$REST_LON,\"capacity\":1}" \
  "${OWNER_AUTH[@]}"
TIGHT_ID=$(jfield "['id']")
expect "publish its menu" 200 POST /api/v1/menus \
  "{\"restaurant_id\":\"$TIGHT_ID\",\"categories\":[{\"category_id\":\"c1\",\"category_name\":\"Mains\",\"display_order\":1,\"items\":[{\"item_id\":\"burger\",\"name\":\"Burger\",\"description\":\"Beef burger\",\"base_price\":10.00,\"is_available\":true}]}]}" \
  "${OWNER_AUTH[@]}"

TIGHT_ORDER="{\"restaurant_id\":\"$TIGHT_ID\",\"items\":[{\"item_id\":\"burger\",\"quantity\":1}],\"total_amount\":10.00}"
expect "first order takes the only slot" 201 POST /api/v1/orders "$TIGHT_ORDER" \
  -H "X-Idempotency-Key: $IDEM-cap1" "${CUST_AUTH[@]}"
CAP1=$(jfield "['id']")
expect "second order is placed too" 201 POST /api/v1/orders "$TIGHT_ORDER" \
  -H "X-Idempotency-Key: $IDEM-cap2" "${CUST_AUTH[@]}"
CAP2=$(jfield "['id']")

poll_status "$CAP1" confirmed 45 "${CUST_AUTH[@]}"
poll_status "$CAP2" cancelled 60 "${CUST_AUTH[@]}"

CAP2_PAY=$(docker_payment_row "$CAP2")
[[ -n "$CAP2_PAY" ]] && assert "  the refused order was refunded" "$(cut -d'|' -f1 <<<"$CAP2_PAY")" "refunded"

if have_container sfo-order-db; then
  # It never reached 'confirmed' at all: the gate is *entry* to the rail, so the refused
  # order goes created -> cancelled without ever occupying a slot.
  CAP2_TRAIL=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
    "SELECT string_agg(new_status::text, ',' ORDER BY seq) FROM order_tracking_logs WHERE order_id='$CAP2';" \
    2>/dev/null | tr -d '[:space:]')
  assert "  it never joined the rail" "$CAP2_TRAIL" "created,cancelled"
  CAP2_REASON=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
    "SELECT metadata->>'reason' FROM order_tracking_logs WHERE order_id='$CAP2' AND new_status='cancelled';" \
    2>/dev/null | tr -d '[:space:]')
  assert "  and the trail names why" "$CAP2_REASON" "kitchen_at_capacity"
  RAIL=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
    "SELECT count(*) FROM orders WHERE restaurant_id='$TIGHT_ID' AND status='confirmed' AND kitchen_decision IS NULL;" \
    2>/dev/null | tr -d '[:space:]')
  assert "  the rail never exceeded its capacity of 1" "$RAIL" "1"
fi

# --- no rider left behind ----------------------------------------------------------------
section "Order saga — the fleet is not leaking"

STUCK=$(riders_stuck_unavailable)
if [[ "$STUCK" == "-" ]]; then
  printf '  %sSKIP%s  fleet leak check (docker/sfo-rider-db not reachable)\n' "$DIM" "$RESET"
else
  # Every saga above either delivered or compensated, and both paths release. A non-zero
  # count here means a rider is stranded, which is precisely the bug the first revision of
  # the Week 2 blueprint shipped: it refunded on failure but never released.
  assert "no rider is stranded after every saga finished" "$STUCK" "0"
fi

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
  # always first, and the saga appends one entry per transition through to 'delivered'.
  # The extra 'confirmed' appears only when the internal key was available: the audit-log
  # section above appends one by hand, and that endpoint records a *reported* transition
  # with no compare-and-set, so a manual report is an extra entry rather than a no-op.
  EXPECTED_TRAIL="created,confirmed,assigned,picked_up,delivered"
  [[ -n "$INTERNAL_KEY" ]] && EXPECTED_TRAIL="created,confirmed,confirmed,assigned,picked_up,delivered"
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
