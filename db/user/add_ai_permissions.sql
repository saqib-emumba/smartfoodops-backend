-- ============================================================================
-- One-off migration for an already-running sfo-user-db container (Week 4, D59).
--
-- Adds the `ai:search` permission, grants it to customers, and registers the route that
-- demands it. init.sql only runs on an empty volume, so an existing deployment runs this by
-- hand, once:
--   docker exec -i sfo-user-db psql -U sfo_user_admin -d sfo_user_core < db/user/add_ai_permissions.sql
--
-- Mirrors the `ai:search` rows in sections 1d-1f of db/user/init.sql exactly. Every statement
-- is idempotent, so re-running it is a no-op. Nothing is dropped or rewritten.
--
-- Deploy ordering: run BEFORE the ai-service build goes live. Until the route row exists the
-- gateway refuses POST /api/v1/ai/search with a 403 (fail-closed, D57) — a safe failure, but
-- a failure. The policy snapshot refreshes every POLICY_CACHE_TTL_SECONDS (30s), so the new
-- route is live that long after this commits, with no user-service restart.
-- ============================================================================

BEGIN;

INSERT INTO permissions (name, description) VALUES
('ai:search', 'Semantic search over dishes and restaurants (Week 4, D59)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p ON p.name = 'ai:search'
WHERE r.name = 'customer'
ON CONFLICT DO NOTHING;

-- system_admin holds every permission (see init.sql 1e): re-run the same cross join so the
-- table keeps answering "who can do what" on its own, without the `is_admin` bypass.
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r CROSS JOIN permissions p
WHERE r.name = 'system_admin'
ON CONFLICT DO NOTHING;

INSERT INTO route_permissions (method, path_pattern, permission_id, description)
SELECT 'POST', '/api/v1/ai/search', p.id, 'Hybrid semantic search over dishes and restaurants'
FROM permissions p
WHERE p.name = 'ai:search'
ON CONFLICT (method, path_pattern) DO NOTHING;

COMMIT;

-- Verify (expect: ai:search granted to customer and system_admin; one route row):
-- SELECT r.name AS role, p.name AS permission
--   FROM roles r JOIN role_permissions rp ON rp.role_id = r.id
--   JOIN permissions p ON p.id = rp.permission_id WHERE p.name = 'ai:search' ORDER BY r.name;
-- SELECT method, path_pattern FROM route_permissions WHERE path_pattern = '/api/v1/ai/search';
