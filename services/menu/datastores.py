"""MongoDB and Redis client lifecycle for the Menu Service.

This service owns the `menus` and `order_tracking_logs` collections and never touches
PostgreSQL. Clients are created once at startup so their connection pools are reused.
"""

import os
from contextlib import asynccontextmanager
from logging import Logger

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from common.config import DEFAULT_MONGO_URI, DEFAULT_REDIS_URL, MONGO_TIMEOUT_MS
from common.errors import service_unavailable

MONGO_URI = os.getenv("MONGO_URI", DEFAULT_MONGO_URI)
REDIS_URL = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
REDIS_TIMEOUT = 2.0


class DocumentStores:
    """Holds the Mongo and Redis clients for the life of the process."""

    def __init__(self, *, logger: Logger):
        self._logger = logger
        self.mongo_client: AsyncIOMotorClient | None = None
        self.db = None
        self.redis = None

    @asynccontextmanager
    async def lifespan(self, _: FastAPI):
        """Create the Mongo/Redis clients once and reuse their connection pools."""
        self.mongo_client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
            socketTimeoutMS=MONGO_TIMEOUT_MS,
            connectTimeoutMS=MONGO_TIMEOUT_MS,
        )
        self.db = self.mongo_client.get_default_database()
        self.redis = aioredis.from_url(REDIS_URL, socket_timeout=REDIS_TIMEOUT)
        self._logger.info("Mongo and Redis clients initialised")
        try:
            yield
        finally:
            self.mongo_client.close()
            await self.redis.aclose()
            self.mongo_client, self.db, self.redis = None, None, None

    def unavailable(self, exc: Exception) -> HTTPException:
        """Translate a MongoDB socket timeout / network failure into 503."""
        self._logger.error("MongoDB operation failed: %s", exc)
        return service_unavailable(
            "Document database is unavailable; please retry shortly"
        )

    async def mongo_reachable(self) -> bool:
        try:
            await self.mongo_client.admin.command("ping")
            return True
        except Exception as exc:  # pragma: no cover - health must never raise
            self._logger.warning("Mongo health probe failed: %s", exc)
            return False

    async def redis_reachable(self) -> bool:
        try:
            await self.redis.ping()
            return True
        except Exception as exc:  # pragma: no cover - health must never raise
            self._logger.warning("Redis health probe failed: %s", exc)
            return False
