"""The Redis connection lifecycle shared by the menu cache and the refresh token store.

Both open one blocking client for the life of the process with the same two timeouts, both
close it on shutdown, and both must answer a health probe without ever raising. That is the
whole of what they have in common — key schemes, TTLs and every read and write stay in the
owning service, because what is *in* the cache is that service's business.

Only imported by the two services that install `redis`, so it is not part of the chassis
requirements list.
"""

from logging import Logger

import redis

from common.config import REDIS_TIMEOUT


class RedisStore:
    """A process-lifetime Redis client with a health probe.

    Blocking client and plain methods on purpose: both services using it are synchronous
    throughout — psycopg2 and `def` handlers — so FastAPI already runs each request in a
    worker thread.
    """

    def __init__(self, url: str, *, logger: Logger, name: str):
        self._url = url
        self._logger = logger
        self._name = name
        self._client: redis.Redis | None = None

    def connect(self, detail: str = "") -> None:
        """Open the client. `detail` appends to the log line, e.g. a TTL."""
        self._client = redis.Redis.from_url(
            self._url,
            socket_timeout=REDIS_TIMEOUT,
            socket_connect_timeout=REDIS_TIMEOUT,
            decode_responses=True,
        )
        self._logger.info("%s initialised%s", self._name, detail)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_reachable(self) -> bool:
        """Whether Redis answers PING. Never raises — this feeds a health endpoint."""
        try:
            return bool(self._client and self._client.ping())
        except redis.RedisError as exc:  # pragma: no cover - health must never raise
            self._logger.warning("%s health probe failed: %s", self._name, exc)
            return False
