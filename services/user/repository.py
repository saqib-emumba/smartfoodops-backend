"""PostgreSQL access for the `users` table and the `roles` lookup.

All SQL for this service lives here so that route handlers stay free of query text and
driver-specific error handling.
"""

from uuid import UUID

import psycopg2
from fastapi import HTTPException

from common.errors import bad_request, conflict
from common.postgres import PostgresPool
from schemas import UserRegisterRequest

_INSERT_USER = """
    INSERT INTO users (email, password_hash, full_name, phone, role_id)
    VALUES (%s, %s, %s, %s, %s)
    RETURNING id, email, full_name, phone
"""

_SELECT_USER = """
    SELECT u.id, u.email, u.full_name, u.phone, r.name AS role
    FROM users u
    JOIN roles r ON r.id = u.role_id
    WHERE u.id = %s
"""

# The only query that reads password_hash. Kept separate from _SELECT_USER so the hash
# cannot reach a response model by accident — the profile endpoints use that one.
_SELECT_CREDENTIALS = """
    SELECT u.id, u.password_hash, r.name AS role
    FROM users u
    JOIN roles r ON r.id = u.role_id
    WHERE u.email = %s
"""

_SELECT_ROLE_FOR_USER = """
    SELECT u.id, r.name AS role
    FROM users u
    JOIN roles r ON r.id = u.role_id
    WHERE u.id = %s
"""


def _duplicate_account(exc: psycopg2.errors.UniqueViolation) -> HTTPException:
    """Name the field that collided so the caller knows what to change."""
    constraint = getattr(exc.diag, "constraint_name", None) or ""
    field = "phone number" if "phone" in constraint else "email address"
    return conflict(f"An account with this {field} already exists")


class UserRepository:
    def __init__(self, db: PostgresPool):
        self._db = db

    def register(self, payload: UserRegisterRequest, password_hash: str) -> dict:
        """Insert a user, resolving the role name to roles.id in the same transaction."""
        with self._db.cursor(commit=True) as cur:
            role = self._resolve_role(cur, payload.role)
            try:
                cur.execute(
                    _INSERT_USER,
                    (
                        str(payload.email),
                        password_hash,
                        payload.full_name,
                        payload.phone,
                        role["id"],
                    ),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise _duplicate_account(exc) from exc
            row = cur.fetchone()

        return {**row, "role": role["name"]}

    def find(self, user_id: UUID) -> dict | None:
        """Resolve a single profile, joining the roles lookup table for the role name."""
        with self._db.cursor() as cur:
            cur.execute(_SELECT_USER, (str(user_id),))
            return cur.fetchone()

    def find_credentials(self, email: str) -> dict | None:
        """Fetch id, role and password_hash for a login attempt.

        The only path that reads the hash. Callers must not let the result reach a
        response — see main.login, which pulls out `id` and `role` and drops the rest.
        """
        with self._db.cursor() as cur:
            cur.execute(_SELECT_CREDENTIALS, (email,))
            return cur.fetchone()

    def find_role(self, user_id: UUID) -> dict | None:
        """Re-read a user's current role when minting a token from a refresh exchange.

        Deliberately re-read rather than carried in the refresh token: a role changed
        mid-session takes effect at the next refresh instead of lingering for days.
        """
        with self._db.cursor() as cur:
            cur.execute(_SELECT_ROLE_FOR_USER, (str(user_id),))
            return cur.fetchone()

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
