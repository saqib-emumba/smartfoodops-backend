"""SmartFoodOps User Service — registration and profile lookups (Port 8001).

Owns its own PostgreSQL database: the `users` and `riders` tables plus the `roles` lookup
that role names resolve against. No other service can read `users` — they hold no
credentials for this database and call GET /api/v1/users/{user_id} instead.
"""

import os
from uuid import UUID

import bcrypt
from fastapi import FastAPI, status

from common.config import required
from common.errors import not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import UserRepository
from schemas import UserRegisterRequest, UserResponse

SERVICE_NAME = "user-service"
DATABASE_URL = required("DATABASE_URL")

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(DATABASE_URL, logger=logger)
users = UserRepository(db)

app = FastAPI(title="SmartFoodOps User Service", lifespan=db.lifespan)


def hash_password(plaintext: str) -> str:
    """Hash with a per-password salt; the plaintext is never stored or logged."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


@app.get("/api/v1/users/health")
def health():
    """Liveness probe that also proves the database round-trips."""
    return {
        "status": "User Service is up and connected",
        "service": SERVICE_NAME,
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "database_reachable": db.is_reachable(),
    }


@app.post(
    "/api/v1/users/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_user(payload: UserRegisterRequest) -> UserResponse:
    """Register a user, mapping the incoming role name to roles.id via a DB lookup."""
    row = users.register(payload, hash_password(payload.password))
    return UserResponse(**row)


@app.get("/api/v1/users/{user_id}", response_model=UserResponse)
def get_user(user_id: UUID) -> UserResponse:
    """Resolve a single profile — the boundary other services read instead of `users`."""
    row = users.find(user_id)
    if row is None:
        raise not_found(f"User {user_id} not found")
    return UserResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
