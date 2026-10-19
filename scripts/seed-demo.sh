#!/usr/bin/env bash
# Seed a demo customer with a believable order history, so the assistant has something real to talk about.
#
#   ./scripts/seed-demo.sh              full seed: accounts, restaurant, menu, rider, 3 delivered orders
#                                       (2 reviewed), 1 rejected-and-refunded order, 1 order in progress
#   ./scripts/seed-demo.sh --live       only a FRESH in-progress order (rider assigned, not yet picked up)
#   ./scripts/seed-demo.sh --cancelled  add one more order the kitchen rejects (cancelled and refunded). Run it BEFORE
#                                       --live: "why was my order cancelled?" explains the latest cancelled order, and
#                                       "where is my order?" the latest order, so the in-progress one must be newest
#   ./scripts/seed-demo.sh --advance    move the in-progress order one step: picked up, then delivered
#   ./scripts/seed-demo.sh --status     show what the demo customer's orders look like right now
#
# Everything goes through the public API, exactly as a client would, so the orders, the trail, the payments
# and the saga history are real. Safe to re-run: accounts, the restaurant and the menu are reused.
#
# To add the orders to an account you already use, give its credentials (it must be a customer):
#   CUSTOMER_EMAIL=you@example.com CUSTOMER_PASSWORD='your password' ./scripts/seed-demo.sh
#
# IMPORTANT: an accepted order that nobody picks up is cancelled by the saga after DELIVERY_TIMEOUT_SECONDS
# (3600 s, services/common/config.py). An "in progress" order therefore lasts about an hour. Run `--live`
# shortly before you need one, not the night before. Delivered and cancelled orders last forever.
#
# Requires: bash, curl, python3. The stack must be up (docker compose up -d).

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${DEMO_STATE:-$HERE/../.demo-state.json}"

# Fixed demo identities. Passwords are throwaway dev values, not secrets.
DEMO_PASSWORD='DemoPass123!'
CUSTOMER_EMAIL="${CUSTOMER_EMAIL:-demo.customer@smartfoodops.demo}"
CUSTOMER_PASSWORD="${CUSTOMER_PASSWORD:-$DEMO_PASSWORD}"
OWNER_EMAIL="demo.owner@smartfoodops.demo"
RIDER_EMAIL="demo.rider@smartfoodops.demo"
RIDER2_EMAIL="demo.rider2@smartfoodops.demo"

# Islamabad, about 10 km from the 33.70, 73.00 the smoke and Postman tests put their riders at, so the demo riders
# are the only ones near the demo kitchen and dispatch cannot hand the order to a rider left over from another run.
# Override to demo from another city: REST_LAT=24.8607 REST_LON=67.0011 (Karachi).
REST_LAT="${REST_LAT:-33.6500}"
REST_LON="${REST_LON:-73.0900}"
RIDER2_LAT=$(python3 -c "print(round($REST_LAT + 0.009, 6))")   # about a kilometre north

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
die()  { printf '  %s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

# ---- HTTP -------------------------------------------------------------------------------------------
# call METHOD PATH [JSON] [TOKEN] [IDEMPOTENCY-KEY]  ->  CODE, BODY
call() {
  local method="$1" path="$2" data="${3:-}" token="${4:-}" idem="${5:-}"
  local args=(-sS -m 60 -X "$method" "$BASE_URL/api/v1$path" -H 'Content-Type: application/json' -w $'\n%{http_code}')
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  [[ -n "$idem"  ]] && args+=(-H "X-Idempotency-Key: $idem")
  [[ -n "$data"  ]] && args+=(-d "$data")
  local out; out=$(curl "${args[@]}" 2>&1) || { CODE=000; BODY="$out"; return; }
  CODE=$(tail -n1 <<<"$out"); BODY=$(sed '$d' <<<"$out")
}
# jget JSON PYTHON-INDEX-EXPR   e.g. jget "$BODY" "['body']['id']"
jget() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d$2)" "$1" 2>/dev/null; }

state_get() { python3 -c "import json,sys,os; p=sys.argv[1]; d=json.load(open(p)) if os.path.exists(p) else {}; print(d.get(sys.argv[2],''))" "$STATE" "$1"; }
state_set() { python3 -c "
import json,sys,os
p=sys.argv[1]; d=json.load(open(p)) if os.path.exists(p) else {}
d[sys.argv[2]]=sys.argv[3]; json.dump(d,open(p,'w'),indent=2)" "$STATE" "$1" "$2"; }

# ---- accounts ---------------------------------------------------------------------------------------
login() {   # login EMAIL PASSWORD -> prints an access token, or nothing
  call POST /users/login "{\"email\":\"$1\",\"password\":\"$2\"}"
  [[ "$CODE" == "200" ]] && jget "$BODY" "['body']['access_token']"
}
ensure_account() {   # ensure_account NAME EMAIL PHONE ROLE PASSWORD -> token
  local token; token=$(login "$2" "$5")
  if [[ -z "$token" ]]; then
    call POST /users/register "{\"email\":\"$2\",\"password\":\"$5\",\"full_name\":\"$1\",\"phone\":\"$3\",\"role\":\"$4\"}"
    [[ "$CODE" == "201" ]] || die "could not register $2 ($CODE): $BODY"
    token=$(login "$2" "$5")
  fi
  [[ -n "$token" ]] || die "could not sign in as $2"
  printf '%s' "$token"
}

# ---- orders -----------------------------------------------------------------------------------------
poll_status() {   # poll_status ORDER WANT SECONDS TOKEN
  local waited=0 got=""
  while (( waited < $3 )); do
    call GET "/orders/$1" "" "$4"; got=$(jget "$BODY" "['body']['status']")
    [[ "$got" == "$2" ]] && return 0
    sleep 2; waited=$((waited + 2))
  done
  die "order $1 stayed '$got', wanted '$2' after ${3}s"
}
# Two demo riders, so the restaurant still shows a free courier while one is busy with the in-progress order.
# Dispatch picks the nearest, so the order goes to whichever rider the saga claimed: look that up.
carrier_for() {   # carrier_for ORDER -> CARRIER_TOKEN
  call GET "/orders/$1" "" "$CUSTOMER_TOKEN"
  if [[ "$(jget "$BODY" "['body']['rider_id']")" == "$RIDER2_ID" ]]; then CARRIER_TOKEN="$RIDER2_TOKEN"; else CARRIER_TOKEN="$RIDER_TOKEN"; fi
}
rider_free() {   # rider_free TOKEN: wait until that rider is available again (the saga releases them after delivery)
  local waited=0
  while (( waited < 40 )); do
    call GET /riders/me "" "$1"
    [[ "$(jget "$BODY" "['body']['is_available']")" == "True" ]] && return 0
    sleep 2; waited=$((waited + 2))
  done
  die "the demo rider did not become available again"
}
# Each in-progress demo order keeps a rider busy until it is advanced or the saga times it out (an hour). Run
# `--live` a few times and both demo riders would be tied up, so the next order would find nobody and be cancelled.
# Before making a fresh one, finish whatever an earlier run left a rider holding.
release_rider() {   # release_rider TOKEN
  local token="$1" oid
  call GET /riders/me "" "$token"; oid=$(jget "$BODY" "['body']['current_order_id']")
  [[ -z "$oid" || "$oid" == "None" ]] && return 0
  say "  finishing an earlier in-progress order ($oid) to free its rider"
  call POST "/riders/me/orders/$oid/picked-up" "" "$token"     # may already be picked up; the answer is ignored
  sleep 4
  call POST "/riders/me/orders/$oid/delivered" "" "$token"
  [[ "$CODE" == "200" ]] || die "could not finish the earlier order $oid ($CODE): $BODY"
  rider_free "$token"
}
release_busy_riders() { release_rider "$RIDER_TOKEN"; release_rider "$RIDER2_TOKEN"; }

place() {   # place ITEMS-JSON LABEL -> prints the order id
  call POST /orders "{\"restaurant_id\":\"$REST_ID\",\"items\":$1}" "$CUSTOMER_TOKEN" "demo-$(date +%s)-$RANDOM"
  [[ "$CODE" == "201" ]] || die "could not place the $2 order ($CODE): $BODY"
  local id; id=$(jget "$BODY" "['body']['id']")
  poll_status "$id" confirmed 40 "$CUSTOMER_TOKEN"
  printf '%s' "$id"
}
accept_and_assign() {
  call POST "/orders/$1/accept" "" "$OWNER_TOKEN"; [[ "$CODE" == "200" ]] || die "kitchen could not accept $1 ($CODE): $BODY"
  poll_status "$1" assigned 90 "$CUSTOMER_TOKEN"
}
pick_up()   { carrier_for "$1"; call POST "/riders/me/orders/$1/picked-up" "" "$CARRIER_TOKEN"; [[ "$CODE" == "200" ]] || die "pickup failed ($CODE): $BODY"; poll_status "$1" picked_up 40 "$CUSTOMER_TOKEN"; }
deliver()   { carrier_for "$1"; call POST "/riders/me/orders/$1/delivered" "" "$CARRIER_TOKEN"; [[ "$CODE" == "200" ]] || die "delivery failed ($CODE): $BODY"; poll_status "$1" delivered 40 "$CUSTOMER_TOKEN"; rider_free "$CARRIER_TOKEN"; }

# ---- setup shared by every mode -----------------------------------------------------------------------
setup() {
  step "Accounts"
  if [[ "$CUSTOMER_EMAIL" == "demo.customer@smartfoodops.demo" ]]; then
    CUSTOMER_TOKEN=$(ensure_account "Demo Customer" "$CUSTOMER_EMAIL" "03009990001" customer "$CUSTOMER_PASSWORD") || exit 1
  else
    CUSTOMER_TOKEN=$(login "$CUSTOMER_EMAIL" "$CUSTOMER_PASSWORD")
    [[ -n "$CUSTOMER_TOKEN" ]] || die "could not sign in as $CUSTOMER_EMAIL: check CUSTOMER_EMAIL and CUSTOMER_PASSWORD"
  fi
  ok "customer  $CUSTOMER_EMAIL"
  OWNER_TOKEN=$(ensure_account "Demo Owner" "$OWNER_EMAIL" "03009990002" restaurant_admin "$DEMO_PASSWORD") || exit 1; ok "owner     $OWNER_EMAIL"
  RIDER_TOKEN=$(ensure_account "Demo Rider" "$RIDER_EMAIL" "03009990003" rider "$DEMO_PASSWORD") || exit 1;          ok "rider     $RIDER_EMAIL"
  RIDER2_TOKEN=$(ensure_account "Demo Rider Two" "$RIDER2_EMAIL" "03009990004" rider "$DEMO_PASSWORD") || exit 1;   ok "rider     $RIDER2_EMAIL"

  step "Restaurant and menu"
  REST_ID=$(state_get restaurant_id)
  if [[ -n "$REST_ID" ]]; then
    call GET "/restaurants/$REST_ID" "" "$CUSTOMER_TOKEN"; [[ "$CODE" == "200" ]] || REST_ID=""
  fi
  if [[ -z "$REST_ID" ]]; then
    call POST /restaurants/onboard "{\"name\":\"Demo Kitchen\",\"address\":\"1 Demo Street, Islamabad\",\"latitude\":$REST_LAT,\"longitude\":$REST_LON,\"capacity\":50,\"prep_time_minutes\":20}" "$OWNER_TOKEN"
    [[ "$CODE" == "201" ]] || die "could not onboard the restaurant ($CODE): $BODY"
    REST_ID=$(jget "$BODY" "['body']['id']"); state_set restaurant_id "$REST_ID"
  fi
  # A restaurant onboarded before the preparation time existed has the default; set the demo kitchen's own.
  call PATCH "/restaurants/$REST_ID" '{"prep_time_minutes":20}' "$OWNER_TOKEN"
  [[ "$CODE" == "200" ]] || die "could not set the preparation time ($CODE): $BODY"
  ok "Demo Kitchen  $REST_ID  (preparation time 20 minutes)"
  MENU=$(cat <<JSON
{"restaurant_id":"$REST_ID","categories":[
 {"category_id":"mains","category_name":"Mains","display_order":1,"items":[
  {"item_id":"karahi","name":"Chicken Karahi","description":"Spicy wok-cooked chicken with tomatoes and green chillies","base_price":12.50,"is_available":true,"dietary_tags":["halal","pakistani","spicy","karahi"]},
  {"item_id":"biryani","name":"Beef Biryani","description":"Fragrant spiced rice layered with slow-cooked beef","base_price":11.00,"is_available":true,"dietary_tags":["halal","pakistani","biryani"]},
  {"item_id":"burger","name":"Spicy Chicken Burger","description":"Crispy chicken, hot sauce, pickles and lettuce in a brioche bun","base_price":9.50,"is_available":true,"dietary_tags":["halal","burger","fast-food","spicy"]},
  {"item_id":"pizza","name":"Margherita Pizza","description":"Tomato, mozzarella and fresh basil on a thin crust","base_price":10.00,"is_available":true,"dietary_tags":["vegetarian","pizza","italian"]},
  {"item_id":"salad","name":"Garden Quinoa Salad","description":"Light crisp salad with quinoa, cucumber and lemon","base_price":8.00,"is_available":true,"dietary_tags":["vegan","light-meal","gluten-free"]}]},
 {"category_id":"sides","category_name":"Drinks and desserts","display_order":2,"items":[
  {"item_id":"lassi","name":"Sweet Mango Lassi","description":"Cold sweet yogurt drink with mango","base_price":4.00,"is_available":true,"dietary_tags":["vegetarian","pakistani"]},
  {"item_id":"brownie","name":"Chocolate Brownie","description":"Warm fudgy brownie","base_price":5.50,"is_available":true,"dietary_tags":["vegetarian"]},
  {"item_id":"special","name":"Lamb Chops (sold out)","description":"Grilled lamb chops","base_price":18.00,"is_available":false,"dietary_tags":["halal","bbq"]}]}]}
JSON
)
  call POST /menus "$MENU" "$OWNER_TOKEN"; [[ "$CODE" == "200" ]] || die "could not publish the menu ($CODE): $BODY"
  ok "menu published (7 dishes, 1 sold out)"

  step "Rider"
  call POST /riders "{\"vehicle_type\":\"motorbike\",\"vehicle_number\":\"DEMO-001\",\"current_latitude\":$REST_LAT,\"current_longitude\":$REST_LON}" "$RIDER_TOKEN"
  [[ "$CODE" == "201" || "$CODE" == "409" ]] || die "could not register the rider ($CODE): $BODY"
  call PATCH /riders/me/location "{\"current_latitude\":$REST_LAT,\"current_longitude\":$REST_LON}" "$RIDER_TOKEN"
  [[ "$CODE" == "200" ]] || die "could not place the rider ($CODE): $BODY"
  call PATCH /riders/me/availability '{"is_available":true}' "$RIDER_TOKEN"
  call GET /riders/me "" "$RIDER_TOKEN"; RIDER_ID=$(jget "$BODY" "['body']['id']")
  # The second rider is about a kilometre away, so dispatch prefers the first when both are free.
  call POST /riders "{\"vehicle_type\":\"bicycle\",\"vehicle_number\":\"DEMO-002\",\"current_latitude\":$RIDER2_LAT,\"current_longitude\":$REST_LON}" "$RIDER2_TOKEN"
  [[ "$CODE" == "201" || "$CODE" == "409" ]] || die "could not register the second rider ($CODE): $BODY"
  call PATCH /riders/me/location "{\"current_latitude\":$RIDER2_LAT,\"current_longitude\":$REST_LON}" "$RIDER2_TOKEN"
  [[ "$CODE" == "200" ]] || die "could not place the second rider ($CODE): $BODY"
  call PATCH /riders/me/availability '{"is_available":true}' "$RIDER2_TOKEN"
  call GET /riders/me "" "$RIDER2_TOKEN"; RIDER2_ID=$(jget "$BODY" "['body']['id']")
  ok "two riders on shift near the restaurant"
}

cancelled_order() {   # an order the kitchen rejects: cancelled, and the payment refunded
  local id; id=$(place '[{"item_id":"biryani","quantity":1}]' "to be rejected") || exit 1
  call POST "/orders/$id/reject" "" "$OWNER_TOKEN"; [[ "$CODE" == "200" ]] || die "kitchen could not reject $id ($CODE): $BODY"
  poll_status "$id" cancelled 90 "$CUSTOMER_TOKEN"
  state_set rejected_order "$id"; ok "cancelled and refunded  ($id)"
}

in_progress_order() {   # creates the fresh in-progress order and records it
  step "In-progress order (rider assigned, waiting to pick it up)"
  local id; id=$(place '[{"item_id":"karahi","quantity":1},{"item_id":"lassi","quantity":2}]' "in-progress") || exit 1
  accept_and_assign "$id"
  state_set in_progress_order "$id"
  ok "order $id is assigned and waiting for pickup"
  say "  ${DIM}It lasts about an hour unless a rider picks it up; --advance moves it along.${RESET}"
}

show_status() {
  step "The in-progress demo order"
  [[ -n "${CUSTOMER_TOKEN:-}" ]] || die "not signed in"
  local id; id=$(state_get in_progress_order)
  if [[ -n "$id" ]]; then
    call GET "/orders/$id" "" "$CUSTOMER_TOKEN"
    say "  in-progress order $id: status $(jget "$BODY" "['body']['status']"), rider reported $(jget "$BODY" "['body']['rider_reported_stage']")"
  else
    say "  no in-progress order recorded (run --live)"
  fi
}

# ---- modes ------------------------------------------------------------------------------------------
MODE="${1:-full}"
case "$MODE" in
  --status)
    CUSTOMER_TOKEN=$(login "$CUSTOMER_EMAIL" "$CUSTOMER_PASSWORD"); show_status; exit 0 ;;
  --advance)
    setup >/dev/null
    id=$(state_get in_progress_order); [[ -n "$id" ]] || die "no in-progress order; run --live first"
    call GET "/orders/$id" "" "$CUSTOMER_TOKEN"; status=$(jget "$BODY" "['body']['status']")
    case "$status" in
      assigned)  step "Rider picks the order up";  pick_up "$id"; ok "order is now picked_up (on its way)" ;;
      picked_up) step "Rider delivers the order";  deliver "$id"; ok "order is now delivered"; state_set in_progress_order "" ;;
      delivered) die "that order is already delivered; run --live for a new in-progress one" ;;
      *)         die "the order is '$status' (the saga cancelled it, most likely because it waited over an hour); run --live" ;;
    esac
    exit 0 ;;
  --cancelled)
    setup; step "A cancelled order"; cancelled_order; exit 0 ;;
  --live)
    setup; release_busy_riders; in_progress_order; exit 0 ;;
  full) ;;
  *) die "unknown option '$MODE' (use --live, --cancelled, --advance or --status)" ;;
esac

setup
release_busy_riders

step "Delivered orders"
ORDERS=()
for spec in \
  '[{"item_id":"karahi","quantity":1},{"item_id":"lassi","quantity":1}]|karahi and lassi' \
  '[{"item_id":"burger","quantity":2},{"item_id":"brownie","quantity":1}]|burgers and a brownie' \
  '[{"item_id":"pizza","quantity":1},{"item_id":"salad","quantity":1}]|pizza and salad'; do
  items="${spec%%|*}"; label="${spec##*|}"
  id=$(place "$items" "$label") || exit 1; accept_and_assign "$id"; pick_up "$id"; deliver "$id"
  ORDERS+=("$id"); ok "delivered: $label  ($id)"
done

step "Reviews on the first two delivered orders"
call POST "/orders/${ORDERS[0]}/review" '{"rating":5,"comment":"Great karahi, nicely spicy, arrived hot."}' "$CUSTOMER_TOKEN"; ok "5 stars on the karahi order ($CODE)"
call POST "/orders/${ORDERS[1]}/review" '{"rating":3,"comment":"Burgers were good but the delivery took a little while."}' "$CUSTOMER_TOKEN"; ok "3 stars on the burger order ($CODE)"

step "A cancelled order (the kitchen turns it down, the payment is refunded)"
cancelled_order

in_progress_order

step "Done"
cat <<EOF

  Sign in at  ${BOLD}$BASE_URL/chat/${RESET}
    email     $CUSTOMER_EMAIL
    password  $( [[ "$CUSTOMER_EMAIL" == "demo.customer@smartfoodops.demo" ]] && echo "$DEMO_PASSWORD" || echo "(yours)" )

  This account now has 3 delivered orders (2 reviewed), 1 cancelled and refunded, and 1 in progress.
  Walk-through of the questions to ask: readme/interview-demo-guide.md
  The in-progress order expires after about an hour: run  ./scripts/seed-demo.sh --live  just before you need it.
EOF
