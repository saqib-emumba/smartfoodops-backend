-- ============================================================================
-- One-off migration for an already-running sfo-order-db container (D50).
--
-- init.sql only runs on an empty Postgres volume (docker-entrypoint-initdb.d), so a
-- deployment that already has data needs this run by hand instead:
--   docker exec -i sfo-order-db psql -U <user> -d sfo_order_core < db/order/migrate_items_to_relational.sql
--
-- Creates order_line_items and order_line_item_options (mirroring db/order/init.sql
-- exactly, IF NOT EXISTS, since this volume predates them) and unpacks every existing
-- order's `items` JSONB array into them. The column itself is not dropped here — see the
-- verification query and the separate, gated db/order/drop_orders_items_column.sql — but
-- its NOT NULL constraint is relaxed below, in this transaction. Unlike D49's
-- current_latitude/current_longitude, `items` has no default, so the new code (which
-- never sets it) would fail every INSERT until the constraint is gone — this cannot wait
-- for the deferred DROP COLUMN the way D49's cleanup could.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS order_line_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_no SMALLINT NOT NULL,
    menu_item_id VARCHAR(255) NOT NULL,
    item_name VARCHAR(255),
    quantity INT NOT NULL CHECK (quantity > 0),
    unit_price DECIMAL(10, 2) NOT NULL,
    line_total DECIMAL(10, 2) NOT NULL,
    customizations JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_line_items_order ON order_line_items(order_id);

CREATE TABLE IF NOT EXISTS order_line_item_options (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    line_item_id UUID NOT NULL REFERENCES order_line_items(id) ON DELETE CASCADE,
    group_key VARCHAR(255),
    name VARCHAR(255) NOT NULL,
    extra_price DECIMAL(10, 2) NOT NULL DEFAULT 0.0,
    position SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_order_line_item_options_line_item ON order_line_item_options(line_item_id);

-- The new code never sets this column, so it must stop being NOT NULL before that code can
-- insert a row at all — dropping the constraint (not the column) keeps the old data intact
-- and the column itself still there for db/order/drop_orders_items_column.sql to remove
-- later, once the backfill below is verified.
ALTER TABLE orders ALTER COLUMN items DROP NOT NULL;

-- Backfill: unpack every order's existing `items` JSONB array, in submission order.
DO $$
DECLARE
    order_row RECORD;
    item_json JSONB;
    option_json JSONB;
    line_pos SMALLINT;
    option_pos SMALLINT;
    new_line_item_id UUID;
BEGIN
    FOR order_row IN SELECT id, items FROM orders LOOP
        line_pos := 0;
        FOR item_json IN SELECT * FROM jsonb_array_elements(order_row.items) LOOP
            INSERT INTO order_line_items
                (order_id, line_no, menu_item_id, item_name, quantity, unit_price, line_total, customizations)
            VALUES (
                order_row.id,
                line_pos,
                item_json->>'item_id',
                item_json->>'name',
                (item_json->>'quantity')::int,
                (item_json->>'unit_price')::decimal,
                (item_json->>'line_total')::decimal,
                item_json->'customizations'
            )
            RETURNING id INTO new_line_item_id;

            option_pos := 0;
            FOR option_json IN SELECT * FROM jsonb_array_elements(
                COALESCE(item_json->'selected_options', '[]'::jsonb)
            ) LOOP
                INSERT INTO order_line_item_options (line_item_id, group_key, name, extra_price, position)
                VALUES (
                    new_line_item_id,
                    option_json->>'group_id',
                    option_json->>'name',
                    COALESCE((option_json->>'extra_price')::decimal, 0.0),
                    option_pos
                );
                option_pos := option_pos + 1;
            END LOOP;

            line_pos := line_pos + 1;
        END LOOP;
    END LOOP;
END $$;

COMMIT;

-- --- Verify before proceeding --------------------------------------------------------
-- SELECT COALESCE(SUM(jsonb_array_length(items)), 0) FROM orders;
-- SELECT count(*) FROM order_line_items;
-- These two counts must match (one row per line item) before running
-- db/order/drop_orders_items_column.sql.
