"""Pooled PostgreSQL access for the services that own relational tables.

Wraps psycopg2's threaded pool so that lease/commit/rollback ordering and failure
translation are written once instead of in every service. The pool is opened by the
FastAPI lifespan and closed on shutdown, so connections are never created per request.
"""

from contextlib import asynccontextmanager, contextmanager
from logging import Logger

import psycopg2
from fastapi import FastAPI
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from common.config import POOL_MAX_CONNECTIONS, POOL_MIN_CONNECTIONS
from common.errors import internal_error

DEFAULT_EXHAUSTED_DETAIL = "Database connection pool exhausted"


class PostgresPool:
    """A bounded connection pool bound to one service's FastAPI lifespan."""

    def __init__(
        self,
        dsn: str,
        *,
        logger: Logger,
        minconn: int = POOL_MIN_CONNECTIONS,
        maxconn: int = POOL_MAX_CONNECTIONS,
        exhausted_detail: str = DEFAULT_EXHAUSTED_DETAIL,
    ):
        self._dsn = dsn
        self._logger = logger
        self._minconn = minconn
        self._maxconn = maxconn
        # Services word pool exhaustion differently because the caller-visible
        # consequence differs (e.g. the Order Service states that no order was created).
        self._exhausted_detail = exhausted_detail
        self._pool: pool.ThreadedConnectionPool | None = None

    @asynccontextmanager
    async def lifespan(self, _: FastAPI):
        """Open a bounded connection pool for the lifetime of the process.

        Pass directly as ``FastAPI(lifespan=db.lifespan)``.
        """
        self._pool = pool.ThreadedConnectionPool(
            minconn=self._minconn, maxconn=self._maxconn, dsn=self._dsn
        )
        self._logger.info("PostgreSQL connection pool initialised")
        try:
            yield
        finally:
            self._pool.closeall()
            self._pool = None

    @contextmanager
    def cursor(self, commit: bool = False):
        """Lease a pooled connection. A starved/unreachable pool surfaces as 500.

        The connection is committed only on a clean exit when ``commit`` is set;
        any exception rolls back before the connection returns to the pool.
        """
        if self._pool is None:
            raise internal_error("Database pool is not initialised")
        try:
            conn = self._pool.getconn()
        except (pool.PoolError, psycopg2.OperationalError) as exc:
            self._logger.error("Could not lease a PostgreSQL connection: %s", exc)
            raise internal_error(self._exhausted_detail) from exc
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
            conn.commit() if commit else conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def is_reachable(self) -> bool:
        """Round-trip the database for a health probe. Never raises."""
        try:
            with self.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
            return True
        except Exception as exc:  # pragma: no cover - health must never raise
            self._logger.warning("Health check database probe failed: %s", exc)
            return False
