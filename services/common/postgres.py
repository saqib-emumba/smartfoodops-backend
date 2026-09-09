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


def constraint_of(exc) -> str:
    """The unique constraint a UniqueViolation names, or '' when the driver reported none.

    Only the accessor is shared. Which constraint maps to which 409 message stays in the
    repository that owns the table — those messages are the content, not boilerplate.
    """
    return getattr(exc.diag, "constraint_name", None) or ""


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
        # `cursor_factory` is passed here, at connect time, rather than per-call in
        # `cursor()` below — deliberately (Week 3). `opentelemetry-instrumentation-psycopg2`
        # injects its tracing wrapper as the CONNECTION's default cursor factory, built
        # from whichever `cursor_factory` it sees passed to `psycopg2.connect(...)`; a
        # `cursor_factory` supplied instead at `conn.cursor(cursor_factory=...)` time
        # overrides that default and bypasses the tracing wrapper entirely — silently, no
        # error, just no span. Passing it here means `conn.cursor()` below can be called
        # with no arguments and still get both the traced cursor and RealDictCursor rows,
        # whether or not OpenTelemetry is instrumented in this process.
        self._pool = pool.ThreadedConnectionPool(
            minconn=self._minconn,
            maxconn=self._maxconn,
            dsn=self._dsn,
            cursor_factory=RealDictCursor,
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
            # No `cursor_factory=` here — the connection's own default, set once at
            # connect time above, already produces RealDictCursor rows (traced ones, when
            # instrumented). See the comment in `lifespan()` for why call-time and
            # connect-time are not interchangeable here.
            with conn.cursor() as cur:
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
