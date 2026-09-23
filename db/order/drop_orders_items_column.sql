-- ============================================================================
-- One-off cleanup for an already-running sfo-order-db container (D50).
--
-- Run only after db/order/migrate_items_to_relational.sql reports a verified match
-- between the old JSONB array's item count and the new tables' row count:
--   docker exec -i sfo-order-db psql -U <user> -d sfo_order_core < db/order/drop_orders_items_column.sql
--
-- The new Order Service image must already be deployed and serving before this runs —
-- the old image still reads/writes `items` directly, and dropping the column out from
-- under it would break checkout and every order read in flight.
-- ============================================================================

BEGIN;

ALTER TABLE orders DROP COLUMN items;

COMMIT;
