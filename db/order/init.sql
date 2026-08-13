-- ============================================================================
-- Order Service database — sfo_order_core (container sfo-order-db, host port 5434)
--
-- Owns `orders` and `payments`. Only the Order Service connects here.
--
-- Every column pointing at another service's table is a plain UUID: a foreign key
-- cannot span physical databases, so the reference is verified over HTTP before the
-- insert (see services/order/clients.py) instead of by the engine.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Define custom ENUM types
CREATE TYPE order_status AS ENUM ('created', 'confirmed', 'assigned', 'picked_up', 'delivered', 'cancelled');
CREATE TYPE payment_status AS ENUM ('pending', 'authorized', 'captured', 'refunded');

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

-- 2. Payments Table (Built with Idempotency Protection)
-- Payments belong to the order lifecycle, so they stay in this database and keep a real
-- foreign key to `orders`.
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID UNIQUE NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status payment_status NOT NULL DEFAULT 'pending',
    transaction_reference VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
