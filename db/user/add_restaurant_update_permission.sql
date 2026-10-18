-- ============================================================================
-- One-off migration for an already-running sfo-user-db container (D68).
--
-- Adds `restaurant:update`, grants it to restaurant_admin (and system_admin, which holds everything), and registers
-- `PATCH /api/v1/restaurants/{restaurant_id}`, the route an owner uses to set their kitchen's preparation time.
--   docker exec -i sfo-user-db psql -U sfo_user_admin -d sfo_user_core < db/user/add_restaurant_update_permission.sql
--
-- Mirrors the same rows in db/user/init.sql. Idempotent. Run BEFORE the new restaurant-service build goes live:
-- until the route row exists the gateway refuses the route with a 403 (fail-closed, D57). The policy snapshot
-- refreshes every POLICY_CACHE_TTL_SECONDS (30s), with no user-service restart.
-- ============================================================================

BEGIN;

INSERT INTO permissions (name, description) VALUES
('restaurant:update', 'Change settings of a restaurant the caller owns, such as its preparation time (D68)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r JOIN permissions p ON p.name = 'restaurant:update'
WHERE r.name IN ('restaurant_admin', 'system_admin')
ON CONFLICT DO NOTHING;

INSERT INTO route_permissions (method, path_pattern, permission_id, description)
SELECT 'PATCH', '/api/v1/restaurants/{restaurant_id}', p.id, 'Change a restaurant''s settings; ownership checked in the handler'
FROM permissions p WHERE p.name = 'restaurant:update'
ON CONFLICT (method, path_pattern) DO NOTHING;

COMMIT;
