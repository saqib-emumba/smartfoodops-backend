-- ============================================================================
-- One-off migration for an already-running sfo-user-db container (D57).
--
-- init.sql only runs on an empty Postgres volume (docker-entrypoint-initdb.d), so a
-- deployment that already has data needs this run by hand instead:
--   docker exec -i sfo-user-db psql -U sfo_user_admin -d sfo_user_core < db/user/add_rbac_permissions.sql
--
-- Adds the three RBAC policy tables and seeds them with the grant matrix the platform
-- shipped with. Everything below mirrors sections 1d-1f of db/user/init.sql exactly, and
-- every statement is idempotent, so re-running this is a no-op.
--
-- Deploy ordering: run this BEFORE the new user-service image. The policy table is read at
-- startup and an empty one is a boot failure by design (services/user/policy.py) — the old
-- image ignores these tables, so applying the migration early is safe and applying it late
-- means the new user-service refuses to start until it lands.
--
-- Nothing here is destructive: no column is dropped and no existing row is rewritten, so
-- unlike backfill_user_roles.sql this file needs no gated second step.
--
-- PREREQUISITE: `roles.id` must already be UUID. Commit 6c53df6 changed it from SERIAL and
-- shipped no migration, because this repo has no migration tooling and handles schema
-- changes by resetting the volume (README, "There is no migration tooling"). A database
-- still carrying the old INT column cannot hold these foreign keys, so the guard below
-- stops with a readable message instead of letting Postgres report a bare "foreign key
-- constraint cannot be implemented". Reset that one volume and let init.sql rebuild it:
--   docker compose rm -sf db-user-postgres
--   docker volume rm smartfoodops-backend_user_postgres_data
--   docker compose up -d db-user-postgres
-- ============================================================================

BEGIN;

DO $$
DECLARE
    id_type text;
BEGIN
    SELECT data_type INTO id_type
      FROM information_schema.columns
     WHERE table_name = 'roles' AND column_name = 'id';

    IF id_type IS NULL THEN
        RAISE EXCEPTION 'No `roles` table here — is this the User Service database?';
    ELSIF id_type <> 'uuid' THEN
        RAISE EXCEPTION
            'roles.id is % but these tables need uuid (commit 6c53df6). This database '
            'predates that change and has no migration path; reset the user_postgres_data '
            'volume and let db/user/init.sql rebuild it — see the header of this file.',
            id_type;
    END IF;
END $$;

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
('delivery:report',    'Report pickup and delivery of an assigned order')
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
    ('POST',  '/api/v1/riders/me/orders/{order_id}/delivered',  'delivery:report', 'Report delivery')
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

COMMIT;

-- --- Verify after running ------------------------------------------------------------
-- Expect 10 permissions, 19 role grants (10 explicit + 9 completing system_admin), and 23
-- routes of which 15 demand a permission and 8 are authenticated-only:
-- SELECT count(*) FROM permissions;
-- SELECT count(*) FROM role_permissions;
-- SELECT count(*) FILTER (WHERE permission_id IS NOT NULL) AS gated,
--        count(*) FILTER (WHERE permission_id IS NULL)     AS auth_only
--   FROM route_permissions;
--
-- The artifact this whole change exists to produce — who can do what, in one query:
-- SELECT r.name AS role, p.name AS permission
--   FROM roles r
--   JOIN role_permissions rp ON rp.role_id = r.id
--   JOIN permissions p ON p.id = rp.permission_id
--  ORDER BY r.name, p.name;
