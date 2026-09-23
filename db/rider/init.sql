-- ============================================================================
-- Rider Service database — sfo_rider_core (container sfo-rider-db, host port 5437)
--
-- Owns the delivery fleet: who the riders are, where they are, and which order
-- each is carrying. Only the Rider Service connects here.
--
-- This table lived in sfo_user_core through Week 1, where a rider was treated as
-- an extension of a user identity and got a real foreign key to `users`. Week 2
-- moved it (D28): dispatch writes `is_available` and `current_order_id` on every
-- assignment, and under D01 a service may not write another service's tables.
-- The foreign key was the price of the move — `user_id` is now a plain UUID
-- verified over HTTP before the insert, like every other cross-service reference
-- in the platform (D02).
--
-- Week 4 (D49): live location moved out of this table entirely, into a Redis GEO
-- index (`riders:geo`, see services/rider/repositories/geo.py). Write-hot,
-- ephemeral position pings no longer compete with this table's `FOR UPDATE SKIP
-- LOCKED` dispatch claim, which is the one piece that still needs a real
-- transactional guarantee and stays here. `haversine_km` and the coordinate
-- columns went with it — nothing in this database computes or stores a position
-- any more.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Riders Table (the delivery fleet)
CREATE TABLE IF NOT EXISTS riders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Points at users(id) in sfo_user_core. No foreign key can follow it across a
    -- database boundary, so the Rider Service verifies the account exists and
    -- currently holds the `rider` role over HTTP before inserting (D02, D18).
    user_id UUID UNIQUE NOT NULL,
    vehicle_type VARCHAR(100) NOT NULL,
    vehicle_number VARCHAR(100) UNIQUE NOT NULL,
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    -- The order this rider is currently carrying, in sfo_order_core. Two things
    -- depend on it: a retried dispatch activity finds the order already held and
    -- returns the same rider instead of claiming a second one, and pickup and
    -- delivery are authorised from this row rather than by asking the Order
    -- Service who was assigned (D16 — one place decides).
    current_order_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- At most one rider per order, enforced by the engine rather than by a check in
-- Python. This is what makes a retried dispatch activity unable to strand a rider:
-- claiming a second one for an order that already has one is refused here, the
-- same argument D24 made for putting the tracking trail where its foreign key
-- could be enforced.
CREATE UNIQUE INDEX IF NOT EXISTS idx_riders_current_order
    ON riders (current_order_id)
    WHERE current_order_id IS NOT NULL;
