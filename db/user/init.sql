-- ============================================================================
-- User Service database — sfo_user_core (container sfo-user-db, host port 5432)
--
-- Owns identity and nothing else: `roles`, `users`, and since D57 the access-control
-- policy itself (`permissions`, `role_permissions`, `route_permissions`). Only the User
-- Service connects here; every other service reads a profile through
-- GET /api/v1/users/{user_id}, and learns what a caller may do from the identity headers
-- the API gateway sets after its verify subrequest.
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
-- UUID PK, like every other table's id here, rather than a SERIAL: the old int PK was the
-- one surrogate key in this schema that wasn't a UUID, which was a red flag for anyone
-- reading the ERD rather than a deliberate choice. Still ordered by created_at (not id)
-- wherever the seed order matters, since a UUID carries no ordering of its own.
CREATE TABLE IF NOT EXISTS roles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
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
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, role_id)
);

-- 1d. Permissions — the vocabulary a role is granted in, and the only thing a route ever
-- demands (D57). Named `<resource>:<action>` so a grant reads as a sentence and so an
-- auditor can tell from the name alone what a role can do. Decoupling this from the URL
-- that implements it is the whole point: renaming a route is a `route_permissions` edit,
-- not a re-grant to every role that held the capability.
CREATE TABLE IF NOT EXISTS permissions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO permissions (name, description) VALUES
('order:create',       'Place an order as the paying customer'),
('order:read_any',     'Read any order regardless of ownership (operator tooling)'),
('payment:create',     'Pay for an order directly, outside the saga'),
('payment:read',       'Read a payment belonging to an order the caller owns'),
('restaurant:onboard', 'Register a restaurant and become its admin'),
('menu:write',         'Publish or replace a restaurant menu'),
('kitchen:read',       'Read a restaurant kitchen queue'),
('kitchen:decide',     'Accept or reject an order on behalf of a restaurant'),
('rider:profile',      'Join the fleet and maintain own rider profile, location, availability'),
('delivery:report',    'Report pickup and delivery of an assigned order'),
('ai:search',          'Semantic search over dishes and restaurants (Week 4, D59)'),
('ai:rag_context',     'Assemble RAG context (matches, order history, courier availability) for own account (D62)')
ON CONFLICT (name) DO NOTHING;

-- 1e. Role <-> permission grants. Composite PK for the same reason as user_roles (1c):
-- "this role holds this permission" has no attributes worth a surrogate id, and the PK is
-- itself the no-duplicate-grant constraint. ON DELETE CASCADE both ways — unlike a role, a
-- permission grant is not reference data anyone points at, it *is* the pointer.
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id UUID NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (role_id, permission_id)
);

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM (VALUES
    ('customer',         'order:create'),
    ('customer',         'payment:create'),
    ('customer',         'payment:read'),
    ('customer',         'ai:search'),
    ('customer',         'ai:rag_context'),
    ('restaurant_admin', 'restaurant:onboard'),
    ('restaurant_admin', 'menu:write'),
    ('restaurant_admin', 'kitchen:read'),
    ('restaurant_admin', 'kitchen:decide'),
    ('rider',            'rider:profile'),
    ('rider',            'delivery:report'),
    ('system_admin',     'order:read_any')
) AS grant_pair(role_name, permission_name)
JOIN roles r ON r.name = grant_pair.role_name
JOIN permissions p ON p.name = grant_pair.permission_name
ON CONFLICT DO NOTHING;

-- system_admin holds every permission, including ones added later by migration. Seeded
-- rather than left implicit so this table answers "who can do what" on its own, without a
-- reader having to know about the `is_admin` bypass in services/common/auth.py. That bypass
-- stays in code as well: a permission added here but never granted must not lock an
-- operator out of the endpoint that needs it. Deliberate redundancy, not an oversight.
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r CROSS JOIN permissions p
WHERE r.name = 'system_admin'
ON CONFLICT DO NOTHING;

-- 1f. Which permission a route demands — read by the API gateway's verify subrequest, so a
-- wrong-role request is refused at nginx before any backend service sees it (D57).
--
-- `path_pattern` is the FastAPI-style templated path, not a regex: it is legible in a plain
-- SELECT, and services/user/policy.py compiles it to `^/api/v1/orders/[^/]+/accept$` once at
-- load. Matching is fail-closed — a gated path with no row here is refused, so adding a route
-- without adding a row is a visible 403, never a silently open endpoint.
--
-- A NULL permission_id means "any authenticated caller": the route is gated by an ownership
-- check in its handler (require_self_or_admin, verify_owner), which needs the resource row
-- and so can never move to the gateway.
--
-- Public routes (login, register, refresh, */health) and internal-key routes (X-Internal-Key,
-- D15) are deliberately ABSENT rather than listed: nginx exempts them from auth_request, so
-- the policy decision point never sees them at all. Absence here is not an oversight.
CREATE TABLE IF NOT EXISTS route_permissions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    method VARCHAR(10) NOT NULL,
    path_pattern VARCHAR(200) NOT NULL,
    permission_id UUID REFERENCES permissions(id) ON DELETE RESTRICT,
    description TEXT,
    UNIQUE (method, path_pattern)
);

INSERT INTO route_permissions (method, path_pattern, permission_id, description)
SELECT route.method, route.path_pattern, p.id, route.description
FROM (VALUES
    ('POST',  '/api/v1/orders',                            'order:create',       'Checkout'),
    ('GET',   '/api/v1/orders',                            'order:read_any',     'List orders by status'),
    ('POST',  '/api/v1/payments',                          'payment:create',     'Direct payment'),
    ('GET',   '/api/v1/payments/{payment_id}',              'payment:read',       'Payment state; ownership settled by reading its order as the caller'),
    ('POST',  '/api/v1/restaurants/onboard',               'restaurant:onboard', 'Restaurant onboarding'),
    ('POST',  '/api/v1/menus',                             'menu:write',         'Publish a menu'),
    ('GET',   '/api/v1/orders/kitchen/{restaurant_id}',    'kitchen:read',       'Kitchen queue'),
    ('POST',  '/api/v1/orders/{order_id}/accept',          'kitchen:decide',     'Kitchen accepts'),
    ('POST',  '/api/v1/orders/{order_id}/reject',          'kitchen:decide',     'Kitchen rejects'),
    ('POST',  '/api/v1/riders',                            'rider:profile',      'Join the fleet'),
    ('GET',   '/api/v1/riders/me',                         'rider:profile',      'Own rider profile'),
    ('PATCH', '/api/v1/riders/me/location',                'rider:profile',      'Own location'),
    ('PATCH', '/api/v1/riders/me/availability',            'rider:profile',      'Own availability'),
    ('POST',  '/api/v1/riders/me/orders/{order_id}/picked-up', 'delivery:report', 'Report pickup'),
    ('POST',  '/api/v1/riders/me/orders/{order_id}/delivered',  'delivery:report', 'Report delivery'),
    ('POST',  '/api/v1/ai/search',                         'ai:search',          'Hybrid semantic search over dishes and restaurants'),
    ('POST',  '/api/v1/ai/rag-context',                    'ai:rag_context',     'Multi-source RAG context; ownership of customer_id checked in the handler')
) AS route(method, path_pattern, permission_name, description)
JOIN permissions p ON p.name = route.permission_name
ON CONFLICT (method, path_pattern) DO NOTHING;

-- Authenticated-but-not-role-gated: the handler's own ownership check is the real guard.
INSERT INTO route_permissions (method, path_pattern, permission_id, description) VALUES
('POST',   '/api/v1/users/logout',                   NULL, 'Own session; refresh token is the credential'),
('GET',    '/api/v1/users/{user_id}',                NULL, 'require_self_or_admin'),
('POST',   '/api/v1/users/{user_id}/roles',          NULL, 'require_self_or_admin + system_admin carve-out'),
('DELETE', '/api/v1/users/{user_id}/roles/{role_name}', NULL, 'require_self_or_admin + system_admin carve-out'),
('GET',    '/api/v1/restaurants/{restaurant_id}',    NULL, 'Any authenticated user may read a restaurant'),
('GET',    '/api/v1/menus/{restaurant_id}',          NULL, 'Any authenticated user may read a menu'),
('GET',    '/api/v1/orders/{order_id}',              NULL, 'require_self_or_admin on customer_id'),
('GET',    '/api/v1/orders/{order_id}/logs',         NULL, 'require_self_or_admin on customer_id')
ON CONFLICT (method, path_pattern) DO NOTHING;
