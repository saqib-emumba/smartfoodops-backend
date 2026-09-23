#!/usr/bin/env bash
#
# One-off migration for an already-running stack: copies every rider's last-reported
# location out of Postgres (sfo-rider-db) into the new Redis GEO index (sfo-redis, logical
# database 2), as part of D49 — see readme/rider-location-redis-design.md.
#
# This can't be a pure SQL script like db/user/backfill_user_roles.sql: it crosses two
# systems. Run this immediately before redeploying the new rider-service image (the old
# image keeps writing current_latitude/current_longitude until it's replaced, so minimising
# the gap between this read and the redeploy minimises how many pings land in between and
# are lost — acceptable, since that data was already loseable by design).
#
# Only after this reports a verified match should db/rider/drop_rider_location_columns.sql
# be run against sfo-rider-db.
#
# Usage:
#   ./scripts/migrate_rider_locations_to_redis.sh
#
set -euo pipefail

PSQL=(docker exec sfo-rider-db psql -U sfo_rider_admin -d sfo_rider_core -tA -F',')
REDIS=(docker exec -i sfo-redis redis-cli -n 2)

echo "Reading riders with a reported location from sfo-rider-db..."
ROWS=$("${PSQL[@]}" -c \
  "SELECT user_id, current_latitude, current_longitude FROM riders
    WHERE current_latitude IS NOT NULL AND current_longitude IS NOT NULL;")

PG_COUNT=0
while IFS=',' read -r user_id lat lon; do
  [[ -z "$user_id" ]] && continue
  "${REDIS[@]}" GEOADD riders:geo "$lon" "$lat" "$user_id" >/dev/null
  PG_COUNT=$((PG_COUNT + 1))
done <<<"$ROWS"

echo "Wrote $PG_COUNT location(s) into riders:geo."

REDIS_COUNT=$("${REDIS[@]}" ZCARD riders:geo)

echo "Postgres rows with a location: $PG_COUNT"
echo "Redis riders:geo cardinality:  $REDIS_COUNT"

if [[ "$PG_COUNT" -ne "$REDIS_COUNT" ]]; then
  echo "MISMATCH: counts do not agree. Do NOT run drop_rider_location_columns.sql yet." >&2
  exit 1
fi

echo
echo "Spot-check a few rows before trusting this (GEO's geohash quantisation is coarser"
echo "than DECIMAL(9,6) — expect close agreement, not bit-exact equality):"
echo
while IFS=',' read -r user_id lat lon; do
  [[ -z "$user_id" ]] && continue
  echo "  $user_id: Postgres ($lat, $lon) vs Redis $("${REDIS[@]}" GEOPOS riders:geo "$user_id")"
done < <(head -n 5 <<<"$ROWS")

echo
echo "Counts match. Once you have spot-checked the values above, it is safe to redeploy"
echo "the new rider-service image and then run db/rider/drop_rider_location_columns.sql."
