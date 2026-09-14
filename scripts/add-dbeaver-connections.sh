#!/usr/bin/env bash
# SmartFoodOps — register all 7 per-service Postgres connections in DBeaver.
#
# Local dev convenience only: not part of smoke-test.sh or saga-resilience-test.sh, and not
# run in CI. Each service owns one database (D01), each publishing a distinct host port
# (docker-compose.yml, db-*-postgres blocks), so DBeaver needs one connection per service
# rather than one connection browsing multiple databases.
#
# KNOWN BROKEN on DBeaver 26.2.0 (Community, macOS): running this launches DBeaver and the
# connections appear in the Navigator for that session, but nothing is ever written to
# `~/Library/DBeaverData/workspace6/General/.dbeaver/data-sources.json` — not even on a
# clean Cmd+Q quit — so every connection is gone on the next launch. A connection created by
# hand through Database > New Database Connection *does* persist correctly, so the gap is
# specific to `-con`-created connections on this build, not DBeaver's save path in general.
# Root cause not identified (no error in dbeaver-debug.log); worth retrying against a newer
# DBeaver release before relying on this script again.
#
# Until that's fixed, the reliable path is: add one connection by hand (above), quit and
# confirm it survives, then hand-edit `data-sources.json` to add the rest under the same
# "SmartFoodOps" folder key, matching the schema DBeaver itself wrote for that first one.
# Passwords live in a separate, DBeaver-managed `credentials-config.json` — don't hand-edit
# that file; let DBeaver prompt for each password on first connect and save it from there.
#
# Usage: ./scripts/add-dbeaver-connections.sh
# Safe to re-run: create=true (re)creates/updates each connection by name.
#
# Passwords below match the local-dev defaults in .env. If you've changed any
# *_POSTGRES_PASSWORD value there, update the matching line below to match.
set -euo pipefail

DBEAVER="/Applications/DBeaver.app/Contents/MacOS/dbeaver"

if [ ! -x "$DBEAVER" ]; then
  echo "DBeaver not found at $DBEAVER — adjust the path (Community vs Enterprise builds differ)." >&2
  exit 1
fi

# All -con blocks go to ONE launch of the binary. Looping and invoking dbeaver once per
# connection (the first cut of this script did that) races: if DBeaver wasn't already
# running, each launch can start its own process before the first one's IPC listener comes
# up, so you get several independent instances each holding a private in-memory copy of the
# workspace — closing whichever one you actually see persists only what that instance got,
# sometimes none of it. One process, one workspace write, every time.
declare -a CONNS=(
  "SFO User DB|5432|sfo_user_core|sfo_user_admin|sfo_user_password_123"
  "SFO Restaurant DB|5433|sfo_restaurant_core|sfo_restaurant_admin|sfo_restaurant_password_123"
  "SFO Order DB|5434|sfo_order_core|sfo_order_admin|sfo_order_password_123"
  "SFO Payment DB|5435|sfo_payment_core|sfo_payment_admin|sfo_payment_password_123"
  "SFO Menu DB|5436|sfo_menu_core|sfo_menu_admin|sfo_menu_password_123"
  "SFO Rider DB|5437|sfo_rider_core|sfo_rider_admin|sfo_rider_password_123"
  "SFO Analytics DB|5438|sfo_analytics_core|sfo_analytics_admin|sfo_analytics_password_123"
)

args=(-nosplash)
for entry in "${CONNS[@]}"; do
  IFS='|' read -r name port db user pass <<< "$entry"
  args+=(-con "driver=postgres-jdbc|name=${name}|host=localhost|port=${port}|database=${db}|user=${user}|password=${pass}|savePassword=true|create=true|folder=SmartFoodOps")
done

"$DBEAVER" "${args[@]}"

echo "Done. Open DBeaver and check the 'SmartFoodOps' folder in the Database Navigator."
echo "If it's not already running, DBeaver's GUI may take a few seconds to finish starting"
echo "before the connections appear — give it a moment rather than re-running the script."
