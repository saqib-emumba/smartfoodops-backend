"""SmartFoodOps Restaurant Service — onboarding and lookups (Port 8002).

Owns the PostgreSQL `restaurants` table. Owner identity/authorisation is resolved over HTTP
against the User Service so that this service never touches the `users` table directly.
"""

import logging
import os
from contextlib import asynccontextmanager, contextmanager
from uuid import UUID

import httpx
import psycopg2
from fastapi import FastAPI, HTTPException, status
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from schemas import RestaurantOnboardRequest, RestaurantResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("restaurant-service")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core",
)
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:8001")
OWNER_ROLE = "restaurant_admin"
HTTP_TIMEOUT = 5.0

db_pool = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global db_pool
    db_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
    logger.info("PostgreSQL connection pool initialised")
    try:
        yield
    finally:
        db_pool.closeall()
        db_pool = None


app = FastAPI(title="SmartFoodOps Restaurant Service", lifespan=lifespan)


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


def verify_owner(owner_id: UUID) -> dict:
    """Confirm via the User Service that the owner exists and may onboard restaurants."""
    url = f"{USER_SERVICE_URL}/api/v1/users/{owner_id}"
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.get(url)
    except httpx.RequestError as exc:
        logger.error("User Service unreachable at %s: %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User Service is unreachable; cannot verify restaurant owner",
        ) from exc

    if response.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Owner {owner_id} does not exist",
        )
    if response.status_code != status.HTTP_200_OK:
        logger.error(
            "Unexpected User Service response %s: %s",
            response.status_code,
            response.text[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from User Service while verifying owner",
        )

    owner = response.json()
    if owner.get("role") != OWNER_ROLE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Owner {owner_id} has role '{owner.get('role')}' and is not authorised "
                f"to onboard restaurants (requires '{OWNER_ROLE}')"
            ),
        )
    return owner


@app.get("/api/v1/restaurants/health")
def health():
    db_ok = True
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    except Exception as exc:  # pragma: no cover - health must never raise
        logger.warning("Health check database probe failed: %s", exc)
        db_ok = False
    return {
        "status": "Restaurant Service is operational",
        "service": "restaurant-service",
        "database_reachable": db_ok,
        "user_service_url": USER_SERVICE_URL,
    }


@app.post(
    "/api/v1/restaurants/onboard",
    response_model=RestaurantResponse,
    status_code=status.HTTP_201_CREATED,
)
def onboard_restaurant(payload: RestaurantOnboardRequest) -> RestaurantResponse:
    """Onboard a restaurant once its owner is verified through the User Service."""
    verify_owner(payload.owner_id)

    with db_cursor(commit=True) as cur:
        try:
            cur.execute(
                """
                INSERT INTO restaurants (owner_id, name, address, latitude, longitude, capacity)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, owner_id, name, address, latitude, longitude, is_active, capacity
                """,
                (
                    str(payload.owner_id),
                    payload.name,
                    payload.address,
                    payload.latitude,
                    payload.longitude,
                    payload.capacity,
                ),
            )
        except psycopg2.errors.ForeignKeyViolation as exc:
            # Owner disappeared between verification and insert.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Owner {payload.owner_id} does not exist",
            ) from exc
        row = cur.fetchone()

    return RestaurantResponse(**row)


@app.get("/api/v1/restaurants/{restaurant_id}", response_model=RestaurantResponse)
def get_restaurant(restaurant_id: UUID) -> RestaurantResponse:
    """Expose restaurant state (including is_active) for other services to verify."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, owner_id, name, address, latitude, longitude, is_active, capacity
            FROM restaurants
            WHERE id = %s
            """,
            (str(restaurant_id),),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Restaurant {restaurant_id} not found",
        )
    return RestaurantResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
