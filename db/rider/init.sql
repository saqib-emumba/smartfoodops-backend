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
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Great-circle distance in kilometres between two coordinate pairs.
--
-- This is the platform's first database function, and it exists because distance
-- is needed inside the dispatch query's ORDER BY. Computing it in Python instead
-- would mean reading every available rider into the service to sort them, which
-- is what makes the row-level lock in that query impossible to express.
--
-- Plain `LANGUAGE sql` rather than PL/pgSQL so the planner can inline it, and
-- IMMUTABLE so a functional index on it stays available if the fleet ever
-- outgrows a sequential scan over the partial index below.
CREATE OR REPLACE FUNCTION haversine_km(
    lat1 DOUBLE PRECISION, lon1 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION, lon2 DOUBLE PRECISION
) RETURNS DOUBLE PRECISION AS $$
    SELECT 6371.0 * 2 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2))
        * power(sin(radians(lon2 - lon1) / 2), 2)
    ));
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

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
    current_latitude DECIMAL(9, 6),
    current_longitude DECIMAL(9, 6),
    -- The order this rider is currently carrying, in sfo_order_core. Two things
    -- depend on it: a retried dispatch activity finds the order already held and
    -- returns the same rider instead of claiming a second one, and pickup and
    -- delivery are authorised from this row rather than by asking the Order
    -- Service who was assigned (D16 — one place decides).
    current_order_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Only the rows dispatch can actually choose from. Partial, because a rider who
-- is busy or has never reported a location is never a candidate and does not
-- belong in the index at all — on a fleet where most riders are mid-delivery at
-- peak hour, that is most of the table.
CREATE INDEX IF NOT EXISTS idx_riders_dispatchable
    ON riders (is_available)
    WHERE is_available AND current_latitude IS NOT NULL AND current_longitude IS NOT NULL;

-- At most one rider per order, enforced by the engine rather than by a check in
-- Python. This is what makes a retried dispatch activity unable to strand a rider:
-- claiming a second one for an order that already has one is refused here, the
-- same argument D24 made for putting the tracking trail where its foreign key
-- could be enforced.
CREATE UNIQUE INDEX IF NOT EXISTS idx_riders_current_order
    ON riders (current_order_id)
    WHERE current_order_id IS NOT NULL;
