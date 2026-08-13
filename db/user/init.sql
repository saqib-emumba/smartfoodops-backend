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
