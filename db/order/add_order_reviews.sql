-- ============================================================================
-- Week 5 (D64): customer reviews of delivered orders.
--
-- Hand-run, once, against an existing sfo_order_core volume — a fresh volume gets the table
-- from db/order/init.sql. Idempotent, one transaction, safe to re-run (same convention as
-- db/menu/add_dietary_tags.sql).
--
--   docker exec -i sfo-order-db psql -U sfo_order_admin -d sfo_order_core < db/order/add_order_reviews.sql
--
-- Deploy order: run this BEFORE starting the new order-service build, which reads the table.
-- ============================================================================

BEGIN;

-- Mirrors the `order_reviews` section of db/order/init.sql exactly.
CREATE TABLE IF NOT EXISTS order_reviews (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL,   -- users.id       (User Service database)
    restaurant_id UUID NOT NULL, -- restaurants.id (Restaurant Service database)
    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT CHECK (char_length(comment) <= 1000),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_order_reviews_restaurant
    ON order_reviews (restaurant_id, created_at DESC);

COMMIT;
