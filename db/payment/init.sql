-- ============================================================================
-- Payment Service database — sfo_payment_core (container sfo-payment-db, host port 5435)
--
-- Owns `payments`. Only the Payment Service connects here, which is the point of the
-- split: card handling is the one compliance boundary we want to be able to lock down
-- on its own, without dragging the order lifecycle inside it.
--
-- `order_id` used to be a real foreign key into `orders`, back when both tables shared
-- one database. It is now a plain UUID pointing into the Order Service's database, where
-- no foreign key can follow it, so the order is verified over HTTP before the insert
-- (see services/payment/clients.py) instead of by the engine.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Define custom ENUM types. `payment_status` moved here with the table; the Order
-- Service's database no longer declares it.
CREATE TYPE payment_status AS ENUM ('pending', 'authorized', 'captured', 'refunded');

-- 1. Payments Table (Built with Idempotency Protection)
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID UNIQUE NOT NULL, -- orders.id (Order Service database), no cross-DB FK
    -- Idempotency guard: protects transactions against double-charging under network retries
    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status payment_status NOT NULL DEFAULT 'pending',
    transaction_reference VARCHAR(255), -- External gateway id (e.g. a Stripe charge_id)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- The two hot lookups — by `idempotency_key` (replay detection, on every write) and by
-- `order_id` ("has this order been paid for?") — are already served by the UNIQUE
-- constraints above, which Postgres backs with a btree index each. A second index on
-- either column would be dead weight, so the only one declared here is for the reads that
-- have no constraint behind them: sweeping for payments left mid-flight.
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
