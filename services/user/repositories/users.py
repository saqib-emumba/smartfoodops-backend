"""PostgreSQL access for the `users` table and the `roles` lookup.

All SQL for this service lives here so that route handlers stay free of query text and
driver-specific error handling.
"""

from uuid import UUID

import psycopg2
from fastapi import HTTPException

from common.errors import bad_request, conflict
from common.postgres import constraint_of
from common.repository import Repository
from user.schemas.users import UserRegisterRequest

_INSERT_USER = """
    INSERT INTO users (email, password_hash, full_name, phone)
    VALUES (%s, %s, %s, %s)
    RETURNING id, email, full_name, phone
"""

_INSERT_USER_ROLE = """
    INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)
    ON CONFLICT DO NOTHING
"""

# array_agg fans the user_roles join back into one row per user, with the role names sorted
# so a test (or anything else) comparing the array gets a deterministic order.
_SELECT_USER = """
    SELECT u.id, u.email, u.full_name, u.phone,
           array_agg(r.name ORDER BY r.name) AS roles
    FROM users u
    JOIN user_roles ur ON ur.user_id = u.id
    JOIN roles r ON r.id = ur.role_id
    WHERE u.id = %s
    GROUP BY u.id, u.email, u.full_name, u.phone
"""

# The only query that reads password_hash. Kept separate from _SELECT_USER so the hash
# cannot reach a response model by accident — the profile endpoints use that one.
_SELECT_CREDENTIALS = """
    SELECT u.id, u.password_hash,
           array_agg(r.name ORDER BY r.name) AS roles
    FROM users u
    JOIN user_roles ur ON ur.user_id = u.id
    JOIN roles r ON r.id = ur.role_id
    WHERE u.email = %s
    GROUP BY u.id, u.password_hash
"""

_SELECT_ROLES_FOR_USER = """
    SELECT u.id, array_agg(r.name ORDER BY r.name) AS roles
    FROM users u
    JOIN user_roles ur ON ur.user_id = u.id
    JOIN roles r ON r.id = ur.role_id
    WHERE u.id = %s
    GROUP BY u.id
"""

_COUNT_ROLES_FOR_USER = "SELECT count(*) AS n FROM user_roles WHERE user_id = %s"

_DELETE_USER_ROLE = "DELETE FROM user_roles WHERE user_id = %s AND role_id = %s"


def _duplicate_account(exc: psycopg2.errors.UniqueViolation) -> HTTPException:
    """Name the field that collided so the caller knows what to change."""
    constraint = constraint_of(exc)
    field = "phone number" if "phone" in constraint else "email address"
    return conflict(f"An account with this {field} already exists")


class UserRepository(Repository):
    def register(self, payload: UserRegisterRequest, password_hash: str) -> dict:
        """Insert a user and grant the resolved role, in the same transaction."""
        with self._db.cursor(commit=True) as cur:
            role = self._resolve_role(cur, payload.role)
            try:
                cur.execute(
                    _INSERT_USER,
                    (str(payload.email), password_hash, payload.full_name, payload.phone),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise _duplicate_account(exc) from exc
            row = cur.fetchone()
            cur.execute(_INSERT_USER_ROLE, (row["id"], role["id"]))

        return {**row, "roles": [role["name"]]}

    def find(self, user_id: UUID) -> dict | None:
        """Resolve a single profile, joining user_roles/roles for the granted role names."""
        return self.one(_SELECT_USER, (str(user_id),))

    def find_credentials(self, email: str) -> dict | None:
        """Fetch id, roles and password_hash for a login attempt.

        The only path that reads the hash. Callers must not let the result reach a
        response — see main.login, which pulls out `id` and `roles` and drops the rest.
        """
        return self.one(_SELECT_CREDENTIALS, (email,))

    def find_roles(self, user_id: UUID) -> dict | None:
        """Re-read a user's current role set when minting a token from a refresh exchange.

        Deliberately re-read rather than carried in the refresh token: a role granted or
        revoked mid-session takes effect at the next refresh instead of lingering for days.
        """
        return self.one(_SELECT_ROLES_FOR_USER, (str(user_id),))

    def grant_role(self, user_id: UUID, role_name: str) -> None:
        """Add a role grant. Idempotent — granting an already-held role is a no-op."""
        with self._db.cursor(commit=True) as cur:
            role = self._resolve_role(cur, role_name)
            cur.execute(_INSERT_USER_ROLE, (str(user_id), role["id"]))

    def revoke_role(self, user_id: UUID, role_name: str) -> None:
        """Remove a role grant. Refuses to leave a user with zero roles."""
        with self._db.cursor(commit=True) as cur:
            role = self._resolve_role(cur, role_name)
            cur.execute(_COUNT_ROLES_FOR_USER, (str(user_id),))
            if cur.fetchone()["n"] <= 1:
                raise conflict("A user must always hold at least one role")
            cur.execute(_DELETE_USER_ROLE, (str(user_id), role["id"]))

    @staticmethod
    def _resolve_role(cur, role_name: str) -> dict:
        """Map an incoming role name onto roles.id.

        The `roles` table is the single source of truth, so an unknown name is a 400 from
        this lookup rather than a 422 from schema validation.
        """
        cur.execute("SELECT id, name FROM roles WHERE name = %s", (role_name,))
        role = cur.fetchone()
        if role is not None:
            return role

        cur.execute("SELECT name FROM roles ORDER BY id")
        valid = [row["name"] for row in cur.fetchall()]
        raise bad_request(f"Unknown role '{role_name}'. Valid roles: {valid}")
