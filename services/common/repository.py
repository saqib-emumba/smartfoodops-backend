"""The lease-a-cursor / execute / fetch shape every repository repeats.

Holds no SQL, no table names and no column lists: those are each service's own knowledge and
stay in the repository that owns the table. This is only the boilerplate around them, which
appeared about fifteen times across six services in three slightly different spellings.

Deliberately not covered, because wrapping them would hide the thing that matters:

* multi-statement transactions, where two writes must commit together (the Order Service
  writes an order and its opening audit entry in one cursor)
* handlers that catch `UniqueViolation` *inside* the cursor to map a constraint to a 409

Those keep using `self._db.cursor(...)` directly. A base class that tried to express them
would need more parameters than the code it replaced.
"""

from logging import Logger

from common.postgres import PostgresPool


class Repository:
    """Base for a repository bound to one service's connection pool."""

    def __init__(self, db: PostgresPool, *, logger: Logger | None = None):
        self._db = db
        self._logger = logger

    def _row(self, row: dict | None) -> dict | None:
        """Hook applied to every row on its way out. Identity by default.

        An extension point for a repository that needs to convert a driver-native type
        (e.g. `Decimal`) to the plain type its schemas declare. Without this hook the base
        class would silently drop that conversion from every read.
        """
        return row

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        """Read a single row, or None."""
        with self._db.cursor() as cur:
            cur.execute(sql, params)
            return self._row(cur.fetchone())

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        """Read every matching row."""
        with self._db.cursor() as cur:
            cur.execute(sql, params)
            return [self._row(row) for row in cur.fetchall()]

    def write_one(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute a committing statement and return its RETURNING row, if any."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(sql, params)
            return self._row(cur.fetchone())
