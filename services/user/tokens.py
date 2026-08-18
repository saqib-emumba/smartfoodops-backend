"""Refresh-token storage for the User Service.

Access tokens are stateless and cannot be withdrawn once signed, so the refresh token is
what a session actually hangs on: revoke it and the session dies within one access-token
lifetime. That makes this store the thing `POST /logout` acts on.

Two deliberate choices:

* Only the SHA-256 of a token is stored. A dump of this Redis is then a list of hashes
  rather than a set of live credentials, the same reasoning that keeps plaintext passwords
  out of Postgres.
* Refreshing rotates. The presented token is consumed in the same round trip that reads it
  (GETDEL), so a token replayed by an attacker who captured it finds nothing there.

The User Service is synchronous — psycopg2 and plain `def` handlers — so this uses the
blocking Redis client, unlike the Menu Service's async one.
"""

import hashlib
from logging import Logger
from uuid import UUID

import redis

from common.config import REFRESH_TOKEN_TTL_DAYS

REFRESH_TTL_SECONDS = REFRESH_TOKEN_TTL_DAYS * 24 * 60 * 60
REDIS_TIMEOUT = 2.0

_KEY_PREFIX = "refresh:"


def _key(token: str) -> str:
    """Hash before use, so the raw token is never a key we hold."""
    return _KEY_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


class RefreshTokenStore:
    """Redis-backed session store, opened for the life of the process."""

    def __init__(self, url: str, *, logger: Logger):
        self._url = url
        self._logger = logger
        self._client: redis.Redis | None = None

    def connect(self) -> None:
        self._client = redis.Redis.from_url(
            self._url,
            socket_timeout=REDIS_TIMEOUT,
            socket_connect_timeout=REDIS_TIMEOUT,
            decode_responses=True,
        )
        self._logger.info("Refresh token store initialised")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_reachable(self) -> bool:
        try:
            return bool(self._client and self._client.ping())
        except redis.RedisError as exc:  # pragma: no cover - health must never raise
            self._logger.warning("Refresh store health probe failed: %s", exc)
            return False

    def store(self, token: str, user_id: UUID) -> None:
        """Record an issued token against its user, expiring with the session."""
        self._client.setex(_key(token), REFRESH_TTL_SECONDS, str(user_id))

    def consume(self, token: str) -> UUID | None:
        """Atomically read and delete a token, returning its user, or None if unknown.

        The delete is the rotation: whoever presents this token gets a new one, and this
        one stops working for everybody — including whoever else may have copied it.
        """
        user_id = self._client.getdel(_key(token))
        return UUID(user_id) if user_id else None

    def revoke_owned(self, token: str, user_id: UUID) -> bool:
        """Drop a token on logout, but only if it belongs to the caller.

        The ownership check costs one read and stops a logout request from ending someone
        else's session. Not atomic, which is acceptable here: the loser of the race is a
        token that was being deleted anyway.
        """
        key = _key(token)
        if self._client.get(key) != str(user_id):
            return False
        return bool(self._client.delete(key))
