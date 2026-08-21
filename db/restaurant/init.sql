-- ============================================================================
-- Restaurant Service database — sfo_restaurant_core
-- (container sfo-restaurant-db, host port 5433)
--
-- Owns `restaurants` and the `order_tickets` kitchen queue. Only the Restaurant
-- Service connects here; every other service reads a restaurant through
-- GET /api/v1/restaurants/{restaurant_id}.
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

-- 2. Order Tickets (the kitchen queue) — added in Week 2.
--
-- A ticket is one order presented to one kitchen, and its status is the kitchen's
-- answer. It exists because the order saga has to wait for a human to accept or
-- decline: the Week 2 blueprint originally modelled acceptance as a synchronous
-- HTTP call that returned the decision, which no real kitchen can do (D27).
CREATE TYPE ticket_status AS ENUM ('pending', 'accepted', 'rejected', 'expired');

CREATE TABLE IF NOT EXISTS order_tickets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- orders.id, in sfo_order_core. UNIQUE rather than merely indexed: the workflow
    -- activity that creates a ticket is retried on any transport failure, and the
    -- second attempt must collide here instead of queueing the order twice.
    order_id UUID UNIQUE NOT NULL,
    -- Both ends of this reference live in this database, so unlike every other
    -- cross-service reference in the platform it gets a real foreign key — the same
    -- argument D24 made for moving the tracking trail beside the orders it
    -- describes. A ticket for a restaurant that does not exist is refused by the
    -- engine, and deleting a restaurant takes its queue with it.
    restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    -- The lines as priced by the Order Service, so the kitchen sees what was bought
    -- without a call back. A snapshot, not a live reference.
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    status ticket_status NOT NULL DEFAULT 'pending',
    decided_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Serves both reads that matter: the admin's queue ("my pending tickets, oldest
-- first") and the capacity check that counts them. Both filter on
-- (restaurant_id, status) and order by arrival, so one index covers them.
CREATE INDEX IF NOT EXISTS idx_tickets_queue
    ON order_tickets (restaurant_id, status, created_at);
