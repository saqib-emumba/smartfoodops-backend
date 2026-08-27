"""HTTP transport to sibling services.

Services must not touch each other's databases, so every cross-boundary read *and* write
goes over HTTP through this client. It exists to make the failure contract identical
everywhere:

    dependency unreachable     -> 503  (caller may retry)
    dependency returns 404     -> 404  (worded by the calling service)
    dependency returns 401/403 -> 403  (the forwarded caller may not have this)
    dependency returns 2xx     -> the envelope's `body`, or {} when there is no body
    anything else              -> 502  (we got an answer we cannot use)

The client carries the downstream service's display name so error text and log lines read
the same regardless of which service is calling.

Every response in the platform is enveloped as `{status, body, message, errors}` (D35), so
a successful call unwraps `body` here rather than handing the whole envelope to the caller
— the one place this happens, so no client above this layer has to know the envelope exists.
"""

from logging import Logger
from typing import Callable, ClassVar

import httpx
from fastapi import HTTPException, status

from common.config import HTTP_TIMEOUT, service_url
from common.errors import bad_gateway, forbidden, not_found, service_unavailable

# Builds the exception raised when the downstream answers 404. Callers that reference an
# entity rather than fetch it override this — see the Order Service, where an unknown
# customer is a 422 because the order, not the customer, is what the client asked for.
MissingError = Callable[[str], HTTPException]


class ServiceClient:
    """Client for exactly one downstream service."""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        logger: Logger,
        timeout: float = HTTP_TIMEOUT,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._logger = logger
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _unreachable(self, url: str, exc: Exception, hint: str):
        self._logger.error("%s unreachable at %s: %s", self.name, url, exc)
        return service_unavailable(f"{self.name} is unreachable; {hint}")

    def _message(self, response: httpx.Response) -> str:
        """The downstream's own envelope `message`, or its raw text if it has none."""
        try:
            decoded = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(decoded, dict) and "message" in decoded:
            return decoded["message"]
        return response.text[:200]

    def _payload(
        self,
        response: httpx.Response,
        *,
        missing: str,
        missing_error: MissingError,
        bad_gateway_hint: str | None,
        passthrough: dict[int, MissingError] | None = None,
    ) -> dict:
        """Map the downstream status code onto this service's error contract.

        `passthrough` names status codes that mean something more specific than "the
        dependency broke" — a full kitchen answering `409`, say — and would otherwise fall
        into the generic `502` below. Each maps to a factory called with the downstream's
        own `message`, so the caller's meaning survives the hop rather than being flattened
        into "unexpected response". Empty by default: most callers have nothing to add here.
        """
        if response.status_code == status.HTTP_404_NOT_FOUND:
            raise missing_error(missing)
        # An auth refusal downstream is a decision, not a malfunction, so it passes through
        # instead of becoming a 502. Since we call as the original caller, "the dependency
        # refused us" means "the caller may not have this" — which is a 403 to them.
        if response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ):
            self._logger.info(
                "%s refused the forwarded credentials (%s)",
                self.name,
                response.status_code,
            )
            raise forbidden(f"Not authorised to access this resource in the {self.name}")
        if passthrough and response.status_code in passthrough:
            raise passthrough[response.status_code](self._message(response))
        # Any 2xx is success. This was originally `!= 200`, which was true while every
        # cross-service call was a GET; the moment writes came through here it turned a
        # `201` from a creating endpoint — and a `202` from the signal relay — into a `502`
        # reported to a caller whose request had in fact succeeded. Matching the class
        # rather than enumerating members is what stops that recurring with the next code.
        if not 200 <= response.status_code < 300:
            self._logger.error(
                "Unexpected %s response %s: %s",
                self.name,
                response.status_code,
                response.text[:200],
            )
            suffix = f" while {bad_gateway_hint}" if bad_gateway_hint else ""
            raise bad_gateway(f"Unexpected response from {self.name}{suffix}")
        # 204 has no body to decode. Nothing returns one through this client today, but a
        # signal relay is an obvious future caller and `.json()` would raise on it.
        if response.status_code == status.HTTP_204_NO_CONTENT or not response.content:
            return {}
        decoded = response.json()
        # Unwrap the envelope. `decoded["body"]` is `None` for the handful of routes with
        # nothing to return, and callers already treat a missing key as "nothing here" via
        # `.get(...)`, so normalise both to `{}` rather than handing back `None` itself.
        if isinstance(decoded, dict) and "body" in decoded:
            return decoded["body"] or {}
        # Not yet enveloped — a rolling deploy has this service ahead of the one it just
        # called. Falls back to the raw payload rather than raising, so a stack mid-rollout
        # degrades to "reads the old shape" instead of "every cross-service call 502s".
        self._logger.warning(
            "%s response has no envelope 'body' key; treating it as unenveloped", self.name
        )
        return decoded

    def get(
        self,
        path: str,
        *,
        missing: str,
        unreachable_hint: str,
        bad_gateway_hint: str | None = None,
        missing_error: MissingError = not_found,
        headers: dict | None = None,
        passthrough: dict[int, MissingError] | None = None,
    ) -> dict:
        """Blocking GET returning the decoded JSON body.

        `missing` is the detail shown to our caller when the downstream answers 404, and
        `missing_error` the status it becomes; `unreachable_hint` completes the sentence
        "<Service> is unreachable; ...". `headers` carries the caller's credentials —
        see common.auth.bearer. `passthrough` — see `_payload`.
        """
        url = self._url(path)
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers=headers)
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc, unreachable_hint) from exc
        return self._payload(
            response,
            missing=missing,
            missing_error=missing_error,
            bad_gateway_hint=bad_gateway_hint,
            passthrough=passthrough,
        )

    async def aget(
        self,
        path: str,
        *,
        missing: str,
        unreachable_hint: str,
        bad_gateway_hint: str | None = None,
        missing_error: MissingError = not_found,
        headers: dict | None = None,
        passthrough: dict[int, MissingError] | None = None,
    ) -> dict:
        """Async counterpart to :meth:`get`, for services with async route handlers."""
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc, unreachable_hint) from exc
        return self._payload(
            response,
            missing=missing,
            missing_error=missing_error,
            bad_gateway_hint=bad_gateway_hint,
            passthrough=passthrough,
        )

    def post(
        self,
        path: str,
        *,
        json: dict,
        missing: str,
        unreachable_hint: str,
        bad_gateway_hint: str | None = None,
        missing_error: MissingError = not_found,
        headers: dict | None = None,
        passthrough: dict[int, MissingError] | None = None,
    ) -> dict:
        """Blocking POST returning the decoded JSON body.

        Added for the Week 2 saga, which is the first thing in the platform to *write*
        across a service boundary — until then every cross-boundary call was a lookup.

        Writes route through here rather than calling httpx directly so the mapping above
        stays the only place that decides what a failure looks like. An activity that
        hand-rolled its own client would be a second answer to "is a refused refund a 403
        or a 502?", and the two would drift. `passthrough` — see `_payload`.
        """
        url = self._url(path)
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, json=json, headers=headers)
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc, unreachable_hint) from exc
        return self._payload(
            response,
            missing=missing,
            missing_error=missing_error,
            bad_gateway_hint=bad_gateway_hint,
            passthrough=passthrough,
        )

    async def apost(
        self,
        path: str,
        *,
        json: dict,
        missing: str,
        unreachable_hint: str,
        bad_gateway_hint: str | None = None,
        missing_error: MissingError = not_found,
        headers: dict | None = None,
        passthrough: dict[int, MissingError] | None = None,
    ) -> dict:
        """Async counterpart to :meth:`post`, for services with async route handlers.

        A prior revision of this module had one of these and deleted it as dead code —
        nothing called it. D36 gave it a real caller: `order/clients/orchestrator.py`
        starts and signals a saga from inside `checkout.py`'s and `kitchen.py`'s `async
        def` handlers, which already await other lookups on the same request and would
        block the event loop for the duration of a blocking `post()` otherwise.
        """
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=json, headers=headers)
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc, unreachable_hint) from exc
        return self._payload(
            response,
            missing=missing,
            missing_error=missing_error,
            bad_gateway_hint=bad_gateway_hint,
            passthrough=passthrough,
        )


class ServiceFacade:
    """Base for a service's view of one sibling: a named client at a configurable URL.

    Eight client classes across five services opened with the same three things — a
    module-level `os.getenv("X_SERVICE_URL", DEFAULT_X_SERVICE_URL)`, an `__init__` building
    one `ServiceClient`, and a `base_url` property for the health endpoint. Subclasses
    declare *which* sibling and add the calls they make.

    No route, message, or credential choice lives here. Which paths a sibling exposes and
    how a 404 against it should be worded belong to the service doing the calling — see
    `common/__init__.py` on why that line is where it is.
    """

    display_name: ClassVar[str]
    env_var: ClassVar[str]
    default_url: ClassVar[str]
    timeout: ClassVar[float] = HTTP_TIMEOUT

    def __init__(self, logger: Logger):
        self._logger = logger
        self._client = ServiceClient(
            self.display_name,
            service_url(self.env_var, self.default_url),
            logger=logger,
            timeout=self.timeout,
        )

    @property
    def base_url(self) -> str:
        """The resolved base URL, surfaced by every service's health endpoint."""
        return self._client.base_url
