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

-- The kitchen's answer, which is a fact about this order and therefore lives on it.
-- Deliberately NOT a member of order_status: acceptance does not move the order
-- along its lifecycle (an accepted order is still `confirmed` until a rider is
-- found), and adding a value would perturb the declaration order that the
-- compare-and-set in OrderRepository.transition depends on (D31).
CREATE TYPE kitchen_decision AS ENUM ('accepted', 'rejected');

-- What the rider has reported so far, as a durable column rather than only a signal
-- (Week 3, D46) — the same fix D32 already gave the kitchen's answer, applied to the
-- other place a lost signal used to read as silence. Declared in lifecycle order for the
-- same reason order_status is: OrderRepository.record_rider_report's guard compares with
-- `>`, so 'delivered' > 'picked_up' has to be true by declaration, not by convention.
CREATE TYPE rider_report_stage AS ENUM ('picked_up', 'delivered');

-- 1. Orders Table (Primary Registry, Idempotency Guard; Line Items Live Beside It — D50)
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL,   -- users.id       (User Service database)
    restaurant_id UUID NOT NULL, -- restaurants.id (Restaurant Service database)
    rider_id UUID,               -- riders.id      (User Service database)
    total_amount DECIMAL(10, 2) NOT NULL,
    status order_status NOT NULL DEFAULT 'created',
    -- NULL means the kitchen has not answered yet. Together with status =
    -- 'confirmed' that is the definition of "on the rail", which is what the
    -- capacity check counts.
    kitchen_decision kitchen_decision,
    kitchen_decided_at TIMESTAMP WITH TIME ZONE,
    -- What the rider has told the Order Service so far, independent of `status`: a report
    -- can arrive and this column can be set even if the *signal* carrying it to the saga
    -- is lost. The saga reads this back on a pickup/delivery timeout, exactly like it
    -- already does for `kitchen_decision` (D46).
    rider_reported_stage rider_report_stage,
    rider_reported_at TIMESTAMP WITH TIME ZONE,
    idempotency_key VARCHAR(255) UNIQUE, -- Protects order creation writes against API duplicate submissions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Order-history reads filter by customer, which no longer benefits from a foreign key.
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);

-- 1a. Order Line Items (Priced Snapshot Of What Was Ordered, Written Once At Checkout — D50)
-- Normalized out of orders.items (previously a single JSONB column) for database-enforced
-- integrity and per-item queryability. Write-once: nothing ever UPDATEs these rows after
-- checkout, same guarantee the JSONB column offered. `menu_item_id` is a plain reference
-- into the Menu Service's database (cross-db, no engine FK, same convention as
-- restaurant_id above) and deliberately NOT resolved against the live menu item — this row
-- must survive that item being edited or deleted later, which is the point of a snapshot.
CREATE TABLE IF NOT EXISTS order_line_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_no SMALLINT NOT NULL, -- preserves the original submission order
    menu_item_id VARCHAR(255) NOT NULL,
    item_name VARCHAR(255), -- snapshot at checkout; immune to later menu edits
    quantity INT NOT NULL CHECK (quantity > 0),
    unit_price DECIMAL(10, 2) NOT NULL,
    line_total DECIMAL(10, 2) NOT NULL,
    customizations JSONB, -- raw customer selection echo; a passthrough, not an entity tree, so left as-is
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_order_line_items_order ON order_line_items(order_id);

-- 1b. Order Line Item Options (The Priced, Resolved Customizations Chosen For A Line Item)
CREATE TABLE IF NOT EXISTS order_line_item_options (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    line_item_id UUID NOT NULL REFERENCES order_line_items(id) ON DELETE CASCADE,
    group_key VARCHAR(255), -- the customization group this option was chosen from
    name VARCHAR(255) NOT NULL,
    extra_price DECIMAL(10, 2) NOT NULL DEFAULT 0.0,
    position SMALLINT NOT NULL DEFAULT 0 -- preserves the original selection order
);

CREATE INDEX IF NOT EXISTS idx_order_line_item_options_line_item ON order_line_item_options(line_item_id);

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

-- Serves the two reads the kitchen queue needs, which are the same shape: an admin
-- listing "my orders awaiting a decision", and the capacity count gating entry into
-- 'confirmed'. Partial, because every other order in the table is irrelevant to both —
-- and on a busy platform that is nearly all of them.
CREATE INDEX IF NOT EXISTS idx_orders_kitchen_queue
    ON orders (restaurant_id, created_at)
    WHERE status = 'confirmed' AND kitchen_decision IS NULL;

-- ============================================================================
-- Transactional outbox (Week 3, D39) — the relay's queue, not a second audit trail.
--
-- order_tracking_logs already records every transition for a human reading the timeline;
-- this table exists only so a background relay (services/common/outbox.py) can publish
-- the same facts to Kafka without a second write after the commit. Written inside the
-- exact same `cursor(commit=True)` blocks that already write orders and
-- order_tracking_logs — see OrderRepository.create/transition/decide_kitchen — so an event
-- can never exist for a write that didn't happen, and vice versa (the D09/D24 argument,
-- applied again).
-- ============================================================================
CREATE TABLE IF NOT EXISTS order_outbox (
    -- Doubles as the event's dedup key on the consumer side (Kafka delivery here is
    -- at-least-once: a relay crash between the broker ack and marking a row published
    -- republishes it).
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Claim order for the relay. NOT a resumable "last seq processed" cursor: BIGSERIAL
    -- hands out values before commit, so a lower seq can commit after a higher one and be
    -- skipped forever if the relay tracked a high-water mark instead of querying
    -- published_at IS NULL directly.
    seq BIGSERIAL NOT NULL,
    aggregate_type VARCHAR(32) NOT NULL DEFAULT 'order',
    aggregate_id UUID NOT NULL, -- == the Kafka partition key, so per-order ordering holds
    event_type VARCHAR(64) NOT NULL,
    event_version SMALLINT NOT NULL DEFAULT 1,
    payload JSONB NOT NULL,
    -- W3C trace context, captured from the request's own span at the moment this row is
    -- inserted — not by the relay, which runs minutes later and would otherwise start an
    -- orphan trace disconnected from the request that caused the write.
    traceparent TEXT,
    tracestate TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ, -- NULL == unpublished; the relay's only WHERE clause
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT
);

-- The relay's only query. Stays the size of the backlog rather than the size of history,
-- because nearly every row ends up published.
CREATE INDEX IF NOT EXISTS idx_order_outbox_unpublished
    ON order_outbox (seq) WHERE published_at IS NULL;

-- Lets a future admin/debug read ask "what has this order emitted so far?" without a scan.
CREATE INDEX IF NOT EXISTS idx_order_outbox_aggregate
    ON order_outbox (aggregate_id, seq);
