"""The floor every health probe in the platform answers with.

A payload builder, not a route. Each service still owns its own probe wording and its own
dependency checks; only `service` and `database_reachable` are guaranteed. The Order
Service's route stays `async` because it awaits Temporal — that asymmetry lives in the
route, not here.

The human-readable sentence each service used to put in an inner `status` field ("Payment
Service is operational") now travels as the envelope's `message` instead (see
common/responses.py) — carrying it here too would put the same sentence in the response
twice, under two different keys.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Probe(Protocol):
    """Anything that can answer a health check without raising."""

    def is_reachable(self) -> bool: ...


class HealthResponse(BaseModel):
    """`service` is guaranteed; `database_reachable` is guaranteed for every service that
    has one, which is every service but the orchestrator's stateless API side (D36).
    Everything else is per-service — `temporal_reachable`, `cache_reachable`,
    `*_service_url`, and so on. `extra="allow"` rather than declaring every service's
    fields here, which would make this module know about Temporal, Redis and every sibling
    URL — exactly the domain knowledge common/__init__.py says does not belong in this
    package.
    """

    model_config = ConfigDict(extra="allow")

    service: str
    database_reachable: bool | None = None


def health_payload(service_name: str, db: Probe | None, **extra) -> dict:
    """`{service, database_reachable}` plus this service's own dependencies.

    Typed against the probe protocol rather than `PostgresPool` so this module does not
    pull psycopg2 in behind a type annotation.

    `db=None` for the one stateless service in the platform — the orchestrator's API side
    (D36) — and `database_reachable` is omitted rather than reported `False`: there is no
    pool to be unreachable, and `False` would read as a degraded service rather than one
    with nothing of the kind to check.
    """
    payload = {"service": service_name}
    if db is not None:
        payload["database_reachable"] = db.is_reachable()
    payload.update(extra)
    return payload
