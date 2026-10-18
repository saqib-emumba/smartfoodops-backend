-- ============================================================================
-- One-off migration for an already-running sfo-restaurant-db container (D68).
--
-- Adds `restaurants.prep_time_minutes`: how long a kitchen takes to prepare an order, one figure per
-- restaurant, set by its owner. The AI assistant's delivery-time estimate is this plus the courier's trip.
-- init.sql only runs on an empty volume, so an existing deployment runs this by hand, once:
--   docker exec -i sfo-restaurant-db psql -U sfo_restaurant_admin -d sfo_restaurant_core < db/restaurant/add_prep_time.sql
--
-- Mirrors the same column in db/restaurant/init.sql. Idempotent. Existing restaurants get the 15-minute default
-- until their owner sets one (PATCH /api/v1/restaurants/{id}).
-- ============================================================================

ALTER TABLE restaurants
    ADD COLUMN IF NOT EXISTS prep_time_minutes INT NOT NULL DEFAULT 15 CHECK (prep_time_minutes BETWEEN 1 AND 120);
