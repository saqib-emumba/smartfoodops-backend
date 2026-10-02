-- ============================================================================
-- One-off migration for an already-running sfo-order-db container.
--
-- init.sql only runs on an empty Postgres volume (docker-entrypoint-initdb.d), so a
-- deployment that already has data needs this run by hand instead:
--   docker exec -i sfo-order-db psql -U <user> -d sfo_order_core < db/order/add_compensation_failed_status.sql
--
-- Adds 'compensation_failed' to order_status (mirroring db/order/init.sql exactly, which a
-- fresh volume already gets it from). Postgres 15 allows ALTER TYPE ... ADD VALUE inside a
-- transaction block, as long as the new value is not *used* in that same transaction — this
-- file only adds it, so BEGIN/COMMIT here is safe the same way it is in
-- migrate_items_to_relational.sql.
-- ============================================================================

BEGIN;

ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'compensation_failed';

COMMIT;
