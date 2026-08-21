-- ============================================================================
-- Restaurant Service database — sfo_restaurant_core
-- (container sfo-restaurant-db, host port 5433)
--
-- Owns `restaurants`, and nothing else. Only the Restaurant Service connects
-- here; every other service reads a restaurant through
-- GET /api/v1/restaurants/{restaurant_id}.
--
-- An `order_tickets` kitchen queue lived here through the first cut of Week 2.
-- It held a status, an items snapshot and a decision timestamp — none of which
-- was restaurant-domain data that `orders` did not already have — so the whole
-- table was a cross-database hop for facts about an order's lifecycle. D32 moved
-- the kitchen's decision onto `orders.kitchen_decision`, which took the saga's
-- dependency on this service to zero.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Restaurants Table
CREATE TABLE IF NOT EXISTS restaurants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- users.id, which lives in the User Service database. There is no foreign key to
    -- enforce across databases, so the owner is verified over HTTP before onboarding.
    owner_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    address TEXT NOT NULL,
    latitude DECIMAL(9, 6) NOT NULL,
    longitude DECIMAL(9, 6) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    capacity INT NOT NULL DEFAULT 50,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for restaurants geo queries
CREATE INDEX IF NOT EXISTS idx_restaurants_geo ON restaurants(latitude, longitude);

-- Owner lookups ("list my restaurants") scan by owner, so index the reference.
CREATE INDEX IF NOT EXISTS idx_restaurants_owner ON restaurants(owner_id);
