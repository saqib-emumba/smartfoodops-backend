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
