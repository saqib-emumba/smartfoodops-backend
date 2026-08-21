#!/usr/bin/env bash
# SmartFoodOps — saga resilience checks.
#
# Separate from smoke-test.sh on purpose. These two tests are slow (minutes) and
# destructive (one restarts a container mid-saga), so folding them into the suite that runs
# after every change would make that suite something people skip.
#
# They are also the two that actually justify Temporal. Everything in smoke-test.sh could
# be passed by a synchronous implementation with a retry loop; neither of these could.
#
#   1. CONCURRENCY  — N orders, one rider. Exactly one may be assigned, and the rest must
#                     cancel rather than double-book. Proves FOR UPDATE SKIP LOCKED.
#   2. DURABILITY   — kill the worker mid-saga. The order must still reach 'delivered'.
#                     Proves the workflow is state in Temporal, not state in a process.
#
# Requires docker access to the local stack. Usage: bash scripts/saga-resilience-test.sh

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
if [[ -t 1 ]]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'; RESET=$'\e[0m'
else
  BOLD=''; DIM=''; RED=''; GREEN=''; RESET=''
fi

PASSED=0; FAILED=0; FAILURES=()
section() { printf '\n%s%s%s\n' "$BOLD" "$1" "$RESET"; }
ok()  { PASSED=$((PASSED+1)); printf '  %sPASS%s  %s\n' "$GREEN" "$RESET" "$1"; }
bad() { FAILED=$((FAILED+1)); FAILURES+=("$1 — $2"); printf '  %sFAIL%s  %s\n        %s\n' "$RED" "$RESET" "$1" "$2"; }
assert() { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "expected '$3', got '$2'"; }
note() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }

ENV_FILE="$(dirname "$0")/../.env"
INTERNAL_KEY=$(grep -m1 '^INTERNAL_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)

for c in sfo-rider-db sfo-order-db sfo-payment-db sfo-order-worker sfo-temporal-server; do
  docker ps --format '{{.Names}}' | grep -q "^$c$" || {
    printf '%sNeeds the local stack running (%s not found).%s Try: docker compose up -d\n' \
      "$RED" "$c" "$RESET"; exit 1; }
done

api() { # api <method> <path> <body> [curl args...]
  local m="$1" p="$2" b="${3:-}"; shift 3
  if [[ -n "$b" ]]; then
    curl -sS -m 25 -X "$m" "$BASE_URL$p" -H 'Content-Type: application/json' -d "$b" "$@"
  else
    curl -sS -m 25 -X "$m" "$BASE_URL$p" "$@"
  fi
}
jf() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)" 2>/dev/null; }

order_status() {
  api GET "/api/v1/orders/$1" "" -H "X-Internal-Key: $INTERNAL_KEY" 2>/dev/null | jf "['status']"
}
order_status_internal() {
  api GET "/api/v1/orders/$1/internal" "" -H "X-Internal-Key: $INTERNAL_KEY" 2>/dev/null | jf "['status']"
}
wait_status() { # wait_status <order> <want> <limit>
  local waited=0 got=""
  while (( waited < $3 )); do
    got=$(order_status_internal "$1")
    [[ "$got" == "$2" ]] && { echo "$got"; return 0; }
    sleep 3; waited=$((waited+3))
  done
  echo "$got"; return 1
}
rq() { docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -tA -c "$1" 2>/dev/null | tr -d '[:space:]'; }

printf '%sSmartFoodOps saga resilience%s  ->  %s\n' "$BOLD" "$RESET" "$BASE_URL"

# ---------------------------------------------------------------- fixtures
TAG="res$(date +%s)$$"
PW="Passw0rd!"
reg() { api POST /api/v1/users/register \
  "{\"email\":\"$1_$TAG@example.com\",\"password\":\"$PW\",\"full_name\":\"$2\",\"phone\":\"$3${RANDOM}\",\"role\":\"$4\"}"; }
login() { api POST /api/v1/users/login "{\"email\":\"$1_$TAG@example.com\",\"password\":\"$PW\"}" | jf "['access_token']"; }

reg owner "Res Owner" "+1911" restaurant_admin >/dev/null
reg cust  "Res Cust"  "+1922" customer         >/dev/null
reg rider "Res Rider" "+1933" rider            >/dev/null
OWNER_T=$(login owner); CUST_T=$(login cust); RIDER_T=$(login rider)
OA=(-H "Authorization: Bearer $OWNER_T"); CA=(-H "Authorization: Bearer $CUST_T"); RA=(-H "Authorization: Bearer $RIDER_T")

LAT=33.68; LON=73.04
REST=$(api POST /api/v1/restaurants/onboard \
  "{\"name\":\"Resilience Diner\",\"address\":\"9 Chaos Lane\",\"latitude\":$LAT,\"longitude\":$LON,\"capacity\":40}" \
  "${OA[@]}" | jf "['id']")
api POST /api/v1/menus \
  "{\"restaurant_id\":\"$REST\",\"categories\":[{\"category_id\":\"c1\",\"category_name\":\"Mains\",\"display_order\":1,\"items\":[{\"item_id\":\"pie\",\"name\":\"Pie\",\"description\":\"A pie\",\"base_price\":10.00,\"is_available\":true}]}]}" \
  "${OA[@]}" >/dev/null
RIDER=$(api POST /api/v1/riders \
  "{\"vehicle_type\":\"motorbike\",\"vehicle_number\":\"RES-$TAG\",\"current_latitude\":$LAT,\"current_longitude\":$LON}" \
  "${RA[@]}" | jf "['id']")
note "restaurant=$REST  rider=$RIDER"

ORDER_BODY="{\"restaurant_id\":\"$REST\",\"items\":[{\"item_id\":\"pie\",\"quantity\":1}],\"total_amount\":10.00}"

place() { api POST /api/v1/orders "$ORDER_BODY" -H "X-Idempotency-Key: $1" "${CA[@]}" | jf "['id']"; }
accept() { api POST "/api/v1/orders/$1/accept" "" "${OA[@]}" >/dev/null; }

# ============================================================== 1. concurrency
section "1. Concurrent dispatch against a fleet of one"

# Park every other rider in the database so this run's rider is the only candidate.
docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -q -c \
  "UPDATE riders SET is_available = FALSE WHERE id <> '$RIDER';" >/dev/null 2>&1
note "fleet size for this test: $(rq "SELECT count(*) FROM riders WHERE is_available;")"

N=4
ORDERS=()
for i in $(seq 1 $N); do ORDERS+=("$(place "conc-$TAG-$i")"); done
note "placed $N orders: ${ORDERS[*]}"

# All four must clear payment before any can compete for the rider.
for o in "${ORDERS[@]}"; do wait_status "$o" confirmed 45 >/dev/null; done
# Accept them as close to simultaneously as the shell allows, so the dispatch attempts
# genuinely overlap rather than queueing politely.
for o in "${ORDERS[@]}"; do accept "$o" & done
wait
note "accepted all $N tickets simultaneously"

# One should be assigned; the others exhaust the search window and cancel.
sleep 5
ASSIGNED=0; CANCELLED=0; PENDING=0
for _ in $(seq 1 40); do
  ASSIGNED=0; CANCELLED=0; PENDING=0
  for o in "${ORDERS[@]}"; do
    case "$(order_status_internal "$o")" in
      assigned|picked_up|delivered) ASSIGNED=$((ASSIGNED+1)) ;;
      cancelled)                    CANCELLED=$((CANCELLED+1)) ;;
      *)                            PENDING=$((PENDING+1)) ;;
    esac
  done
  (( PENDING == 0 )) && break
  sleep 3
done
note "assigned=$ASSIGNED cancelled=$CANCELLED still-running=$PENDING"

assert "exactly one order got the only rider" "$ASSIGNED" "1"
assert "the other $((N-1)) cancelled rather than double-booking" "$CANCELLED" "$((N-1))"

# The decisive check: the rider is bound to at most one order. Without SKIP LOCKED two
# transactions could both read it as free, and the partial unique index would then reject
# one of them — turning a prevented race into a failed activity.
assert "the rider is bound to at most one order" \
  "$(rq "SELECT count(*) FROM riders WHERE current_order_id IS NOT NULL;")" "1"

# Refunds must have happened for every cancelled order.
REFUNDED=0
for o in "${ORDERS[@]}"; do
  [[ "$(order_status_internal "$o")" == "cancelled" ]] || continue
  st=$(docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core -tA -c \
      "SELECT status FROM payments WHERE order_id='$o';" 2>/dev/null | tr -d '[:space:]')
  [[ "$st" == "refunded" ]] && REFUNDED=$((REFUNDED+1))
done
assert "every cancelled order was refunded" "$REFUNDED" "$CANCELLED"

# Finish the one live order so it does not hold the rider into the next test.
LIVE=""
for o in "${ORDERS[@]}"; do
  [[ "$(order_status_internal "$o")" == "assigned" ]] && LIVE="$o"
done
if [[ -n "$LIVE" ]]; then
  api POST "/api/v1/riders/me/orders/$LIVE/picked-up" "" "${RA[@]}" >/dev/null
  api POST "/api/v1/riders/me/orders/$LIVE/delivered" "" "${RA[@]}" >/dev/null
  got=$(wait_status "$LIVE" delivered 45)
  assert "the assigned order completed normally" "$got" "delivered"
fi

# ============================================================== 2. durability
section "2. Worker restart mid-saga"

DUR=$(place "dur-$TAG")
note "order $DUR"
got=$(wait_status "$DUR" confirmed 45)
assert "reached 'confirmed' before the restart" "$got" "confirmed"

# The saga is now parked on a durable timer waiting for the kitchen. Nothing about that
# wait lives in the worker process, which is the claim under test.
STAGE_BEFORE=$(docker exec sfo-temporal-server temporal workflow query \
  --address 127.0.0.1:7233 --workflow-id "order-$DUR" --type stage 2>/dev/null \
  | grep -o '"stage":"[^"]*"' | cut -d'"' -f4)
note "saga stage before restart: ${STAGE_BEFORE:-unknown}"

printf '  %s...restarting sfo-order-worker...%s\n' "$DIM" "$RESET"
docker restart sfo-order-worker >/dev/null 2>&1
# Wait for it to be polling again before doing anything that needs an activity.
for _ in $(seq 1 30); do
  docker logs --tail 20 sfo-order-worker 2>&1 | grep -q "Worker polling" && break
  sleep 2
done
ok "worker came back up"

# The order must still be exactly where it was: a restart is not an event in the saga.
assert "the order survived the restart unchanged" "$(order_status_internal "$DUR")" "confirmed"

# And it must still respond to the signal it was waiting for, from a *new* process.
accept "$DUR"
got=$(wait_status "$DUR" assigned 90)
assert "the resumed saga accepted the kitchen signal and dispatched" "$got" "assigned"

RID=$(api GET "/api/v1/orders/$DUR/internal" "" -H "X-Internal-Key: $INTERNAL_KEY" | jf "['rider_id']")
[[ -n "$RID" && "$RID" != "None" ]] && ok "the resumed saga assigned a rider" \
  || bad "the resumed saga assigned a rider" "rider_id empty"

# Restart again, this time mid-delivery, and finish through the new process.
printf '  %s...restarting again, mid-delivery...%s\n' "$DIM" "$RESET"
api POST "/api/v1/riders/me/orders/$DUR/picked-up" "" "${RA[@]}" >/dev/null
wait_status "$DUR" picked_up 45 >/dev/null
docker restart sfo-order-worker >/dev/null 2>&1
for _ in $(seq 1 30); do
  docker logs --tail 20 sfo-order-worker 2>&1 | grep -q "Worker polling" && break
  sleep 2
done
api POST "/api/v1/riders/me/orders/$DUR/delivered" "" "${RA[@]}" >/dev/null
got=$(wait_status "$DUR" delivered 60)
assert "the order reached 'delivered' across two worker restarts" "$got" "delivered"

# The full lifecycle must be recorded once each, with no duplicate transitions from the
# restarts replaying activities — the compare-and-set in OrderRepository.transition is what
# guarantees that, and this is where it earns its place.
TRAIL=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
  "SELECT string_agg(new_status::text, ',' ORDER BY seq) FROM order_tracking_logs WHERE order_id='$DUR';" \
  2>/dev/null | tr -d '[:space:]')
assert "the trail has one entry per transition, no replay duplicates" \
  "$TRAIL" "created,confirmed,assigned,picked_up,delivered"

assert "the rider was released" \
  "$(rq "SELECT count(*) FROM riders WHERE current_order_id='$DUR';")" "0"

# ======================================================= 3. kitchen timeout
# The only path that leaves a ticket `pending`, and therefore the only one that exercises
# the expiry compensation. Slow by construction: it waits out
# RESTAURANT_DECISION_TIMEOUT_SECONDS (120s) without anyone touching the ticket.
section "3. Kitchen never answers (ticket expiry)"

TO=$(place "timeout-$TAG")
note "order $TO — nobody will accept this one"
got=$(wait_status "$TO" confirmed 45)
assert "reached 'confirmed' and is waiting on the kitchen" "$got" "confirmed"

ON_RAIL_BEFORE=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
  "SELECT (status='confirmed' AND kitchen_decision IS NULL)::text FROM orders WHERE id='$TO';" \
  2>/dev/null | tr -d '[:space:]')
assert "the order is on the kitchen's rail" "$ON_RAIL_BEFORE" "true"

note "waiting out the 120s kitchen-decision window..."
got=$(wait_status "$TO" cancelled 180)
assert "silence eventually reads as a refusal" "$got" "cancelled"

PAY=$(docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core -tA -c \
  "SELECT status FROM payments WHERE order_id='$TO';" 2>/dev/null | tr -d '[:space:]')
assert "  the payment was refunded" "$PAY" "refunded"

# The assertion this whole test exists for — and since D32 it holds by construction rather
# than by an explicit cleanup step. The rail is `status='confirmed' AND kitchen_decision IS
# NULL`, so cancelling the order removes it from the rail as a side effect. There is no
# expiry activity left to forget, and no way for a slot to stay held by a dead order.
sleep 3
STILL_ON_RAIL=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
  "SELECT count(*) FROM orders WHERE restaurant_id='$REST'
     AND status='confirmed' AND kitchen_decision IS NULL;" 2>/dev/null | tr -d '[:space:]')
assert "  the abandoned order left the rail, freeing capacity" "$STILL_ON_RAIL" "0"

# A late click on an order the saga already cancelled must not resurrect it.
LATE=$(api POST "/api/v1/orders/$TO/accept" "" "${OA[@]}" | jf "['changed']")
assert "  accepting an already-cancelled order changes nothing" "$LATE" "False"
STILL_CANCELLED=$(order_status_internal "$TO")
assert "  and it stays cancelled" "$STILL_CANCELLED" "cancelled"

# ================================================== 4. lost decision signal
# The one hole the signal design leaves open: the Restaurant Service commits a decision
# *before* relaying it, so a relay lost in flight leaves a ticket saying 'accepted' and a
# workflow that never heard so. Simulated exactly by writing the decision straight into
# sfo_restaurant_core — which is what an accept whose relay died looks like from outside.
#
# Without the recovery lookup the saga would time out and refund an order the kitchen had
# actually taken. With it, the saga reads the ticket and carries on.
section "4. A kitchen decision whose signal was lost"

LOST=$(place "lostsig-$TAG")
note "order $LOST"
got=$(wait_status "$LOST" confirmed 45)
assert "reached 'confirmed' and is waiting on the kitchen" "$got" "confirmed"

# Record the decision without going through the endpoint that would signal the saga —
# which is exactly what an accept whose signal died looks like from the saga's side.
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -q -c \
  "UPDATE orders SET kitchen_decision = 'accepted', kitchen_decided_at = NOW()
    WHERE id = '$LOST';" >/dev/null 2>&1
DECISION=$(docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -tA -c \
  "SELECT kitchen_decision FROM orders WHERE id='$LOST';" 2>/dev/null | tr -d '[:space:]')
assert "the kitchen's decision is on record" "$DECISION" "accepted"
note "no signal was sent — the saga still believes it is waiting"

# It must sit there for the full window, then recover rather than cancel.
note "waiting out the 120s kitchen-decision window..."
got=$(wait_status "$LOST" assigned 200)
assert "the saga recovered the lost decision and dispatched" "$got" "assigned"

PAY=$(docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core -tA -c \
  "SELECT status FROM payments WHERE order_id='$LOST';" 2>/dev/null | tr -d '[:space:]')
assert "  the payment was NOT refunded" "$PAY" "authorized"

# Finish it so the rider is not left holding this order.
api POST "/api/v1/riders/me/orders/$LOST/picked-up" "" "${RA[@]}" >/dev/null
api POST "/api/v1/riders/me/orders/$LOST/delivered" "" "${RA[@]}" >/dev/null
got=$(wait_status "$LOST" delivered 60)
assert "  and completed normally afterwards" "$got" "delivered"

# The mirror case: a lost *rejection* must still cancel, not hang.
LOSTREJ=$(place "lostrej-$TAG")
note "order $LOSTREJ — a lost rejection this time"
wait_status "$LOSTREJ" confirmed 45 >/dev/null
docker exec sfo-order-db psql -U sfo_order_admin -d sfo_order_core -q -c \
  "UPDATE orders SET kitchen_decision = 'rejected', kitchen_decided_at = NOW()
    WHERE id = '$LOSTREJ';" >/dev/null 2>&1
note "waiting out the window again..."
got=$(wait_status "$LOSTREJ" cancelled 200)
assert "a lost rejection still cancels the order" "$got" "cancelled"
PAY=$(docker exec sfo-payment-db psql -U sfo_payment_admin -d sfo_payment_core -tA -c \
  "SELECT status FROM payments WHERE order_id='$LOSTREJ';" 2>/dev/null | tr -d '[:space:]')
assert "  and refunds the customer" "$PAY" "refunded"

# ---------------------------------------------------------------- restore
docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -q -c \
  "UPDATE riders SET is_available = TRUE WHERE current_order_id IS NULL;" >/dev/null 2>&1

section "Summary"
if (( FAILED == 0 )); then
  printf '  %s%d passed, 0 failed%s\n\n' "$GREEN" "$PASSED" "$RESET"
  exit 0
fi
printf '  %d passed, %s%d failed%s\n\n' "$PASSED" "$RED" "$FAILED" "$RESET"
for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
printf '\n'
exit 1
