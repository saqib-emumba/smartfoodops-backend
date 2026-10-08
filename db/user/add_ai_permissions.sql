-- ============================================================================
-- One-off migration for an already-running sfo-user-db container (Week 4, D59).
--
-- Adds the AI Service's permissions (`ai:search`, `ai:rag_context`), grants them to customers,
-- and registers the routes that demand them. init.sql only runs on an empty volume, so an existing deployment runs this by
-- hand, once:
--   docker exec -i sfo-user-db psql -U sfo_user_admin -d sfo_user_core < db/user/add_ai_permissions.sql
--
-- Mirrors the `ai:*` rows in sections 1d-1f of db/user/init.sql exactly. Every statement is
-- idempotent, so re-running it is a no-op — which is also how a database that already ran the
-- Phase 4 version of this file picks up `ai:rag_context`. Nothing is dropped or rewritten.
--
-- Deploy ordering: run BEFORE the ai-service build goes live. Until a route row exists the
-- gateway refuses that route with a 403 (fail-closed, D57) — a safe failure, but
-- a failure. The policy snapshot refreshes every POLICY_CACHE_TTL_SECONDS (30s), so the new
-- route is live that long after this commits, with no user-service restart.
-- ============================================================================

BEGIN;

INSERT INTO permissions (name, description) VALUES
('ai:search',      'Semantic search over dishes and restaurants (Week 4, D59)'),
('ai:rag_context', 'Assemble RAG context (matches, order history, courier availability) for own account (D62)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p ON p.name IN ('ai:search', 'ai:rag_context')
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
SELECT route.method, route.path_pattern, p.id, route.description
FROM (VALUES
    ('POST', '/api/v1/ai/search',      'ai:search',      'Hybrid semantic search over dishes and restaurants'),
    ('POST', '/api/v1/ai/rag-context', 'ai:rag_context', 'Multi-source RAG context; ownership of customer_id checked in the handler')
) AS route(method, path_pattern, permission_name, description)
JOIN permissions p ON p.name = route.permission_name
ON CONFLICT (method, path_pattern) DO NOTHING;

COMMIT;

-- Verify (expect: both permissions granted to customer and system_admin; two route rows):
-- SELECT r.name AS role, p.name AS permission
--   FROM roles r JOIN role_permissions rp ON rp.role_id = r.id
--   JOIN permissions p ON p.id = rp.permission_id WHERE p.name LIKE 'ai:%' ORDER BY r.name, p.name;
-- SELECT method, path_pattern FROM route_permissions WHERE path_pattern LIKE '/api/v1/ai/%';
