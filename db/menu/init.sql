-- ============================================================================
-- Menu Service database — sfo_menu_core (container sfo-menu-db, host port 5436)
--
-- Owns `menus` and its four child tables. Only the Menu Service connects here; every
-- other service reads a menu through GET /api/v1/menus/{restaurant_id}.
--
-- This table replaced the MongoDB `menus` collection, and briefly held the whole
-- category/item/option tree in a single JSONB column (D22). D50 normalized that tree
-- into the five tables below: database-enforced integrity (an item can't reference a
-- nonexistent category, a price can't be negative) and future per-item queryability beat
-- the cost of a multi-statement transaction on every publish, largely absorbed by the
-- Redis cache-aside layer (D23) on the read side.
--
-- `restaurant_id` is a plain UUID pointing into the Restaurant Service's database, where
-- no foreign key can follow it, so the restaurant is verified over HTTP before the upsert
-- (see services/menu/clients.py) instead of by the engine.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Menus Table (One Row Per Restaurant — Now Just An Anchor For The Category Tree)
CREATE TABLE IF NOT EXISTS menus (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- UNIQUE is what makes "publish a menu" an upsert rather than an append: one live
    -- menu per restaurant, enforced by the engine instead of by the application.
    restaurant_id UUID UNIQUE NOT NULL, -- restaurants.id (Restaurant Service database)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Menu Categories (One Menu Has Many Categories)
CREATE TABLE IF NOT EXISTS menu_categories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    menu_id UUID NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    category_key VARCHAR(255) NOT NULL, -- client-supplied category_id, kept as a stable business key
    name VARCHAR(255) NOT NULL,
    display_order INT NOT NULL DEFAULT 1, -- the client's own field; not necessarily unique or gapless
    -- Round-trips the exact order the tree was submitted in, independent of display_order
    -- above (a data field the client controls, not a physical ordinal) — set from each
    -- item/group/option's index in its parent array at publish time.
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_menu_categories_menu ON menu_categories(menu_id);

-- 3. Menu Items (One Category Has Many Items)
CREATE TABLE IF NOT EXISTS menu_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    category_id UUID NOT NULL REFERENCES menu_categories(id) ON DELETE CASCADE,
    item_key VARCHAR(255) NOT NULL, -- client-supplied item_id
    name VARCHAR(255) NOT NULL,
    description TEXT,
    base_price DECIMAL(10, 2) NOT NULL CHECK (base_price > 0),
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    -- Flat scalar tag list, not a nested entity of its own (no attributes beyond the tag
    -- name, never joined or aggregated across items) — an array column, not a sixth table.
    dietary_flags TEXT[] NOT NULL DEFAULT '{}',
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_menu_items_category ON menu_items(category_id);

-- 4. Menu Item Customization Groups (One Item Has Many Groups, e.g. "Size", "Toppings")
CREATE TABLE IF NOT EXISTS menu_item_customization_groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    item_id UUID NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
    group_key VARCHAR(255) NOT NULL, -- client-supplied group_id
    name VARCHAR(255) NOT NULL,
    min_selection INT NOT NULL DEFAULT 1 CHECK (min_selection >= 0),
    max_selection INT NOT NULL DEFAULT 1 CHECK (max_selection >= 1),
    CHECK (min_selection <= max_selection),
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_menu_custom_groups_item ON menu_item_customization_groups(item_id);

-- 5. Menu Item Customization Options (One Group Has Many Options, e.g. "Small"/"Large")
CREATE TABLE IF NOT EXISTS menu_item_customization_options (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    group_id UUID NOT NULL REFERENCES menu_item_customization_groups(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    extra_price DECIMAL(10, 2) NOT NULL DEFAULT 0.0 CHECK (extra_price >= 0),
    position SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_menu_custom_options_group ON menu_item_customization_options(group_id);
