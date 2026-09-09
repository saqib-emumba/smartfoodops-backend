-- ============================================================================
-- Analytics Service database — sfo_analytics_core (container sfo-analytics-db, host port 5438)
--
-- Owns nothing about orders, payments, restaurants or riders — only the two tables a
-- Kafka consumer needs to be correct: which events it has already applied, and the
-- business-facing projection it derived from them. Everything here is *derived* state,
-- rebuildable from the topic (Week 3, D38-D43): if this database were dropped, resetting
-- the consumer group's offset to the earliest available and replaying would reconstruct
-- both tables exactly, because Kafka — not this database — is the source of truth for
-- what happened.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Consumer-side dedup (Week 3, D40). Kafka delivery here is at-least-once — the relay
-- republishes on a crash between broker-ack and marking a row published (D39), and a
-- consumer restart can also redeliver an uncommitted batch — so "have I already applied
-- this fact" has to be a real check, not an assumption. `event_id` is the outbox row's own
-- id (see order_outbox/payment_outbox), stable across every republish of the same event,
-- so it is what a projection update conditions on.
--
-- Keyed by (consumer_group, event_id) rather than event_id alone: this service runs one
-- logical consumer today, but a table shaped for exactly one group would need a schema
-- change the day a second one (a future GenAI read-model, say) starts reading the same
-- topic independently.
CREATE TABLE IF NOT EXISTS processed_events (
    consumer_group VARCHAR(64) NOT NULL,
    event_id UUID NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (consumer_group, event_id)
);

-- Business projection: one row per order, updated incrementally as its events arrive.
-- This is what the Prometheus Counters/Gauges in analytics/consumer.py are seeded from on
-- startup — a Counter's in-process value does not survive a restart on its own, so the
-- durable total this table can be queried for is what makes the metric accurate again
-- after one (see consumer.py's own comment on `Counter._value.set(...)`).
--
-- `delivery_seconds` is computed from the envelope's own `occurred_at` timestamps
-- (order.created's vs order.delivered's), never from Kafka's transport timestamp, which
-- is when the relay happened to publish, not when the kitchen actually finished — the
-- blueprint drafted for this service made exactly that mistake.
CREATE TABLE IF NOT EXISTS order_projections (
    order_id UUID PRIMARY KEY,
    restaurant_id UUID,
    customer_id UUID,
    total_amount DECIMAL(10, 2),
    status VARCHAR(32) NOT NULL,
    placed_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    delivery_seconds DOUBLE PRECISION,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_order_projections_status ON order_projections(status);
