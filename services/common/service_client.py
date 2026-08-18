"""HTTP transport to sibling services.

Services must not read each other's databases, so every cross-boundary lookup goes over
HTTP through this client. It exists to make the failure contract identical everywhere:

    dependency unreachable    -> 503   (caller may retry)
    dependency returns 404    -> 404   (worded by the calling service)
    dependency returns 401/403 -> 403  (the forwarded caller may not have this)
    dependency returns 5xx    -> 502   (we got an answer we cannot use)

The client carries the downstream service's display name so error text and log lines read
the same regardless of which service is calling.
"""

from logging import Logger
from typing import Callable

import httpx
from fastapi import HTTPException, status

from common.config import HTTP_TIMEOUT
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

    def _payload(
        self,
        response: httpx.Response,
        *,
        missing: str,
        missing_error: MissingError,
        bad_gateway_hint: str | None,
    ) -> dict:
        """Map the downstream status code onto this service's error contract."""
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
        if response.status_code != status.HTTP_200_OK:
            self._logger.error(
                "Unexpected %s response %s: %s",
                self.name,
                response.status_code,
                response.text[:200],
            )
            suffix = f" while {bad_gateway_hint}" if bad_gateway_hint else ""
            raise bad_gateway(f"Unexpected response from {self.name}{suffix}")
        return response.json()

    def get(
        self,
        path: str,
        *,
        missing: str,
        unreachable_hint: str,
        bad_gateway_hint: str | None = None,
        missing_error: MissingError = not_found,
        headers: dict | None = None,
    ) -> dict:
        """Blocking GET returning the decoded JSON body.

        `missing` is the detail shown to our caller when the downstream answers 404, and
        `missing_error` the status it becomes; `unreachable_hint` completes the sentence
        "<Service> is unreachable; ...". `headers` carries the caller's credentials —
        see common.auth.bearer.
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
        )

    def post_best_effort(
        self, path: str, payload: dict, *, purpose: str, headers: dict | None = None
    ) -> bool:
        """POST that reports failures instead of raising them.

        For side effects that must not fail the caller's request — the primary work is
        already committed, so a rejected or undeliverable POST is logged and swallowed.
        Returns whether the downstream accepted it.
        """
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(self._url(path), json=payload, headers=headers)
        except httpx.RequestError as exc:
            self._logger.error("Could not deliver %s to %s: %s", purpose, self.name, exc)
            return False
        if response.status_code >= 400:
            self._logger.error(
                "%s rejected %s (%s): %s",
                self.name,
                purpose,
                response.status_code,
                response.text[:200],
            )
            return False
        return True
