#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "🚀 Bootstrapping SmartFoodOps Local Environment..."

# 1. Create the modular directory structure
echo "📂 Creating services and gateway directories..."
mkdir -p smartfoodops-backend/{api-gateway,services/{user,restaurant,menu,order}}
cd smartfoodops-backend

# 2. Write out the environment variables configuration
echo "📝 Generating .env configuration file..."
cat << 'EOF' > .env
# Database Credentials
POSTGRES_DB=smartfoodops_core
POSTGRES_USER=sfo_admin
POSTGRES_PASSWORD=sfo_password_123
POSTGRES_HOST=db-postgres
POSTGRES_PORT=5432

MONGO_URI=mongodb://db-nosql:27017/smartfoodops_menus
REDIS_URL=redis://cache-redis:6379/0

# Services Endpoints (Within Docker Network)
USER_SERVICE_URL=http://user-service:8001
RESTAURANT_SERVICE_URL=http://restaurant-service:8002
MENU_SERVICE_URL=http://menu-service:8003
ORDER_SERVICE_URL=http://order-service:8004
EOF

# 3. Write out the normalized database initialization migration script
echo "🐘 Generating Postgres Database Schema (init.sql)..."
cat << 'EOF' > init.sql
-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Define custom ENUM types
CREATE TYPE order_status AS ENUM ('created', 'confirmed', 'assigned', 'picked_up', 'delivered', 'cancelled');
CREATE TYPE payment_status AS ENUM ('pending', 'authorized', 'captured', 'refunded');

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

-- 2. Restaurants Table
CREATE TABLE IF NOT EXISTS restaurants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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

-- 3. Riders Table
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

-- 4. Orders Table (Primary Registry with JSONB Items and Idempotency Guard)
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE RESTRICT,
    rider_id UUID REFERENCES riders(id) ON DELETE SET NULL,
    items JSONB NOT NULL, -- Stores snapshot of ordered items, prices, and selected customization options at checkout
    total_amount DECIMAL(10, 2) NOT NULL,
    status order_status NOT NULL DEFAULT 'created',
    idempotency_key VARCHAR(255) UNIQUE, -- Protects order creation writes against API duplicate submissions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- 5. Payments Table (Built with Idempotency Protection)
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID UNIQUE NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status payment_status NOT NULL DEFAULT 'pending',
    transaction_reference VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
EOF

# 4. Write out the Docker Compose orchestration configuration
echo "🐳 Generating docker-compose.yml..."
cat << 'EOF' > docker-compose.yml
version: '3.8'

networks:
  smartfoodops-network:
    driver: bridge

volumes:
  postgres_data:
  mongodb_data:
  redis_data:

services:
  # --- 1. DATABASES & CACHING ---
  db-postgres:
    image: postgres:15-alpine
    container_name: sfo-postgres
    restart: always
    environment:
      POSTGRES_DB: smartfoodops_core
      POSTGRES_USER: sfo_admin
      POSTGRES_PASSWORD: sfo_password_123
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql # Auto-runs DDL migrations on boot
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_admin -d smartfoodops_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  db-nosql:
    image: mongo:6.0
    container_name: sfo-mongodb
    restart: always
    ports:
      - "27017:27017"
    volumes:
      - mongodb_data:/data/db
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 5s
      timeout: 5s
      retries: 5

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
    networks:
      - smartfoodops-network

  # --- 3. CORE SERVICE CONTAINER SERVICES ---
  user-service:
    build:
      context: ./services/user
      dockerfile: Dockerfile
    container_name: sfo-user-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core
    depends_on:
      db-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  restaurant-service:
    build:
      context: ./services/restaurant
      dockerfile: Dockerfile
    container_name: sfo-restaurant-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core
    depends_on:
      db-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  menu-service:
    build:
      context: ./services/menu
      dockerfile: Dockerfile
    container_name: sfo-menu-service
    restart: always
    environment:
      - MONGO_URI=mongodb://db-nosql:27017/smartfoodops_menus
      - REDIS_URL=redis://cache-redis:6379/0
    depends_on:
      db-nosql:
        condition: service_healthy
      cache-redis:
        condition: service_healthy
    networks:
      - smartfoodops-network

  order-service:
    build:
      context: ./services/order
      dockerfile: Dockerfile
    container_name: sfo-order-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core
      - USER_SERVICE_URL=http://user-service:8001
      - RESTAURANT_SERVICE_URL=http://restaurant-service:8002
      - MENU_SERVICE_URL=http://menu-service:8003
    depends_on:
      db-postgres:
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
