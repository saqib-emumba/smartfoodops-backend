"""SmartFoodOps Menu Service — hierarchical menus and order audit logs (Port 8003).

Owns the MongoDB `menus` and `order_tracking_logs` collections. Restaurant existence and
active state are resolved over HTTP against the Restaurant Service; this service never
touches PostgreSQL.
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from schemas import MenuResponse, MenuUpsertRequest, OrderTrackingLogCreateRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("menu-service")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://db-nosql:27017/smartfoodops_menus")
REDIS_URL = os.getenv("REDIS_URL", "redis://cache-redis:6379/0")
RESTAURANT_SERVICE_URL = os.getenv(
    "RESTAURANT_SERVICE_URL", "http://restaurant-service:8002"
)
HTTP_TIMEOUT = 5.0
MONGO_TIMEOUT_MS = 5000

mongo_client: AsyncIOMotorClient | None = None
mongo_db = None
redis_client = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create the Mongo/Redis clients once and reuse their connection pools."""
    global mongo_client, mongo_db, redis_client
    mongo_client = AsyncIOMotorClient(
        MONGO_URI,
        serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
        socketTimeoutMS=MONGO_TIMEOUT_MS,
        connectTimeoutMS=MONGO_TIMEOUT_MS,
    )
    mongo_db = mongo_client.get_default_database()
    redis_client = aioredis.from_url(REDIS_URL, socket_timeout=2.0)
    logger.info("Mongo and Redis clients initialised")
    try:
        yield
    finally:
        mongo_client.close()
        await redis_client.aclose()
        mongo_client, mongo_db, redis_client = None, None, None


app = FastAPI(title="SmartFoodOps Menu Service", lifespan=lifespan)


def unavailable(exc: Exception) -> HTTPException:
    """Translate a MongoDB socket timeout / network failure into 503."""
    logger.error("MongoDB operation failed: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Document database is unavailable; please retry shortly",
    )


async def verify_restaurant_active(restaurant_id: UUID) -> dict:
    """Confirm via the Restaurant Service that the restaurant exists and is active."""
    url = f"{RESTAURANT_SERVICE_URL}/api/v1/restaurants/{restaurant_id}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        logger.error("Restaurant Service unreachable at %s: %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restaurant Service is unreachable; cannot verify restaurant",
        ) from exc

    if response.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Restaurant {restaurant_id} does not exist",
        )
    if response.status_code != status.HTTP_200_OK:
        logger.error(
            "Unexpected Restaurant Service response %s: %s",
            response.status_code,
            response.text[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from Restaurant Service",
        )

    restaurant = response.json()
    if not restaurant.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Restaurant {restaurant_id} is not active",
        )
    return restaurant


@app.get("/api/v1/menus/health")
async def health():
    mongo_ok, redis_ok = True, True
    try:
        await mongo_client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - health must never raise
        logger.warning("Mongo health probe failed: %s", exc)
        mongo_ok = False
    try:
        await redis_client.ping()
    except Exception as exc:  # pragma: no cover - health must never raise
        logger.warning("Redis health probe failed: %s", exc)
        redis_ok = False
    return {
        "status": "Menu Service running with NoSQL + Redis connection",
        "service": "menu-service",
        "mongo_reachable": mongo_ok,
        "redis_reachable": redis_ok,
        "restaurant_service_url": RESTAURANT_SERVICE_URL,
    }


@app.post("/api/v1/menus", response_model=MenuResponse)
async def upsert_menu(payload: MenuUpsertRequest) -> MenuResponse:
    """Upsert the full category/item/customization tree for one restaurant."""
    await verify_restaurant_active(payload.restaurant_id)

    now = datetime.now(timezone.utc)
    categories = [category.model_dump() for category in payload.categories]
    try:
        document = await mongo_db.menus.find_one_and_update(
            {"restaurant_id": str(payload.restaurant_id)},
            {
                "$set": {"categories": categories, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError as exc:
        raise unavailable(exc) from exc

    return MenuResponse(
        restaurant_id=document["restaurant_id"], categories=document["categories"]
    )


@app.post("/api/v1/menus/logs", status_code=status.HTTP_201_CREATED)
async def log_order_status(payload: OrderTrackingLogCreateRequest):
    """Append a status transition to the order's `order_tracking_logs` document."""
    entry = {
        "status": payload.status,
        "timestamp": datetime.now(timezone.utc),
        "service": payload.service,
        "raw_log": payload.raw_log,
        "updated_by": payload.updated_by,
        "metadata": payload.metadata or {},
    }
    try:
        result = await mongo_db.order_tracking_logs.update_one(
            {"order_id": str(payload.order_id)},
            {"$push": {"status_history": entry}},
            upsert=True,
        )
    except PyMongoError as exc:
        raise unavailable(exc) from exc

    return {
        "message": "Audit log captured",
        "order_id": str(payload.order_id),
        "created_document": result.upserted_id is not None,
        "status": payload.status,
    }


@app.get("/api/v1/menus/{restaurant_id}", response_model=MenuResponse)
async def get_menu(restaurant_id: UUID) -> MenuResponse:
    """Serve the active menu tree — used by the Order Service to price a checkout."""
    try:
        document = await mongo_db.menus.find_one({"restaurant_id": str(restaurant_id)})
    except PyMongoError as exc:
        raise unavailable(exc) from exc

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No menu published for restaurant {restaurant_id}",
        )
    return MenuResponse(
        restaurant_id=document["restaurant_id"], categories=document["categories"]
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
