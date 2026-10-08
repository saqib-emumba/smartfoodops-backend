-- ============================================================================
-- Week 4 (D60): owner-declared dietary tags on menu items.
--
-- Hand-run, once, against an existing sfo_menu_core volume — a fresh volume gets the column
-- from db/menu/init.sql. There is no migration tooling here (see README), so this follows
-- db/user/add_rbac_permissions.sql: idempotent, in one transaction, safe to re-run.
--
--   docker exec -i sfo-menu-db psql -U sfo_menu_admin -d sfo_menu_core < db/menu/add_dietary_tags.sql
--
-- Deploy order: run this BEFORE starting the new menu-service build, which writes and reads
-- the column. Existing items get '{}' (no tags), which is correct: nobody has declared any.
-- ============================================================================

BEGIN;

ALTER TABLE menu_items
    ADD COLUMN IF NOT EXISTS dietary_tags TEXT[] NOT NULL DEFAULT '{}';

COMMIT;

-- Verify (expect one row, data_type = ARRAY):
-- SELECT column_name, data_type FROM information_schema.columns
--  WHERE table_name = 'menu_items' AND column_name = 'dietary_tags';
