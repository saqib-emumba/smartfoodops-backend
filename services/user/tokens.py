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

Logical database 1, kept clear of the Menu Service's cache in database 0: an accidental
FLUSHDB there costs a cache rebuild, not everybody's session.
"""

import hashlib
from logging import Logger
from uuid import UUID

from common.config import REFRESH_TOKEN_TTL_DAYS
from common.redis_store import RedisStore

REFRESH_TTL_SECONDS = REFRESH_TOKEN_TTL_DAYS * 24 * 60 * 60

_KEY_PREFIX = "refresh:"


def _key(token: str) -> str:
    """Hash before use, so the raw token is never a key we hold."""
    return _KEY_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


class RefreshTokenStore(RedisStore):
    """Redis-backed session store. The connection lifecycle comes from RedisStore; the key
    scheme — SHA-256 of the token, never the token itself — and rotate-on-refresh stay here.
    """

    def __init__(self, url: str, *, logger: Logger):
        super().__init__(url, logger=logger, name="Refresh token store")

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
