-- ============================================================================
-- One-off cleanup for an already-running sfo-menu-db container (D50).
--
-- Run only after db/menu/migrate_categories_to_relational.sql reports a verified match
-- between the old JSONB tree's item count and the new tables' row count:
--   docker exec -i sfo-menu-db psql -U <user> -d sfo_menu_core < db/menu/drop_menu_categories_column.sql
--
-- The new Menu Service image must already be deployed and serving before this runs — the
-- old image still reads/writes `categories` directly, and dropping the column out from
-- under it would break every request in flight.
-- ============================================================================

BEGIN;

ALTER TABLE menus DROP COLUMN categories;

COMMIT;
