"""The one response envelope every route in the platform answers with.

`{"status": <int>, "body": <T | null>, "message": <str>, "errors": <list[str] | null>}` for
success and failure alike. Before this, a client had to branch on which of seven undeclared
body shapes an endpoint used, and a `422` from a Pydantic validation failure was structurally
different from a `422` raised by `common.errors.unprocessable` — a list versus a string under
the same `detail` key. Both problems are closed here rather than per-route: `ok()` wraps a
success body, and `install_error_handlers()` makes the three ways a request can fail — a
deliberate `HTTPException`, a Pydantic validation rejection, an unhandled exception — all
answer through the same shape.

Nothing here decides *when* to fail; that stays exactly where it was, in `common.errors` and
the domain code that raises. This module only decides what failing looks like on the wire —
consistent with `common/__init__.py`'s rule that infrastructure never touches domain meaning.
"""

import logging
from typing import Generic, TypeVar

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

T = TypeVar("T")

_logger = logging.getLogger("common.responses")

# Every route that answers 201 but replays an idempotent request with 200 documents both —
# see common/errors.py's module docstring for why the mapping itself lives per-service.
REPLAY_RESPONSE = {200: {"description": "Idempotent replay of an already-processed key"}}


class Envelope(BaseModel, Generic[T]):
    """The wire shape. `body` is `None` on every error and on the three 204-turned-200 routes
    that never had one; it is never omitted, so a client can always look for the key.
    """

    status: int
    body: T | None = None
    message: str = ""
    errors: list[str] | None = None


def ok(body: T = None, *, message: str = "", status: int = 200) -> "Envelope[T]":
    """Wrap a success body. `status` must match the route's declared `status_code` — nothing
    here enforces that; scripts/smoke-test.sh's `expect` helper does, for every route.

    Returns the unparameterised model rather than `Envelope[type(body)]`: FastAPI validates
    the return value against each route's own `response_model=Envelope[X]` before
    serialising, so the concrete type is bound there, once, not on every call here.
    """
    return Envelope(status=status, body=body, message=message, errors=None)


def _error_response(*, status_code: int, message: str, errors: list[str] | None, headers=None):
    payload = Envelope(status=status_code, body=None, message=message, errors=errors)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload, exclude_none=False),
        headers=headers,
    )


def _describe(error: dict) -> str:
    """One readable line per Pydantic validation error: `body.total_amount: msg`."""
    loc = ".".join(str(part) for part in error.get("loc", ()) if part != "body") or "body"
    return f"{loc}: {error.get('msg', 'invalid value')}"


def install_error_handlers(app: FastAPI) -> None:
    """Register the three handlers that make every failure answer through the envelope.

    Call once per service, right after constructing `app` — before any router is included,
    though registration order does not matter for exception handlers the way it does for
    routes.
    """

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        # Every `common.errors` factory becomes this. `exc.headers` is re-emitted rather
        # than dropped, which is what keeps the `WWW-Authenticate: Bearer` challenge on a
        # 401 — losing it here would be invisible to every test in this platform and break
        # only a client that actually honours the challenge.
        return _error_response(
            status_code=exc.status_code,
            message=str(exc.detail),
            errors=None,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default here is `{"detail": [...]}` — a list, where `unprocessable()`
        # produces a string. This is the one place that collision closes: `message` is
        # always a sentence, `errors` is always a list-or-null.
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message="Request validation failed",
            errors=[_describe(e) for e in exc.errors()],
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Starlette's own fallback answers plain-text `Internal Server Error` with no JSON
        # body at all — a shape a client's envelope-parsing code cannot even attempt. This
        # is what stands between that and a route that assumed a row could not be missing.
        _logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message="Internal server error",
            errors=None,
        )
