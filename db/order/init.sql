-- ============================================================================
-- Order Service database — sfo_order_core (container sfo-order-db, host port 5434)
--
-- Owns `orders` and the append-only `order_tracking_logs` trail beside it. Only the
-- Order Service connects here.
--
-- `payments` used to live here too. It now belongs to the Payment Service's own database
-- (sfo_payment_core), which is why neither the table nor the `payment_status` enum is
-- declared below — see readme/payments-service-migration.md.
--
-- `order_tracking_logs` moved the other way: it used to be a MongoDB collection owned by
-- the Menu Service, and came here because a status transition is an Order Service fact —
-- see readme/postgres-menu-tracking-migration-v2.md.
--
-- Every column pointing at another service's table is a plain UUID: a foreign key
-- cannot span physical databases, so the reference is verified over HTTP before the
-- insert (see services/order/clients.py) instead of by the engine. The one real foreign
-- key here is `order_tracking_logs.order_id`, because both ends live in this database.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Define custom ENUM types
CREATE TYPE order_status AS ENUM ('created', 'confirmed', 'assigned', 'picked_up', 'delivered', 'cancelled');

-- 1. Orders Table (Primary Registry with JSONB Items and Idempotency Guard)
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL,   -- users.id       (User Service database)
    restaurant_id UUID NOT NULL, -- restaurants.id (Restaurant Service database)
    rider_id UUID,               -- riders.id      (User Service database)
    items JSONB NOT NULL, -- Stores snapshot of ordered items, prices, and selected customization options at checkout
    total_amount DECIMAL(10, 2) NOT NULL,
    status order_status NOT NULL DEFAULT 'created',
    idempotency_key VARCHAR(255) UNIQUE, -- Protects order creation writes against API duplicate submissions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Order-history reads filter by customer, which no longer benefits from a foreign key.
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);

-- 2. Order Tracking Logs (Append-Only Audit Trail Of Status Transitions)
-- One row per transition rather than an array on `orders`: appending to a JSONB column
-- rewrites the whole order row under MVCC, so a chatty delivery would rewrite the order
-- once per GPS ping. Inserts here touch nothing the checkout path reads.
CREATE TABLE IF NOT EXISTS order_tracking_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- A genuine foreign key, which the MongoDB collection could not have: an entry for an
    -- order that does not exist is rejected outright, and deleting an order takes its
    -- trail with it rather than orphaning rows nothing will ever read again.
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    -- Append order, and the reason it is a sequence rather than a timestamp: entries
    -- written inside one transaction share a `created_at`, and "the status before this
    -- one" has to be answerable without a tie-break.
    seq BIGSERIAL NOT NULL,
    old_status order_status,          -- Filled in server-side from the preceding entry; NULL on the first
    new_status order_status NOT NULL, -- Same enum as orders.status, so an invented status name is rejected by the engine
    service VARCHAR(100) NOT NULL,    -- Microservice that observed the transition
    updated_by VARCHAR(100) NOT NULL DEFAULT 'system', -- Actor on whose behalf it happened
    raw_log TEXT,                     -- Event payload as the emitting service serialised it
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, -- Dynamic per-event fields (idempotency key, ETA, coordinates)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Serves both reads this table has: the chronological timeline for one order, and the
-- single-row "what was the status before this entry?" lookup that fills `old_status`.
CREATE INDEX IF NOT EXISTS idx_tracking_order_timeline ON order_tracking_logs(order_id, seq DESC);
