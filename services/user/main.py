"""SmartFoodOps User Service — registration and profile lookups (Port 8001).

Owns the PostgreSQL `users` table and resolves role names against the `roles` lookup table.
Other services must never read `users` directly; they call GET /api/v1/users/{user_id}.
"""

import logging
import os
from contextlib import asynccontextmanager, contextmanager
from uuid import UUID

import bcrypt
import psycopg2
from fastapi import FastAPI, HTTPException, status
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from schemas import UserRegisterRequest, UserResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("user-service")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core",
)

db_pool = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Open a bounded connection pool for the lifetime of the process."""
    global db_pool
    db_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
    logger.info("PostgreSQL connection pool initialised")
    try:
        yield
    finally:
        db_pool.closeall()
        db_pool = None


app = FastAPI(title="SmartFoodOps User Service", lifespan=lifespan)


@contextmanager
def db_cursor(commit: bool = False):
    """Lease a pooled connection. A starved/unreachable pool surfaces as 500."""
    if db_pool is None:
        raise HTTPException(status_code=500, detail="Database pool is not initialised")
    try:
        conn = db_pool.getconn()
    except (pool.PoolError, psycopg2.OperationalError) as exc:
        logger.error("Could not lease a PostgreSQL connection: %s", exc)
        raise HTTPException(
            status_code=500, detail="Database connection pool exhausted"
        ) from exc
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit() if commit else conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


@app.get("/api/v1/users/health")
def health():
    """Liveness probe that also proves the database round-trips."""
    db_ok = True
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    except Exception as exc:  # pragma: no cover - health must never raise
        logger.warning("Health check database probe failed: %s", exc)
        db_ok = False
    return {
        "status": "User Service is up and connected",
        "service": "user-service",
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "database_reachable": db_ok,
    }


@app.post(
    "/api/v1/users/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_user(payload: UserRegisterRequest) -> UserResponse:
    """Register a user, mapping the incoming role name to roles.id via a DB lookup."""
    password_hash = bcrypt.hashpw(
        payload.password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT id, name FROM roles WHERE name = %s", (payload.role,))
        role = cur.fetchone()
        if role is None:
            cur.execute("SELECT name FROM roles ORDER BY id")
            valid = [row["name"] for row in cur.fetchall()]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown role '{payload.role}'. Valid roles: {valid}",
            )

        try:
            cur.execute(
                """
                INSERT INTO users (email, password_hash, full_name, phone, role_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, email, full_name, phone
                """,
                (
                    str(payload.email),
                    password_hash,
                    payload.full_name,
                    payload.phone,
                    role["id"],
                ),
            )
        except psycopg2.errors.UniqueViolation as exc:
            constraint = getattr(exc.diag, "constraint_name", None) or ""
            field = "phone number" if "phone" in constraint else "email address"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An account with this {field} already exists",
            ) from exc
        row = cur.fetchone()

    return UserResponse(**row, role=role["name"])


@app.get("/api/v1/users/{user_id}", response_model=UserResponse)
def get_user(user_id: UUID) -> UserResponse:
    """Resolve a single profile, joining the roles lookup table for the role name."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.full_name, u.phone, r.name AS role
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s
            """,
            (str(user_id),),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found"
        )
    return UserResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
