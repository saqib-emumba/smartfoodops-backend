"""HTTP transport to sibling services.

Services must not read each other's databases, so every cross-boundary lookup goes over
HTTP through this client. It exists to make the failure contract identical everywhere:

    dependency unreachable  -> 503   (caller may retry)
    dependency returns 404  -> 404   (worded by the calling service)
    dependency returns 5xx  -> 502   (we got an answer we cannot use)

The client carries the downstream service's display name so error text and log lines read
the same regardless of which service is calling.
"""

from logging import Logger

import httpx
from fastapi import status

from common.config import HTTP_TIMEOUT
from common.errors import bad_gateway, not_found, service_unavailable


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
        self, response: httpx.Response, *, missing: str, bad_gateway_hint: str | None
    ) -> dict:
        """Map the downstream status code onto this service's error contract."""
        if response.status_code == status.HTTP_404_NOT_FOUND:
            raise not_found(missing)
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
    ) -> dict:
        """Blocking GET returning the decoded JSON body.

        `missing` is the 404 detail shown to our caller; `unreachable_hint` completes the
        sentence "<Service> is unreachable; ...".
        """
        url = self._url(path)
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url)
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc, unreachable_hint) from exc
        return self._payload(
            response, missing=missing, bad_gateway_hint=bad_gateway_hint
        )

    async def aget(
        self,
        path: str,
        *,
        missing: str,
        unreachable_hint: str,
        bad_gateway_hint: str | None = None,
    ) -> dict:
        """Async counterpart to :meth:`get`, for services with async route handlers."""
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url)
        except httpx.RequestError as exc:
            raise self._unreachable(url, exc, unreachable_hint) from exc
        return self._payload(
            response, missing=missing, bad_gateway_hint=bad_gateway_hint
        )

    def post_best_effort(self, path: str, payload: dict, *, purpose: str) -> bool:
        """POST that reports failures instead of raising them.

        For side effects that must not fail the caller's request — the primary work is
        already committed, so a rejected or undeliverable POST is logged and swallowed.
        Returns whether the downstream accepted it.
        """
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(self._url(path), json=payload)
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
