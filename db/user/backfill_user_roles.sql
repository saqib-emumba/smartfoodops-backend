-- ============================================================================
-- One-off migration for an already-running sfo-user-db container.
--
-- init.sql only runs on an empty Postgres volume (docker-entrypoint-initdb.d), so a
-- deployment that already has data needs this run by hand instead:
--   docker exec -i sfo-user-db psql -U <user> -d sfo_user_core < db/user/backfill_user_roles.sql
--
-- Run the verification query between the two statements below before trusting the
-- DROP COLUMN — this file intentionally does NOT wrap both in one blind transaction.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, role_id)
);

-- Backfill: every existing user's single role becomes their first grant.
INSERT INTO user_roles (user_id, role_id)
SELECT id, role_id FROM users
ON CONFLICT DO NOTHING;

COMMIT;

-- --- Verify before proceeding --------------------------------------------------------
-- SELECT count(*) FROM users;
-- SELECT count(*) FROM user_roles;
-- These two counts must match (one grant per user) before running the DROP below.

-- BEGIN;
-- ALTER TABLE users DROP COLUMN role_id;
-- COMMIT;
