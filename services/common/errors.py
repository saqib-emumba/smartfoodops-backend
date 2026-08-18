"""HTTPException factories for the failure modes every service shares.

Centralising these keeps status-code choices consistent across service boundaries: a
missing resource is always 404, an unsatisfiable-but-well-formed request is always 422,
and a failing dependency is always 503 rather than a leaked 500.
"""

from fastapi import HTTPException, status


def bad_request(detail: str) -> HTTPException:
    """400 — the caller sent something malformed or unusable."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def unauthorized(detail: str) -> HTTPException:
    """401 — the caller presented no usable identity.

    Carries the WWW-Authenticate challenge the Bearer scheme requires, which is what
    separates this from 403: the caller may retry with a valid token.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden(detail: str) -> HTTPException:
    """403 — the caller is known but not authorised for this action."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def not_found(detail: str) -> HTTPException:
    """404 — the referenced resource does not exist."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def conflict(detail: str) -> HTTPException:
    """409 — the request collides with existing state (duplicates, races)."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def unprocessable(detail: str) -> HTTPException:
    """422 — well-formed, but rejected by a business rule or current state."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def internal_error(detail: str) -> HTTPException:
    """500 — this service could not do its own job (e.g. a starved pool)."""
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
    )


def bad_gateway(detail: str) -> HTTPException:
    """502 — a dependency answered, but with something we cannot use."""
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


def service_unavailable(detail: str) -> HTTPException:
    """503 — a dependency is unreachable; the caller may retry."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )
