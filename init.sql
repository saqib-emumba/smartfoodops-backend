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
