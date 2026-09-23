#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "🚀 Bootstrapping SmartFoodOps Local Environment..."

# 1. Create the modular directory structure
echo "📂 Creating services, gateway and per-service database directories..."
mkdir -p smartfoodops-backend/{api-gateway,db/{user,restaurant,order,payment,menu,rider,analytics},services/{common,user,restaurant,menu,order,payment,rider,analytics,notification}}
cd smartfoodops-backend

# 2. Write out the environment variables configuration
# Database passwords and the JWT signing material live here: docker-compose.yml builds each
# service's DSN from them, so a password is written in exactly one place. Database and role
# names are not secrets and stay literal in docker-compose.yml.
echo "📝 Generating .env configuration file..."
cat << 'EOF' > .env
# Database Credentials — one password per physical database (database-per-service)
USER_POSTGRES_PASSWORD=sfo_user_password_123
RESTAURANT_POSTGRES_PASSWORD=sfo_restaurant_password_123
ORDER_POSTGRES_PASSWORD=sfo_order_password_123
PAYMENT_POSTGRES_PASSWORD=sfo_payment_password_123
MENU_POSTGRES_PASSWORD=sfo_menu_password_123
RIDER_POSTGRES_PASSWORD=sfo_rider_password_123
ANALYTICS_POSTGRES_PASSWORD=sfo_analytics_password_123

# Redis. docker-compose.yml sets the per-service URL literally (database 0 for the Menu
# Service's cache, database 1 for the User Service's sessions), so this is only read by
# tooling run outside Compose.
REDIS_URL=redis://cache-redis:6379/0

# Services Endpoints (Within Docker Network)
USER_SERVICE_URL=http://user-service:8001
RESTAURANT_SERVICE_URL=http://restaurant-service:8002
MENU_SERVICE_URL=http://menu-service:8003
ORDER_SERVICE_URL=http://order-service:8004
PAYMENT_SERVICE_URL=http://payment-service:8005
RIDER_SERVICE_URL=http://rider-service:8006

# Temporal dev server. docker-compose.yml sets this per-container as well; it is here so
# scripts and a worker run outside Compose read the same address.
TEMPORAL_ADDRESS=temporal-server:7233
EOF

# 2b. Generate the token signing material.
# Appended rather than written above because the heredoc there is quoted and cannot expand a
# command. A fresh keypair per environment is the point: the private key is the ability to
# mint any identity, so it is never a checked-in constant.
echo "🔑 Generating RS256 signing keypair and internal service key..."
jwt_private_pem=$(openssl genrsa 2048 2>/dev/null)
jwt_public_pem=$(printf '%s' "$jwt_private_pem" | openssl rsa -pubout 2>/dev/null)

# `base64 -A` keeps each key on one line, which is the only shape .env accepts.
cat << EOF >> .env

# Access token signing (RS256). docker-compose.yml hands the private key to the User Service
# alone — it is the sole issuer — and the public key to everyone, who can then verify a token
# but never forge one.
JWT_PRIVATE_KEY_B64=$(printf '%s' "$jwt_private_pem" | openssl base64 -A)
JWT_PUBLIC_KEY_B64=$(printf '%s' "$jwt_public_pem" | openssl base64 -A)

# Shared secret for the service-to-service endpoints no end user may call directly, such as
# the Order Service's audit log write.
INTERNAL_API_KEY=$(openssl rand -hex 32)
EOF

# 2c. Observability admin credential (Week 3, D42). Same reasoning as INTERNAL_API_KEY above:
# a random value only openssl generates, appended because the quoted heredoc above cannot.
cat << EOF >> .env

# --- Observability (Week 3) ---
GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)
EOF

# 2d. RabbitMQ credentials (Week 3, D45). Same reasoning again — Celery's broker password
# is never a hardcoded default (common/config.py's own rule, D19); the only default this
# platform ever uses for a real secret is "generate a fresh one here."
cat << EOF >> .env

# --- Notifications (Week 3) ---
RABBITMQ_USER=sfo_rabbitmq_admin
RABBITMQ_PASSWORD=$(openssl rand -hex 16)
EOF

# 3. Write out one initialization migration script per physical database.
# Each is mounted into its own container, so a service's schema exists only in the database
# that service holds credentials for.
echo "🐘 Generating Postgres schemas (db/{user,restaurant,order,payment,menu}/init.sql)..."
cat << 'EOF' > db/user/init.sql
-- ============================================================================
-- User Service database — sfo_user_core (container sfo-user-db, host port 5432)
--
-- Owns identity and nothing else: `roles` and `users`. Only the User Service
-- connects here; every other service reads a profile through
-- GET /api/v1/users/{user_id}.
--
-- `riders` used to live here too, on the argument that a rider is an extension of
-- a user identity and the foreign key to `users` was worth keeping. Week 2 moved it
-- to sfo_rider_core (D28): the Rider Service needs to write availability and
-- location on every dispatch, and under D01 a service may not write another
-- service's tables. The foreign key was the cost of that move — `riders.user_id`
-- is now a plain UUID verified over HTTP, like every other cross-service
-- reference (D02).
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1a. Roles Lookup Table (Normalized Database Design)
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Seed static user roles on initialization
INSERT INTO roles (name, description) VALUES
('customer', 'App Customer / Order placer'),
('restaurant_admin', 'Restaurant Owner / Menu and Order manager'),
('rider', 'Delivery Partner / Logistics handler'),
('system_admin', 'SFO Platform Operations administrator')
ON CONFLICT (name) DO NOTHING;

-- 1b. Users Table (Core Profiles)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    phone VARCHAR(50) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Case-insensitive unique constraint index for emails (prevent duplicate registrations)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));

-- 1c. User <-> Role grants (many-to-many). Replaces the single users.role_id column: a
-- user can hold more than one role (e.g. a restaurant_admin who also places orders as a
-- customer). Composite PK is the grant's whole identity -- "this user holds this role" has
-- no attributes worth a surrogate id, and it doubles as the no-duplicate-grant constraint.
-- ON DELETE CASCADE on user_id (deleting a user drops their grants); ON DELETE RESTRICT on
-- role_id, matching the old column's behavior (a role is reference data, not disposable).
CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, role_id)
);
EOF

cat << 'EOF' > db/restaurant/init.sql
-- ============================================================================
-- Restaurant Service database — sfo_restaurant_core
-- (container sfo-restaurant-db, host port 5433)
--
-- Owns `restaurants`, and nothing else. Only the Restaurant Service connects
-- here; every other service reads a restaurant through
-- GET /api/v1/restaurants/{restaurant_id}.
--
-- An `order_tickets` kitchen queue lived here through the first cut of Week 2.
-- It held a status, an items snapshot and a decision timestamp — none of which
-- was restaurant-domain data that `orders` did not already have — so the whole
-- table was a cross-database hop for facts about an order's lifecycle. D32 moved
-- the kitchen's decision onto `orders.kitchen_decision`, which took the saga's
-- dependency on this service to zero.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Restaurants Table
CREATE TABLE IF NOT EXISTS restaurants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- users.id, which lives in the User Service database. There is no foreign key to
    -- enforce across databases, so the owner is verified over HTTP before onboarding.
    owner_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    address TEXT NOT NULL,
    latitude DECIMAL(9, 6) NOT NULL,
    longitude DECIMAL(9, 6) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    capacity INT NOT NULL DEFAULT 50,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for restaurants geo queries
CREATE INDEX IF NOT EXISTS idx_restaurants_geo ON restaurants(latitude, longitude);

-- Owner lookups ("list my restaurants") scan by owner, so index the reference.
CREATE INDEX IF NOT EXISTS idx_restaurants_owner ON restaurants(owner_id);
EOF

cat << 'EOF' > db/order/init.sql
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

-- 1. Orders Table (Primary Registry with JSONB Items and Idempotency Guard)
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL,   -- users.id       (User Service database)
    restaurant_id UUID NOT NULL, -- restaurants.id (Restaurant Service database)
    rider_id UUID,               -- riders.id      (User Service database)
    items JSONB NOT NULL, -- Stores snapshot of ordered items, prices, and selected customization options at checkout
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
EOF

cat << 'EOF' > db/payment/init.sql
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

-- ============================================================================
-- Transactional outbox (Week 3, D39) — same shape and purpose as order_outbox in
-- sfo_order_core; see that table's comment for the full reasoning. `aggregate_id` here is
-- deliberately `order_id`, not `payment_id`: every consumer of this stream joins on the
-- order, and the Kafka partition key has to match what order_outbox uses so a payment
-- event and an order event for the same order land in the same partition.
-- ============================================================================
CREATE TABLE IF NOT EXISTS payment_outbox (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    seq BIGSERIAL NOT NULL,
    aggregate_type VARCHAR(32) NOT NULL DEFAULT 'payment',
    aggregate_id UUID NOT NULL, -- orders.id (Order Service database) — the partition key
    event_type VARCHAR(64) NOT NULL,
    event_version SMALLINT NOT NULL DEFAULT 1,
    payload JSONB NOT NULL,
    traceparent TEXT,
    tracestate TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_payment_outbox_unpublished
    ON payment_outbox (seq) WHERE published_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_payment_outbox_aggregate
    ON payment_outbox (aggregate_id, seq);
EOF

cat << 'EOF' > db/menu/init.sql
-- ============================================================================
-- Menu Service database — sfo_menu_core (container sfo-menu-db, host port 5436)
--
-- Owns `menus`. Only the Menu Service connects here; every other service reads a menu
-- through GET /api/v1/menus/{restaurant_id}.
--
-- This table replaced the MongoDB `menus` collection. The document shape survived the
-- move intact inside a single JSONB column: a menu is read and written whole, by
-- restaurant, so splitting the category/item/option tree into three relational tables
-- would buy joins nobody performs and cost a transaction on every publish.
--
-- `restaurant_id` is a plain UUID pointing into the Restaurant Service's database, where
-- no foreign key can follow it, so the restaurant is verified over HTTP before the upsert
-- (see services/menu/clients.py) instead of by the engine.
-- ============================================================================

-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Menus Table (One Row Per Restaurant, Whole Category Tree In JSONB)
CREATE TABLE IF NOT EXISTS menus (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- UNIQUE is what makes "publish a menu" an upsert rather than an append: one live
    -- menu per restaurant, enforced by the engine instead of by the application.
    restaurant_id UUID UNIQUE NOT NULL, -- restaurants.id (Restaurant Service database)
    categories JSONB NOT NULL DEFAULT '[]'::jsonb, -- Nested categories -> items -> customization groups -> options
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- No index is declared here on purpose. Every read is `WHERE restaurant_id = ...`, which
-- the UNIQUE constraint above already backs with a btree index; a second one would be
-- dead weight. A GIN index over `categories` would only pay for itself once something
-- searches *inside* the tree (e.g. "which restaurants serve a vegan main?"), and until
-- then it is a write cost on every publish for a query nobody issues.
EOF

cat << 'EOF' > db/rider/init.sql
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
EOF

cat << 'EOF' > db/analytics/init.sql
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
EOF

# 4. Write out the Docker Compose orchestration configuration
echo "🐳 Generating docker-compose.yml..."
cat << 'EOF' > docker-compose.yml
networks:
  smartfoodops-network:
    driver: bridge

volumes:
  user_postgres_data:
  restaurant_postgres_data:
  order_postgres_data:
  payment_postgres_data:
  menu_postgres_data:
  rider_postgres_data:
  redis_data:
  temporal_data:
  prometheus_data:
  grafana_data:
  kafka_data:
  analytics_postgres_data:
  rabbitmq_data:

# Database-per-service: each Postgres-backed service gets its own physical database, with
# its own credentials, so no service can reach another's tables even by accident.
#
# Each DSN is defined once here and injected as that service's DATABASE_URL, so a password
# is never repeated in this file. Passwords come from the gitignored root .env, which
# Compose reads automatically; `:?` aborts every compose command with the message below
# when one is missing, rather than starting the stack with an empty password.
# Database and role names are not secrets, so they stay literal and readable.
x-user-db-env: &user-db-env
  DATABASE_URL: postgresql://sfo_user_admin:${USER_POSTGRES_PASSWORD:?set USER_POSTGRES_PASSWORD in the root .env}@db-user-postgres:5432/sfo_user_core

x-restaurant-db-env: &restaurant-db-env
  DATABASE_URL: postgresql://sfo_restaurant_admin:${RESTAURANT_POSTGRES_PASSWORD:?set RESTAURANT_POSTGRES_PASSWORD in the root .env}@db-restaurant-postgres:5432/sfo_restaurant_core

x-order-db-env: &order-db-env
  DATABASE_URL: postgresql://sfo_order_admin:${ORDER_POSTGRES_PASSWORD:?set ORDER_POSTGRES_PASSWORD in the root .env}@db-order-postgres:5432/sfo_order_core

# Payments are physically isolated so that card handling can be locked down on its own:
# this password unlocks nothing but the `payments` table.
x-payment-db-env: &payment-db-env
  DATABASE_URL: postgresql://sfo_payment_admin:${PAYMENT_POSTGRES_PASSWORD:?set PAYMENT_POSTGRES_PASSWORD in the root .env}@db-payment-postgres:5432/sfo_payment_core

# The Menu Service replaced MongoDB with a Postgres database of its own, so it now holds a
# credential like everybody else — the one service that used to be exempt.
x-menu-db-env: &menu-db-env
  DATABASE_URL: postgresql://sfo_menu_admin:${MENU_POSTGRES_PASSWORD:?set MENU_POSTGRES_PASSWORD in the root .env}@db-menu-postgres:5432/sfo_menu_core

# The `riders` table moved out of sfo_user_core in Week 2: dispatch writes availability and
# location on every assignment, and a service may not write another service's tables.
x-rider-db-env: &rider-db-env
  DATABASE_URL: postgresql://sfo_rider_admin:${RIDER_POSTGRES_PASSWORD:?set RIDER_POSTGRES_PASSWORD in the root .env}@db-rider-postgres:5432/sfo_rider_core

# Owns only what a Kafka consumer needs to be correct: dedup and a derived projection
# (Week 3, D40). No other service ever reads or writes this database — database-per-service
# holds even for a service whose "facts" are all rebuildable from a topic.
x-analytics-db-env: &analytics-db-env
  DATABASE_URL: postgresql://sfo_analytics_admin:${ANALYTICS_POSTGRES_PASSWORD:?set ANALYTICS_POSTGRES_PASSWORD in the root .env}@db-analytics-postgres:5432/sfo_analytics_core

# Every service verifies access tokens, so every service gets the public key. Only the User
# Service gets the private key, further down: a service that cannot sign cannot mint an
# identity, which is the whole reason the signing is asymmetric. The internal key is shared
# because it authenticates service-to-service calls in both directions.
x-jwt-env: &jwt-env
  JWT_PUBLIC_KEY_B64: ${JWT_PUBLIC_KEY_B64:?set JWT_PUBLIC_KEY_B64 in the root .env - scripts/init_bootstrap.sh generates a keypair}
  INTERNAL_API_KEY: ${INTERNAL_API_KEY:?set INTERNAL_API_KEY in the root .env}

# The five Postgres containers differ only in credentials, published port, volume and
# schema. Shared settings live here; the healthcheck reads the container's own POSTGRES_*
# variables (`$$` defers expansion to the container) so it needs no per-database copy.
x-postgres-base: &postgres-base
  image: postgres:15-alpine
  restart: always
  networks:
    - smartfoodops-network
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
    interval: 5s
    timeout: 5s
    retries: 5

services:
  # --- 1. DATABASES & CACHING (ONE PER SERVICE) ---
  # Each database runs on 5432 inside the network and publishes a distinct host port, so a
  # local SQL client can reach all five at once.
  db-user-postgres:
    <<: *postgres-base
    container_name: sfo-user-db
    environment:
      POSTGRES_DB: sfo_user_core
      POSTGRES_USER: sfo_user_admin
      POSTGRES_PASSWORD: ${USER_POSTGRES_PASSWORD}
    ports:
      - "5432:5432"
    volumes:
      - user_postgres_data:/var/lib/postgresql/data
      - ./db/user/init.sql:/docker-entrypoint-initdb.d/init.sql:ro # Auto-runs DDL on first boot

  db-restaurant-postgres:
    <<: *postgres-base
    container_name: sfo-restaurant-db
    environment:
      POSTGRES_DB: sfo_restaurant_core
      POSTGRES_USER: sfo_restaurant_admin
      POSTGRES_PASSWORD: ${RESTAURANT_POSTGRES_PASSWORD}
    ports:
      - "5433:5432" # Maps host 5433 to container 5432
    volumes:
      - restaurant_postgres_data:/var/lib/postgresql/data
      - ./db/restaurant/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  db-order-postgres:
    <<: *postgres-base
    container_name: sfo-order-db
    environment:
      POSTGRES_DB: sfo_order_core
      POSTGRES_USER: sfo_order_admin
      POSTGRES_PASSWORD: ${ORDER_POSTGRES_PASSWORD}
    ports:
      - "5434:5432" # Maps host 5434 to container 5432
    volumes:
      - order_postgres_data:/var/lib/postgresql/data
      - ./db/order/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  db-payment-postgres:
    <<: *postgres-base
    container_name: sfo-payment-db
    environment:
      POSTGRES_DB: sfo_payment_core
      POSTGRES_USER: sfo_payment_admin
      POSTGRES_PASSWORD: ${PAYMENT_POSTGRES_PASSWORD}
    ports:
      - "5435:5432" # Maps host 5435 to container 5432
    volumes:
      - payment_postgres_data:/var/lib/postgresql/data
      - ./db/payment/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  # Replaced sfo-mongodb: the `menus` collection became a `menus` table here, and
  # `order_tracking_logs` went to the Order Service's database instead.
  db-menu-postgres:
    <<: *postgres-base
    container_name: sfo-menu-db
    environment:
      POSTGRES_DB: sfo_menu_core
      POSTGRES_USER: sfo_menu_admin
      POSTGRES_PASSWORD: ${MENU_POSTGRES_PASSWORD}
    ports:
      - "5436:5432" # Maps host 5436 to container 5432
    volumes:
      - menu_postgres_data:/var/lib/postgresql/data
      - ./db/menu/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  # The delivery fleet. Its `riders` table shipped inside sfo_user_core in Week 1 with no
  # code behind it; Week 2 gave it a service that writes to it, and therefore a database.
  db-rider-postgres:
    <<: *postgres-base
    container_name: sfo-rider-db
    environment:
      POSTGRES_DB: sfo_rider_core
      POSTGRES_USER: sfo_rider_admin
      POSTGRES_PASSWORD: ${RIDER_POSTGRES_PASSWORD}
    ports:
      - "5437:5432" # Maps host 5437 to container 5432
    volumes:
      - rider_postgres_data:/var/lib/postgresql/data
      - ./db/rider/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  # Week 3 (D40): the Analytics Service's own database — a dedup table and a projection
  # derived entirely from Kafka, never a fact any other service's request path depends on.
  db-analytics-postgres:
    <<: *postgres-base
    container_name: sfo-analytics-db
    environment:
      POSTGRES_DB: sfo_analytics_core
      POSTGRES_USER: sfo_analytics_admin
      POSTGRES_PASSWORD: ${ANALYTICS_POSTGRES_PASSWORD}
    ports:
      - "5438:5432" # Maps host 5438 to container 5432
    volumes:
      - analytics_postgres_data:/var/lib/postgresql/data
      - ./db/analytics/init.sql:/docker-entrypoint-initdb.d/init.sql:ro

  cache-redis:
    image: redis:7.0-alpine
    container_name: sfo-redis
    restart: always
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  # --- 1b. WORKFLOW ORCHESTRATOR ---
  # The all-in-one development server: gRPC API, Web UI and SQLite persistence in one
  # container. `temporalio/temporal` is the CLI image and its entrypoint is the `temporal`
  # binary, so the server is configured by flags below — not by environment variables.
  #
  # Three details are load-bearing and were each confirmed by running the container:
  #
  #   --ip defaults to "localhost", which is unreachable from sibling containers, so
  #   0.0.0.0 is mandatory. --ui-ip defaults to --ip, so one flag covers both.
  #
  #   --db-filename lives under /home/temporal, not /var/lib/temporal. The image runs as
  #   non-root uid 1000, and a named volume mounted where the image has no directory is
  #   created root-owned, which fails with SQLite's CANTOPEN. /home/temporal is owned by
  #   uid 1000 in the image, so the fresh volume inherits that owner.
  #
  #   --metrics-port is pinned rather than left to a random free port, so Week 3's
  #   Prometheus has a stable scrape target.
  temporal-server:
    image: temporalio/temporal:1.8.2
    container_name: sfo-temporal-server
    restart: always
    command:
      - server
      - start-dev
      - --ip
      - 0.0.0.0
      - --port
      - "7233"
      - --ui-port
      - "8233"
      - --metrics-port
      - "9233"
      - --db-filename
      - /home/temporal/temporal.db
    ports:
      - "7233:7233" # gRPC workflow API, used by the orchestrator service and its worker
      - "8233:8233" # Web UI. Deliberately not behind the gateway: it has no auth.
      - "9233:9233" # Prometheus metrics (Week 3)
    volumes:
      - temporal_data:/home/temporal
    networks:
      - smartfoodops-network
    healthcheck:
      # `nc` is not in this image, but the `temporal` CLI is — it is the entrypoint.
      test: ["CMD", "temporal", "operator", "namespace", "list", "--address", "127.0.0.1:7233"]
      interval: 5s
      timeout: 5s
      retries: 10

  # --- 1c. OBSERVABILITY (Week 3) ---
  # Distributed tracing, metrics and dashboards over the stack above. Every container here
  # is reached only by other containers on smartfoodops-network or by a developer's browser
  # — none of it sits behind the gateway, the same posture nginx.conf already documents for
  # the Temporal Web UI: "no auth, so it stays off the gateway".

  # OTLP receiver + trace storage + UI in one container. jaeger:2.x is itself an
  # OpenTelemetry Collector distribution, so the OTLP ports below are native, not a
  # compatibility shim — no separate Collector container is needed for this platform's
  # size (D38-D43 discussion in key-decisions.md).
  jaeger:
    image: jaegertracing/jaeger:2.1.0
    container_name: sfo-jaeger
    restart: always
    ports:
      - "16686:16686" # Web UI
      - "4317:4317"   # OTLP gRPC receiver
      - "4318:4318"   # OTLP HTTP receiver
    networks:
      - smartfoodops-network

  # Scrapes every service's /metrics over the network directly — no service publishes its
  # app port to the host, and none needs to: Compose's embedded DNS resolves e.g.
  # order-service:8004 for any container on smartfoodops-network, Prometheus included.
  prometheus:
    image: prom/prometheus:v2.53.0
    container_name: sfo-prometheus
    restart: always
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    networks:
      - smartfoodops-network

  # Dashboards and the Prometheus data source are provisioned from files under
  # ./grafana/provisioning, not clicked together, so `docker compose down -v` does not
  # discard them (D42).
  grafana:
    image: grafana/grafana:11.2.0
    container_name: sfo-grafana
    restart: always
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD in the root .env}
      GF_AUTH_ANONYMOUS_ENABLED: "false"
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
    depends_on:
      - prometheus
    networks:
      - smartfoodops-network

  # --- 1d. EVENTING (Week 3) ---
  # Kafka carries facts; Temporal still owns every decision (key-decisions.md D38). No
  # service here is on the order's critical path — checkout and the saga both work with
  # this whole section stopped, because every producer writes to its own outbox table
  # first (D39) and a background relay is what reaches Kafka, never the request itself.

  # Single-node KRaft broker — no Zookeeper. `KAFKA_LOG_DIRS` matches the mounted volume
  # exactly, unlike an early draft of this file that pointed it at the container's writable
  # layer instead: with that mismatch, `docker compose down`/`up` re-formats a fresh empty
  # log dir against the same hardcoded CLUSTER_ID and silently drops every topic, offset and
  # consumer-group position. `KAFKA_AUTO_CREATE_TOPICS_ENABLE: 'false'` is deliberate too —
  # topics are created explicitly by kafka-init below, with the partition count the platform
  # actually needs; auto-create would silently hand out 1 partition instead.
  kafka:
    image: confluentinc/cp-kafka:7.4.0
    container_name: sfo-kafka
    restart: always
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: 'CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT'
      KAFKA_ADVERTISED_LISTENERS: 'PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092'
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_PROCESS_ROLES: 'broker,controller'
      KAFKA_CONTROLLER_QUORUM_VOTERS: '1@kafka:29093'
      KAFKA_LISTENERS: 'PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:29093,PLAINTEXT_HOST://0.0.0.0:9092'
      KAFKA_INTER_BROKER_LISTENER_NAME: 'PLAINTEXT'
      KAFKA_CONTROLLER_LISTENER_NAMES: 'CONTROLLER'
      KAFKA_LOG_DIRS: '/var/lib/kafka/data'
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: 'false'
      CLUSTER_ID: 'MkU3OEVBNTcwNTJENDM2Qk'
    ports:
      - "9092:9092"
    volumes:
      - kafka_data:/var/lib/kafka/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "kafka-broker-api-versions", "--bootstrap-server", "localhost:9092"]
      interval: 5s
      timeout: 5s
      retries: 15

  # Registers the JSON Schema common/events/*.py generates for each event type, and is
  # consulted on the hot path (producer before send, consumer before deserialize) — not
  # just at startup — via a version-keyed cache in common/kafka.py. The wire format stays
  # plain JSON: registering a JSON Schema here does not require the Confluent Avro
  # serializers, only this REST API.
  schema-registry:
    image: confluentinc/cp-schema-registry:7.4.0
    container_name: sfo-schema-registry
    restart: always
    depends_on:
      kafka:
        condition: service_healthy
    ports:
      - "8081:8081"
    environment:
      SCHEMA_REGISTRY_HOST_NAME: schema-registry
      SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS: 'kafka:29092'
      SCHEMA_REGISTRY_LISTENERS: 'http://0.0.0.0:8081'
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/subjects"]
      interval: 5s
      timeout: 5s
      retries: 15

  # One-shot: creates the platform's one topic (and its DLQ) explicitly, at the partition
  # count per-order ordering depends on (3 — see common/events/topics.py), then exits.
  # `restart: "no"` — this is a migration step, not a long-running service.
  kafka-init:
    image: confluentinc/cp-kafka:7.4.0
    container_name: sfo-kafka-init
    restart: "no"
    depends_on:
      kafka:
        condition: service_healthy
    networks:
      - smartfoodops-network
    volumes:
      - ./kafka/init-topics.sh:/init-topics.sh:ro
    entrypoint: ["bash", "/init-topics.sh"]

  # Celery's broker for the Notification Service (Week 3, D45) — the curriculum's mandated
  # transport, used here purely as a concurrency pool for slow simulated SMS/email I/O.
  # Kafka is the durable ledger (see notification/__init__.py); RabbitMQ does not need to
  # be, so no `depends_on` from order/payment-service — nothing on the checkout path ever
  # touches this broker.
  rabbitmq:
    image: rabbitmq:3-management-alpine
    container_name: sfo-rabbitmq
    restart: always
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_USER:?set RABBITMQ_USER in the root .env}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASSWORD:?set RABBITMQ_PASSWORD in the root .env}
    ports:
      - "5672:5672"   # AMQP
      - "15672:15672" # Management UI
      - "15692:15692" # Prometheus metrics (rabbitmq_prometheus, enabled via enabled_plugins)
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq
      - ./rabbitmq/enabled_plugins:/etc/rabbitmq/enabled_plugins:ro
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 5s
      timeout: 5s
      retries: 15

  # --- 2. API GATEWAY (NGINX REVERSE PROXY) ---
  api-gateway:
    image: nginx:alpine
    container_name: sfo-api-gateway
    restart: always
    ports:
      - "80:80"
    volumes:
      - ./api-gateway/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - user-service
      - restaurant-service
      - menu-service
      - order-service
      - payment-service
      - rider-service
      - orchestrator-service
    networks:
      - smartfoodops-network

  # --- 3. CORE SERVICE CONTAINER SERVICES ---
  # Every service depends on its own database only, and on nothing else's.
  user-service:
    build:
      # Context is ./services so the shared `common` chassis is inside the build context.
      context: ./services
      dockerfile: user/Dockerfile
    container_name: sfo-user-service
    restart: always
    environment:
      <<: [*user-db-env, *jwt-env]
      # The one container holding the signing key. Nothing else can issue a token.
      JWT_PRIVATE_KEY_B64: ${JWT_PRIVATE_KEY_B64:?set JWT_PRIVATE_KEY_B64 in the root .env - scripts/init_bootstrap.sh generates a keypair}
      # Database 1, kept apart from the Menu Service's cache in database 0.
      AUTH_REDIS_URL: redis://cache-redis:6379/1
    depends_on:
      db-user-postgres:
        condition: service_healthy
      cache-redis:
        condition: service_healthy
    networks:
      - smartfoodops-network

  restaurant-service:
    build:
      context: ./services
      dockerfile: restaurant/Dockerfile
    container_name: sfo-restaurant-service
    restart: always
    environment:
      <<: [*restaurant-db-env, *jwt-env]
      USER_SERVICE_URL: http://user-service:8001
    depends_on:
      db-restaurant-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  menu-service:
    build:
      context: ./services
      dockerfile: menu/Dockerfile
    container_name: sfo-menu-service
    restart: always
    environment:
      <<: [*menu-db-env, *jwt-env]
      # Database 0. The cache is a copy of the table above, never the source of truth, so
      # this service starts and serves with Redis down — just without the shortcut.
      REDIS_URL: redis://cache-redis:6379/0
      RESTAURANT_SERVICE_URL: http://restaurant-service:8002
    depends_on:
      db-menu-postgres:
        condition: service_healthy
      cache-redis:
        condition: service_healthy
    networks:
      - smartfoodops-network

  order-service:
    build:
      context: ./services
      dockerfile: order/Dockerfile
    container_name: sfo-order-service
    restart: always
    environment:
      <<: [*order-db-env, *jwt-env]
      USER_SERVICE_URL: http://user-service:8001
      RESTAURANT_SERVICE_URL: http://restaurant-service:8002
      MENU_SERVICE_URL: http://menu-service:8003
      # This service starts the order saga and signals the kitchen's decision into it,
      # holding its own Temporal client again (D47). D36 had replaced that client with HTTP
      # calls into the Orchestrator Service, because naming a workflow by class reference
      # dragged the orchestrator's imports into this process; naming it by string does not.
      TEMPORAL_ADDRESS: temporal-server:7233
      # The outbox relay's target (Week 3, D39). Deliberately no `depends_on: kafka` below:
      # the relay's own reconnect loop is what handles Kafka not being ready yet, and a
      # hard startup dependency here would wrongly imply checkout needs Kafka to work.
      KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      SCHEMA_REGISTRY_URL: http://schema-registry:8081
    depends_on:
      db-order-postgres:
        condition: service_healthy
      temporal-server:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # Reads the order it is settling over HTTP; it holds no credentials for sfo_order_core,
  # and no other service holds credentials for sfo_payment_core.
  payment-service:
    build:
      context: ./services
      dockerfile: payment/Dockerfile
    container_name: sfo-payment-service
    restart: always
    environment:
      <<: [*payment-db-env, *jwt-env]
      ORDER_SERVICE_URL: http://order-service:8004
      # See order-service's own comment: no depends_on for kafka on purpose (Week 3, D39).
      KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      SCHEMA_REGISTRY_URL: http://schema-registry:8081
    depends_on:
      db-payment-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # Owns the delivery fleet. It reads the User Service to confirm an account really holds
  # the `rider` role, and the Restaurant Service for the coordinates dispatch measures
  # from; it reaches the Order Service to record a rider's pickup or delivery against the
  # order, then signals the saga itself (D47) — order state stays the Order Service's,
  # the event goes straight to Temporal from the service that observed it.
  rider-service:
    build:
      context: ./services
      dockerfile: rider/Dockerfile
    container_name: sfo-rider-service
    restart: always
    environment:
      <<: [*rider-db-env, *jwt-env]
      USER_SERVICE_URL: http://user-service:8001
      ORDER_SERVICE_URL: http://order-service:8004
      TEMPORAL_ADDRESS: temporal-server:7233
      # Database 2, apart from the Menu Service's cache (0) and the User Service's refresh
      # tokens (1): the fleet's live location index (D49).
      RIDER_REDIS_URL: redis://cache-redis:6379/2
    depends_on:
      db-rider-postgres:
        condition: service_healthy
      temporal-server:
        condition: service_healthy
      cache-redis:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # --- 3b. ANALYTICS (Week 3, D40) ---
  # A Kafka read-model, not a request-path service — no sibling calls, no JWT keys (the
  # first service in the platform that needs none: it never verifies a token, because
  # nothing it serves is user-facing). No `depends_on: kafka`, matching order-service's
  # and payment-service's own comment: the consumer's reconnect loop is what handles Kafka
  # not being ready yet.
  analytics-service:
    build:
      context: ./services
      dockerfile: analytics/Dockerfile
    container_name: sfo-analytics-service
    restart: always
    environment:
      <<: *analytics-db-env
      KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      SCHEMA_REGISTRY_URL: http://schema-registry:8081
    depends_on:
      db-analytics-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # --- 3c. NOTIFICATIONS (Week 3, D45) ---
  # Two containers, one image — see services/notification/__init__.py for why the split is
  # what makes the Celery hop real rather than decorative. Neither depends on kafka or
  # rabbitmq at the compose level: each has its own reconnect loop (the consumer) or is a
  # pure client of a broker it does not need to be up before packages import (the worker).
  notification-consumer:
    build:
      context: ./services
      dockerfile: notification/Dockerfile
    container_name: sfo-notification-consumer
    restart: always
    command: ["python", "-m", "notification.consumer"]
    environment:
      <<: *jwt-env
      USER_SERVICE_URL: http://user-service:8001
      KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      SCHEMA_REGISTRY_URL: http://schema-registry:8081
      RABBITMQ_USER: ${RABBITMQ_USER:?set RABBITMQ_USER in the root .env}
      RABBITMQ_PASSWORD: ${RABBITMQ_PASSWORD:?set RABBITMQ_PASSWORD in the root .env}
    networks:
      - smartfoodops-network

  # The concurrency pool for the slow simulated dispatch in notification/tasks.py. No
  # sibling calls, no JWT keys — it never imports common.auth, only notification.tasks —
  # which is what makes its environment strictly smaller than the consumer's.
  notification-worker:
    build:
      context: ./services
      dockerfile: notification/Dockerfile
    container_name: sfo-notification-worker
    restart: always
    command: ["celery", "-A", "notification.worker", "worker", "--loglevel=info"]
    environment:
      RABBITMQ_USER: ${RABBITMQ_USER:?set RABBITMQ_USER in the root .env}
      RABBITMQ_PASSWORD: ${RABBITMQ_PASSWORD:?set RABBITMQ_PASSWORD in the root .env}
    networks:
      - smartfoodops-network

  # --- 4. ORCHESTRATOR (D36, D47) ---
  # Where workflows live and run. Split out of the Order Service by D36: before that,
  # `order-service` held a Temporal client directly and `order-worker` shared its image and
  # database. This pair owns no database at all, and reaches every fact it needs, including
  # the order itself, over HTTP on the internal key — see orchestrator/clients/order/ and
  # order/apis/transitions.py for the boundary that replaced the direct database access.
  #
  # D47 took the saga routes off the API side. It was the front door for starting and
  # signalling workflows; services do that themselves now, through their own Temporal
  # client, so what is left here is the health probe and /metrics. The worker below is the
  # half that does the work, and orchestrator/registry.py declares what it runs.
  orchestrator-service:
    build:
      context: ./services
      dockerfile: orchestrator/Dockerfile
    container_name: sfo-orchestrator-service
    restart: always
    environment:
      <<: *jwt-env
      TEMPORAL_ADDRESS: temporal-server:7233
    depends_on:
      temporal-server:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # Executes the workflow and its activities. Shares this pair's image, not the Order
  # Service's — only the command differs between the two.
  #
  # It needs every sibling URL the activities actually call, including the Order Service
  # itself now: `transition_order_activity` and `read_kitchen_decision_activity` used to
  # write and read `sfo_order_core` directly; both are HTTP calls now, so ORDER_SERVICE_URL
  # joins PAYMENT_SERVICE_URL and RIDER_SERVICE_URL here for the first time. Still no
  # RESTAURANT_SERVICE_URL: since D32 the saga never calls that service at all — the
  # kitchen's decision is a column on `orders`, and the restaurant's capacity and
  # coordinates ride in the workflow payload, captured once at checkout.
  orchestrator-worker:
    build:
      context: ./services
      dockerfile: orchestrator/Dockerfile
    container_name: sfo-orchestrator-worker
    restart: always
    command: ["python", "-m", "orchestrator.worker"]
    environment:
      <<: *jwt-env
      TEMPORAL_ADDRESS: temporal-server:7233
      ORDER_SERVICE_URL: http://order-service:8004
      PAYMENT_SERVICE_URL: http://payment-service:8005
      RIDER_SERVICE_URL: http://rider-service:8006
    depends_on:
      temporal-server:
        condition: service_healthy
    networks:
      - smartfoodops-network
EOF

# 4b. Write out the observability configuration (Week 3, D42).
echo "📈 Generating prometheus/prometheus.yml and grafana/provisioning..."
mkdir -p prometheus grafana/provisioning/datasources grafana/provisioning/dashboards
cat << 'EOF' > prometheus/prometheus.yml
# Scrape config for SmartFoodOps. Every target is a container on smartfoodops-network,
# reached by its Compose service name — no app service publishes its port to the host, and
# none needs to for Prometheus to see it.
global:
  scrape_interval: 10s
  evaluation_interval: 10s

scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  # Metrics port pinned since Week 2 specifically for this (docker-compose.yml).
  - job_name: "temporal-server"
    static_configs:
      - targets: ["temporal-server:9233"]

  # The seven FastAPI services, each exposing /metrics via common/telemetry.py.
  - job_name: "user-service"
    static_configs:
      - targets: ["user-service:8001"]

  - job_name: "restaurant-service"
    static_configs:
      - targets: ["restaurant-service:8002"]

  - job_name: "menu-service"
    static_configs:
      - targets: ["menu-service:8003"]

  - job_name: "order-service"
    static_configs:
      - targets: ["order-service:8004"]

  - job_name: "payment-service"
    static_configs:
      - targets: ["payment-service:8005"]

  - job_name: "rider-service"
    static_configs:
      - targets: ["rider-service:8006"]

  - job_name: "orchestrator-service"
    static_configs:
      - targets: ["orchestrator-service:8007"]

  # Not a FastAPI process — no /metrics route, so it runs its own bare prometheus_client
  # HTTP server (services/orchestrator/worker.py) instead.
  - job_name: "orchestrator-worker"
    static_configs:
      - targets: ["orchestrator-worker:9108"]

  # Kafka read-model (Week 3, D40) — same /metrics wiring as every other FastAPI service.
  - job_name: "analytics-service"
    static_configs:
      - targets: ["analytics-service:8008"]

  # Not a FastAPI process, same reasoning as orchestrator-worker: a bare prometheus_client
  # HTTP server started in notification/consumer.py::main (Week 3, D45).
  - job_name: "notification-consumer"
    static_configs:
      - targets: ["notification-consumer:9110"]

  # The broker itself, via the rabbitmq_prometheus plugin (rabbitmq/enabled_plugins) — the
  # observability week should not add a component that is itself unobservable.
  - job_name: "rabbitmq"
    static_configs:
      - targets: ["rabbitmq:15692"]
EOF

cat << 'EOF' > grafana/provisioning/datasources/prometheus.yml
# Provisioned, not clicked together in the UI, so `docker compose down -v` does not lose it
# (D42). Grafana reads every file under /etc/grafana/provisioning on startup.
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
EOF

cat << 'EOF' > grafana/provisioning/dashboards/dashboards.yml
# Points Grafana at the JSON dashboard files in this same directory. Dashboards are loaded
# from disk on startup and re-read on the interval below, so editing a JSON file here and
# restarting the container is the update path — no export/import through the UI.
apiVersion: 1

providers:
  - name: SmartFoodOps
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /etc/grafana/provisioning/dashboards
      foldersFromFilesStructure: false
EOF

# 4c. Write out the Kafka topic-init script (Week 3, D42) — mounted into the
# `kafka-init` one-shot container defined in the docker-compose.yml heredoc above.
echo "📨 Generating kafka/init-topics.sh..."
mkdir -p kafka
cat << 'EOF' > kafka/init-topics.sh
#!/usr/bin/env bash
# Creates the platform's Kafka topics explicitly, at the partition count per-order
# ordering depends on, then exits — this is a migration step, not a long-running service
# (see kafka-init's `restart: "no"` in docker-compose.yml).
#
# `--if-not-exists` makes every run idempotent, so `docker compose up` can run this
# against an already-initialised broker with no effect. Partition count and topic names
# must match services/common/events/topics.py — that Python module is the source of truth
# for what a producer/consumer expects to exist; this script is what makes it exist.
#
# Auto-create is deliberately disabled on the broker (KAFKA_AUTO_CREATE_TOPICS_ENABLE:
# 'false'): without an explicit init step, Kafka would silently create a 1-partition topic
# on the first publish, and a typo'd topic name would create a new topic instead of failing.

set -euo pipefail

BOOTSTRAP_SERVER="kafka:29092"
PARTITIONS=3
REPLICATION_FACTOR=1

create_topic() {
  local topic="$1"
  kafka-topics --bootstrap-server "$BOOTSTRAP_SERVER" --create --if-not-exists \
    --topic "$topic" --partitions "$PARTITIONS" --replication-factor "$REPLICATION_FACTOR"
}

create_topic "sfo.order.events.v1"
create_topic "sfo.order.events.v1.dlq"

echo "topics ready"
EOF
chmod +x kafka/init-topics.sh

# 4d. Write out the RabbitMQ plugin config (Week 3, D45).
echo "🐇 Generating rabbitmq/enabled_plugins..."
mkdir -p rabbitmq
cat << 'EOF' > rabbitmq/enabled_plugins
% Bind-mounted into the container at /etc/rabbitmq/enabled_plugins (Week 3, D42-style
% declarative config, matching prometheus/prometheus.yml and kafka/init-topics.sh rather
% than a custom ENTRYPOINT/command override). `rabbitmq_management` ships the UI on 15672;
% `rabbitmq_prometheus` is what makes the broker itself observable on 15692 — the
% observability week should not add a component that is itself a blind spot.
[rabbitmq_management,rabbitmq_prometheus].
EOF

# 5. Write out the Nginx routing gateway configuration
echo "🔒 Generating api-gateway/nginx.conf..."
cat << 'EOF' > api-gateway/nginx.conf
events {
    worker_connections 1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;
    sendfile        on;
    keepalive_timeout  65;

    # Standard Main Access log format
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$upstream_addr"';
    access_log /var/log/nginx/access.log main;

    server {
        listen 80;
        server_name localhost;

        # 👤 Route User service requests
        location /api/v1/users {
            proxy_pass http://user-service:8001;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 🏪 Route Restaurant service requests
        location /api/v1/restaurants {
            proxy_pass http://restaurant-service:8002;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 📜 Route Menu service requests
        location /api/v1/menus {
            proxy_pass http://menu-service:8003;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 🛍️ Route Order service requests
        location /api/v1/orders {
            proxy_pass http://order-service:8004;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 💳 Route Payment service requests
        location /api/v1/payments {
            proxy_pass http://payment-service:8005;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 🛵 Route Rider service requests
        location /api/v1/riders {
            proxy_pass http://rider-service:8006;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 🎼 Route only the Orchestrator Service's health probe. `location =` is an exact
        # match, not a prefix, and since D47 there is nothing else on that service to
        # match: the saga routes it once carried are gone, because services start and
        # signal workflows through their own Temporal client rather than over HTTP. The
        # exact match stays anyway — it is what keeps a future route on this service from
        # becoming publicly reachable the moment someone adds one.
        location = /api/v1/orchestrator/health {
            proxy_pass http://orchestrator-service:8007;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # The Temporal Web UI (localhost:8233) is deliberately absent. It is an operator
        # tool with no authentication of its own, so proxying it through the public gateway
        # would publish every workflow's history — including the arguments each was started
        # with. Reach it directly on the host instead.

        # Global health check endpoint. Enveloped the same as every service response
        # (D35), so a client does not need a special case for the one health probe that
        # isn't behind a service's own router.
        location /health {
            return 200 '{"status": 200, "body": {"service": "api-gateway"}, "message": "gateway is healthy", "errors": null}';
            add_header Content-Type application/json;
        }
    }
}
EOF

echo "✨ Success! Directory setup complete and all core config files written."
echo "👉 Run: 'cd smartfoodops-backend' to enter your project directory."
