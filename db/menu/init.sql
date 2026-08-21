-- ============================================================================
-- Menu Service database — sfo_menu_core (container sfo-menu-db, host port 5436)
--
-- Owns `menus`. Only the Menu Service connects here; every other service reads a menu
-- through GET /api/v1/menus/{restaurant_id}.
--
-- This table replaced the MongoDB `menus` collection. The document shape survived the
-- move intact inside a single JSONB column: a menu is read and written whole, by
-- restaurant, so splitting the category/item/option tree into three relational tables
-- would buy joins nobody performs and cost a transaction on every publish.
--
-- `restaurant_id` is a plain UUID pointing into the Restaurant Service's database, where
-- no foreign key can follow it, so the restaurant is verified over HTTP before the upsert
-- (see services/menu/clients.py) instead of by the engine.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Menus Table (One Row Per Restaurant, Whole Category Tree In JSONB)
CREATE TABLE IF NOT EXISTS menus (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- UNIQUE is what makes "publish a menu" an upsert rather than an append: one live
    -- menu per restaurant, enforced by the engine instead of by the application.
    restaurant_id UUID UNIQUE NOT NULL, -- restaurants.id (Restaurant Service database)
    categories JSONB NOT NULL DEFAULT '[]'::jsonb, -- Nested categories -> items -> customization groups -> options
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- No index is declared here on purpose. Every read is `WHERE restaurant_id = ...`, which
-- the UNIQUE constraint above already backs with a btree index; a second one would be
-- dead weight. A GIN index over `categories` would only pay for itself once something
-- searches *inside* the tree (e.g. "which restaurants serve a vegan main?"), and until
-- then it is a write cost on every publish for a query nobody issues.
