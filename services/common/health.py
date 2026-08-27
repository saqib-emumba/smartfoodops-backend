"""The three keys every health probe in the platform answers.

A payload builder, not a route. The Order Service's probe is `async` because it awaits
Temporal, the other five are not; each owns its own path and its own status wording. Only
the common floor is shared, and each service passes whatever else it depends on.
"""

from typing import Protocol


class Probe(Protocol):
    """Anything that can answer a health check without raising."""

    def is_reachable(self) -> bool: ...


def health_payload(service_name: str, db: Probe, *, status: str, **extra) -> dict:
    """`{status, service, database_reachable}` plus this service's own dependencies.

    Typed against the probe protocol rather than `PostgresPool` so this module does not
    pull psycopg2 in behind a type annotation.
    """
    return {
        "status": status,
        "service": service_name,
        "database_reachable": db.is_reachable(),
        **extra,
    }
