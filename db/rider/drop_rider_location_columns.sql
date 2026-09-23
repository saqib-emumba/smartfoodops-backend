-- ============================================================================
-- One-off cleanup for an already-running sfo-rider-db container (D49).
--
-- Run only after scripts/migrate_rider_locations_to_redis.sh reports a verified match
-- between Postgres's row count and Redis's `ZCARD riders:geo`:
--   docker exec -i sfo-rider-db psql -U <user> -d sfo_rider_core < db/rider/drop_rider_location_columns.sql
--
-- The new rider-service image must already be deployed and serving before this runs — the
-- old image still writes current_latitude/current_longitude on every location ping, and
-- dropping the columns out from under it would break every in-flight write.
-- ============================================================================

BEGIN;

ALTER TABLE riders
    DROP COLUMN current_latitude,
    DROP COLUMN current_longitude;

DROP INDEX IF EXISTS idx_riders_dispatchable;

DROP FUNCTION IF EXISTS haversine_km(
    double precision, double precision, double precision, double precision
);

COMMIT;
