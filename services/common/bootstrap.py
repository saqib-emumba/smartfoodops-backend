"""One place that starts a service: logging, then its own connection pool.

Six `main.py` files and the order worker opened these in the same order with the same
arguments. Nothing here knows about routes or domains — it is the first four lines of a
service, not a framework.
"""

from dataclasses import dataclass
from logging import Logger

from common.config import required
from common.logging_config import configure_logging
from common.postgres import DEFAULT_EXHAUSTED_DETAIL, PostgresPool


@dataclass(frozen=True)
class ServiceRuntime:
    """What every Postgres-backed process in this platform needs before it can serve."""

    name: str
    logger: Logger
    db: PostgresPool


def bootstrap(
    service_name: str, *, exhausted_detail: str = DEFAULT_EXHAUSTED_DETAIL
) -> ServiceRuntime:
    """Configure logging and build this service's pool, failing fast on a missing DSN.

    `exhausted_detail` is worded per service because the caller-visible consequence differs
    — the Order Service states that no order was created. Defaulted rather than required so
    the two services that never customised it keep their current 500 text exactly.
    """
    logger = configure_logging(service_name)
    db = PostgresPool(
        required("DATABASE_URL"), logger=logger, exhausted_detail=exhausted_detail
    )
    return ServiceRuntime(name=service_name, logger=logger, db=db)
