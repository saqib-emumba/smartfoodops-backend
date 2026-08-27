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
    """What a process in this platform needs before it can serve.

    `db` is `None` for the one service with no database of its own — the orchestrator's API
    side (D36) starts and signals sagas over HTTP and holds no state locally, so it has
    nothing for a `DATABASE_URL` to name.
    """

    name: str
    logger: Logger
    db: PostgresPool | None


def bootstrap(
    service_name: str, *, exhausted_detail: str = DEFAULT_EXHAUSTED_DETAIL, db: bool = True
) -> ServiceRuntime:
    """Configure logging and, unless `db=False`, build this service's pool — failing fast
    on a missing DSN.

    `exhausted_detail` is worded per service because the caller-visible consequence differs
    — the Order Service states that no order was created. Defaulted rather than required so
    the services that never customised it keep their current 500 text exactly.

    `db=False` skips `required("DATABASE_URL")` entirely, so a stateless service never even
    checks for a DSN it was never given one to satisfy.
    """
    logger = configure_logging(service_name)
    pool = (
        PostgresPool(required("DATABASE_URL"), logger=logger, exhausted_detail=exhausted_detail)
        if db
        else None
    )
    return ServiceRuntime(name=service_name, logger=logger, db=pool)
