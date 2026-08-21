"""Redis cache-aside layer in front of the `menus` table.

A menu is read on every checkout — the Order Service re-prices each cart against it (D06)
— and written only when a restaurant publishes. That read/write ratio is what makes a
cache worth having here and nowhere else in the platform.

Two rules hold everything together:

* **The cache is never authoritative.** Every method here swallows Redis failures and
  reports the miss, so a dead cache degrades the service to "always reads Postgres"
  rather than taking menus offline. Postgres is the source of truth; this is a copy.
* **A publish invalidates rather than updates.** Writing the new tree into Redis would
  leave two places that must agree; deleting the key leaves one, and the next reader
  repopulates it from the row that was actually committed.

Logical database 0, kept apart from the User Service's refresh tokens in database 1: an
accidental FLUSHDB here costs a cache rebuild, not everybody's session.
"""

from logging import Logger
from uuid import UUID

import redis

from common.config import MENU_CACHE_TTL_SECONDS, REDIS_TIMEOUT

_KEY_PREFIX = "menu:"


def _key(restaurant_id: UUID) -> str:
    return f"{_KEY_PREFIX}{restaurant_id}"


class MenuCache:
    """Redis-backed menu cache, opened for the life of the process.

    Blocking client and plain methods: this service is synchronous throughout — psycopg2
    and `def` handlers — so FastAPI already runs each request in a worker thread.
    """

    def __init__(
        self, url: str, *, logger: Logger, ttl_seconds: int = MENU_CACHE_TTL_SECONDS
    ):
        self._url = url
        self._logger = logger
        self._ttl = ttl_seconds
        self._client: redis.Redis | None = None

    def connect(self) -> None:
        self._client = redis.Redis.from_url(
            self._url,
            socket_timeout=REDIS_TIMEOUT,
            socket_connect_timeout=REDIS_TIMEOUT,
            decode_responses=True,
        )
        self._logger.info("Menu cache initialised (TTL %ss)", self._ttl)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_reachable(self) -> bool:
        try:
            return bool(self._client and self._client.ping())
        except redis.RedisError as exc:  # pragma: no cover - health must never raise
            self._logger.warning("Menu cache health probe failed: %s", exc)
            return False

    def get(self, restaurant_id: UUID) -> str | None:
        """Return the cached menu JSON, or None on a miss — including a Redis outage."""
        try:
            return self._client.get(_key(restaurant_id))
        except redis.RedisError as exc:
            self._logger.warning(
                "Menu cache lookup failed, falling back to Postgres: %s", exc
            )
            return None

    def store(self, restaurant_id: UUID, menu_json: str) -> None:
        """Populate the cache with a TTL, so a missed invalidation self-heals eventually."""
        try:
            self._client.setex(_key(restaurant_id), self._ttl, menu_json)
        except redis.RedisError as exc:
            self._logger.warning("Could not populate the menu cache: %s", exc)

    def invalidate(self, restaurant_id: UUID) -> None:
        """Drop the cached copy after a publish.

        Logged at error level, unlike the read path: a failure here means customers keep
        being quoted the previous prices until the TTL lapses, which is a real if bounded
        correctness problem rather than a lost optimisation.
        """
        try:
            self._client.delete(_key(restaurant_id))
        except redis.RedisError as exc:
            self._logger.error(
                "Could not invalidate the cached menu for restaurant %s; stale prices "
                "may be served for up to %ss: %s",
                restaurant_id,
                self._ttl,
                exc,
            )
