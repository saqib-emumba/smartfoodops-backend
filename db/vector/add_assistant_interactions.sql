-- ============================================================================
-- Week 5 (D63): the assistant's usage log and conversion attribution.
--
-- Hand-run, once, against an existing sfo_vector_core volume — a fresh volume gets the table from
-- db/vector/init.sql. Idempotent, one transaction, safe to re-run.
--
--   docker exec -i sfo-vector-db psql -U sfo_vector_admin -d sfo_vector_core < db/vector/add_assistant_interactions.sql
--
-- Deploy order: run BEFORE starting the new ai-service build, which writes the table.
-- ============================================================================

BEGIN;

-- Assistant usage log and conversion attribution (Week 5, D63) — the one table here that is not
-- derived from a menu. One row per assistant answer: who asked (an id, never the question or the
-- answer), what the router decided, how it ended, and which restaurants and dishes were named.
-- `converted_*` are filled in later by the attribution consumer when the same customer places an
-- order at one of those restaurants within the attribution window (`ATTRIBUTION_WINDOW_HOURS`).
-- It is a measurement, not a source of truth: losing it loses telemetry, never an order or a menu.
CREATE TABLE IF NOT EXISTS assistant_interactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL,
    conversation_id VARCHAR(64) NOT NULL,
    intent VARCHAR(32) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    recommended_restaurant_ids UUID[] NOT NULL DEFAULT '{}',
    recommended_item_ids TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    converted_order_id UUID UNIQUE,
    converted_at TIMESTAMPTZ
);

-- The attribution lookup: this customer's recent, still-unconverted recommendations.
CREATE INDEX IF NOT EXISTS idx_assistant_interactions_customer
    ON assistant_interactions (customer_id, created_at DESC)
    WHERE converted_order_id IS NULL;

COMMIT;
