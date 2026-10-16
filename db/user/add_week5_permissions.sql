-- ============================================================================
-- One-off migration for an already-running sfo-user-db container (Week 5, D63/D64).
--
-- Adds `ai:assistant`, `ai:explain_order`, `ai:partner` and `order:review`, grants them
-- (customer: assistant, explain_order, review; restaurant_admin: partner; system_admin: all), and
-- registers the routes that demand them. init.sql only runs on an empty volume, so an existing
-- deployment runs this by hand, once:
--   docker exec -i sfo-user-db psql -U sfo_user_admin -d sfo_user_core < db/user/add_week5_permissions.sql
--
-- Mirrors the same rows in sections 1d-1f of db/user/init.sql exactly. Every statement is
-- idempotent, so re-running is a no-op.
--
-- Deploy ordering: run BEFORE the ai-service / order-service builds go live. Until a route row
-- exists the gateway refuses that route with a 403 (fail-closed, D57) — a safe failure, but a
-- failure. The policy snapshot refreshes every POLICY_CACHE_TTL_SECONDS (30s), so the new routes
-- are live that long after this commits, with no user-service restart.
-- ============================================================================

BEGIN;

INSERT INTO permissions (name, description) VALUES
('ai:assistant',     'Ask the conversational food assistant (streamed answers grounded in retrieved context, D63)'),
('ai:explain_order', 'Have the AI explain the status or delay of an order the caller owns (D64)'),
('ai:partner',       'Use the AI authoring tools for a restaurant the caller owns: dish copy, promos, review summaries (D64)'),
('order:review',     'Review a delivered order the caller placed (D64)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM (VALUES
    ('customer',         'ai:assistant'),
    ('customer',         'ai:explain_order'),
    ('customer',         'order:review'),
    ('restaurant_admin', 'ai:partner')
) AS grant_pair(role_name, permission_name)
JOIN roles r ON r.name = grant_pair.role_name
JOIN permissions p ON p.name = grant_pair.permission_name
ON CONFLICT DO NOTHING;

-- system_admin holds every permission (see init.sql 1e).
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r CROSS JOIN permissions p
WHERE r.name = 'system_admin'
ON CONFLICT DO NOTHING;

INSERT INTO route_permissions (method, path_pattern, permission_id, description)
SELECT route.method, route.path_pattern, p.id, route.description
FROM (VALUES
    ('GET',  '/api/v1/ai/assistant/stream',       'ai:assistant',     'Conversational assistant, streamed over SSE'),
    ('POST', '/api/v1/ai/explain-order',          'ai:explain_order', 'Explain an order; ownership checked in the handler'),
    ('POST', '/api/v1/ai/partner/describe-dish',  'ai:partner',       'Draft a dish description from ingredients'),
    ('POST', '/api/v1/ai/partner/promo',          'ai:partner',       'Draft promotional copy for an offer'),
    ('POST', '/api/v1/ai/partner/review-summary', 'ai:partner',       'Summarise a restaurant''s reviews; ownership checked in the handler'),
    ('POST', '/api/v1/orders/{order_id}/review',  'order:review',     'Review a delivered order; ownership checked in the handler')
) AS route(method, path_pattern, permission_name, description)
JOIN permissions p ON p.name = route.permission_name
ON CONFLICT (method, path_pattern) DO NOTHING;

COMMIT;

-- Verify (expect: four new permissions; six new route rows):
-- SELECT r.name AS role, p.name AS permission
--   FROM roles r JOIN role_permissions rp ON rp.role_id = r.id
--   JOIN permissions p ON p.id = rp.permission_id
--  WHERE p.name IN ('ai:assistant','ai:explain_order','ai:partner','order:review') ORDER BY 1, 2;
-- SELECT method, path_pattern FROM route_permissions
--  WHERE path_pattern LIKE '/api/v1/ai/%' OR path_pattern LIKE '%/review' ORDER BY 2;
