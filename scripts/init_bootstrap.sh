#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "🚀 Bootstrapping SmartFoodOps Local Environment..."

# 1. Create the modular directory structure
echo "📂 Creating services, gateway and per-service database directories..."
mkdir -p smartfoodops-backend/{api-gateway,db/{user,restaurant,order,payment,menu},services/{common,user,restaurant,menu,order,payment}}
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

# Services Endpoints (Within Docker Network)
USER_SERVICE_URL=http://user-service:8001
RESTAURANT_SERVICE_URL=http://restaurant-service:8002
MENU_SERVICE_URL=http://menu-service:8003
ORDER_SERVICE_URL=http://order-service:8004
PAYMENT_SERVICE_URL=http://payment-service:8005
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

# 3. Write out one initialization migration script per physical database.
# Each is mounted into its own container, so a service's schema exists only in the database
# that service holds credentials for.
echo "🐘 Generating Postgres schemas (db/{user,restaurant,order,payment,menu}/init.sql)..."
cat << 'EOF' > db/user/init.sql
-- ============================================================================
-- User Service database — sfo_user_core (container sfo-user-db, host port 5432)
--
-- Owns identity: `roles`, `users` and the `riders` profile extension. Only the
-- User Service connects here; every other service reads a profile through
-- GET /api/v1/users/{user_id}.
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
    role_id INT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Case-insensitive unique constraint index for emails (prevent duplicate registrations)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));

-- 2. Riders Table
-- A rider is an extension of a user identity, so it stays in this database where the
-- foreign key to `users` is still enforceable. Orders reference a rider by id only.
CREATE TABLE IF NOT EXISTS riders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_type VARCHAR(100) NOT NULL,
    vehicle_number VARCHAR(100) UNIQUE NOT NULL,
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    current_latitude DECIMAL(9, 6),
    current_longitude DECIMAL(9, 6),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_riders_availability ON riders(is_available);
EOF

cat << 'EOF' > db/restaurant/init.sql
-- ============================================================================
-- Restaurant Service database — sfo_restaurant_core
-- (container sfo-restaurant-db, host port 5433)
--
-- Owns `restaurants`. Only the Restaurant Service connects here; every other
-- service reads a restaurant through GET /api/v1/restaurants/{restaurant_id}.
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

# 4. Write out the Docker Compose orchestration configuration
echo "🐳 Generating docker-compose.yml..."
cat << 'EOF' > docker-compose.yml
version: '3.8'

networks:
  smartfoodops-network:
    driver: bridge

volumes:
  user_postgres_data:
  restaurant_postgres_data:
  order_postgres_data:
  payment_postgres_data:
  menu_postgres_data:
  redis_data:

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
    depends_on:
      db-order-postgres:
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
    depends_on:
      db-payment-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network
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

        # Global health check endpoint
        location /health {
            return 200 '{"status": "gateway_is_healthy"}';
            add_header Content-Type application/json;
        }
    }
}
EOF

echo "✨ Success! Directory setup complete and all core config files written."
echo "👉 Run: 'cd smartfoodops-backend' to enter your project directory."
