-- ============================================================================
-- One-off migration for an already-running sfo-menu-db container (D50).
--
-- init.sql only runs on an empty Postgres volume (docker-entrypoint-initdb.d), so a
-- deployment that already has data needs this run by hand instead:
--   docker exec -i sfo-menu-db psql -U <user> -d sfo_menu_core < db/menu/migrate_categories_to_relational.sql
--
-- Creates the four child tables (mirroring db/menu/init.sql exactly, IF NOT EXISTS, since
-- this volume predates them) and unpacks every existing menu's `categories` JSONB tree
-- into them. `categories` itself is untouched here — see the verification query and the
-- separate, gated db/menu/drop_menu_categories_column.sql for that.
--
-- Explicit nested loops rather than a chain of INSERT ... RETURNING CTEs: this walks a
-- four-level tree, and a PL/pgSQL loop reads the same way the tree itself does, which
-- matters more here than it would for a flat backfill.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS menu_categories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    menu_id UUID NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    category_key VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    display_order INT NOT NULL DEFAULT 1,
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_menu_categories_menu ON menu_categories(menu_id);

CREATE TABLE IF NOT EXISTS menu_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    category_id UUID NOT NULL REFERENCES menu_categories(id) ON DELETE CASCADE,
    item_key VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    base_price DECIMAL(10, 2) NOT NULL CHECK (base_price > 0),
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    dietary_flags TEXT[] NOT NULL DEFAULT '{}',
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_menu_items_category ON menu_items(category_id);

CREATE TABLE IF NOT EXISTS menu_item_customization_groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    item_id UUID NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
    group_key VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    min_selection INT NOT NULL DEFAULT 1 CHECK (min_selection >= 0),
    max_selection INT NOT NULL DEFAULT 1 CHECK (max_selection >= 1),
    CHECK (min_selection <= max_selection),
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_menu_custom_groups_item ON menu_item_customization_groups(item_id);

CREATE TABLE IF NOT EXISTS menu_item_customization_options (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    group_id UUID NOT NULL REFERENCES menu_item_customization_groups(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    extra_price DECIMAL(10, 2) NOT NULL DEFAULT 0.0 CHECK (extra_price >= 0),
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_menu_custom_options_group ON menu_item_customization_options(group_id);

-- Backfill: unpack every menu's existing `categories` JSONB tree, in submission order.
DO $$
DECLARE
    menu_row RECORD;
    category_json JSONB;
    item_json JSONB;
    group_json JSONB;
    option_json JSONB;
    category_pos SMALLINT;
    item_pos SMALLINT;
    group_pos SMALLINT;
    option_pos SMALLINT;
    new_category_id UUID;
    new_item_id UUID;
    new_group_id UUID;
BEGIN
    FOR menu_row IN SELECT id, categories FROM menus LOOP
        category_pos := 0;
        FOR category_json IN SELECT * FROM jsonb_array_elements(menu_row.categories) LOOP
            INSERT INTO menu_categories (menu_id, category_key, name, display_order, position)
            VALUES (
                menu_row.id,
                category_json->>'category_id',
                category_json->>'category_name',
                COALESCE((category_json->>'display_order')::int, 1),
                category_pos
            )
            RETURNING id INTO new_category_id;

            item_pos := 0;
            FOR item_json IN SELECT * FROM jsonb_array_elements(COALESCE(category_json->'items', '[]'::jsonb)) LOOP
                INSERT INTO menu_items
                    (category_id, item_key, name, description, base_price, is_available, dietary_flags, position)
                VALUES (
                    new_category_id,
                    item_json->>'item_id',
                    item_json->>'name',
                    item_json->>'description',
                    (item_json->>'base_price')::decimal,
                    COALESCE((item_json->>'is_available')::boolean, true),
                    COALESCE(
                        (SELECT array_agg(flag) FROM jsonb_array_elements_text(
                            COALESCE(item_json->'dietary_flags', '[]'::jsonb)
                        ) AS flag),
                        '{}'
                    ),
                    item_pos
                )
                RETURNING id INTO new_item_id;

                group_pos := 0;
                FOR group_json IN SELECT * FROM jsonb_array_elements(
                    COALESCE(item_json->'customization_groups', '[]'::jsonb)
                ) LOOP
                    INSERT INTO menu_item_customization_groups
                        (item_id, group_key, name, min_selection, max_selection, position)
                    VALUES (
                        new_item_id,
                        group_json->>'group_id',
                        group_json->>'group_name',
                        COALESCE((group_json->>'min_selection')::int, 1),
                        COALESCE((group_json->>'max_selection')::int, 1),
                        group_pos
                    )
                    RETURNING id INTO new_group_id;

                    option_pos := 0;
                    FOR option_json IN SELECT * FROM jsonb_array_elements(
                        COALESCE(group_json->'options', '[]'::jsonb)
                    ) LOOP
                        INSERT INTO menu_item_customization_options (group_id, name, extra_price, position)
                        VALUES (
                            new_group_id,
                            option_json->>'name',
                            COALESCE((option_json->>'extra_price')::decimal, 0.0),
                            option_pos
                        );
                        option_pos := option_pos + 1;
                    END LOOP;

                    group_pos := group_pos + 1;
                END LOOP;

                item_pos := item_pos + 1;
            END LOOP;

            category_pos := category_pos + 1;
        END LOOP;
    END LOOP;
END $$;

COMMIT;

-- --- Verify before proceeding --------------------------------------------------------
-- SELECT COALESCE(SUM(jsonb_array_length(categories)), 0) FROM menus;
-- SELECT count(*) FROM menu_categories;
-- These two counts must match (one row per category) before running
-- db/menu/drop_menu_categories_column.sql. Spot-check a menu or two with:
--   SELECT m.restaurant_id, mc.name, mi.name, mi.base_price
--     FROM menus m JOIN menu_categories mc ON mc.menu_id = m.id
--                  JOIN menu_items mi ON mi.category_id = mc.id
--    WHERE m.restaurant_id = '<some restaurant id>'
--    ORDER BY mc.position, mi.position;
