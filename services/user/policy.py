"""The platform's policy decision point (D57).

Every gated request in SmartFoodOps passes through here exactly once: nginx's `auth_request`
calls apis/internal.py, which verifies the token's signature and then asks this module
whether the caller may have what they asked for. A denial becomes a 403 at the gateway and
the backend service never sees the request.

Two things make that affordable. The policy is reference data of a few dozen rows, so it is
loaded whole into an immutable snapshot rather than queried per request; and the snapshot is
rebuilt at most once per `POLICY_CACHE_TTL_SECONDS`, so the database round trip happens on a
timer rather than on a request. Steady state is a regex match and a set intersection.

Three deliberate behaviours, each of which would be a security or availability bug inverted:

  fail closed     an unmatched (method, path) is refused. Adding a gated route without a
                  `route_permissions` row is a visible 403, never a silently open endpoint.
  fail open on
  refresh error   a snapshot that cannot be *rebuilt* keeps serving the last good one. The
                  policy is not more correct for being unavailable, and flipping the whole
                  platform to 403 over a transient database blip is the worse outcome.
  refuse to boot  an empty policy is a startup failure (`load_initial`), because fail-closed
                  matching against zero rows would 403 every request in the platform. A
                  missing policy is misconfiguration, and it fails like misconfiguration.
"""

import re
import threading
import time
from dataclasses import dataclass

from common.auth import ADMIN_ROLE
from common.config import POLICY_CACHE_TTL_SECONDS
from common.errors import forbidden
from user import deps


@dataclass(frozen=True)
class _Route:
    """One `route_permissions` row, with its path pattern compiled."""

    method: str
    path_pattern: str
    pattern: re.Pattern
    permission: str | None
    # Count of segments that are literal rather than `{param}`. The sort key for match
    # order: `/api/v1/orders/kitchen/{restaurant_id}` must be tried before
    # `/api/v1/orders/{order_id}/accept` would be, so a more specific row always wins over a
    # more general one that also matches. Without this, match order would depend on the
    # order Postgres happened to return rows in.
    specificity: int


@dataclass(frozen=True)
class PolicySnapshot:
    """An immutable read of the three policy tables, valid as of `loaded_at`."""

    routes: tuple[_Route, ...]
    role_permissions: dict[str, frozenset[str]]
    loaded_at: float

    def permissions_for(self, roles: list[str]) -> frozenset[str]:
        """Union of the permissions granted to every role the caller holds.

        Union rather than intersection: holding two roles means being able to do what either
        can, which is the whole point of the multi-role model (D48, §3.3).
        """
        granted: frozenset[str] = frozenset()
        for role in roles:
            granted |= self.role_permissions.get(role, frozenset())
        return granted

    def match(self, method: str, path: str) -> _Route | None:
        """The most specific route matching this request, or None if nothing does."""
        for route in self.routes:
            if route.method == method and route.pattern.match(path):
                return route
        return None


def _compile(path_pattern: str) -> re.Pattern:
    """Turn `/api/v1/orders/{order_id}/accept` into `^/api/v1/orders/[^/]+/accept$`.

    `[^/]+` rather than `.+` so a parameter can never swallow a path separator — otherwise
    `/api/v1/orders/{order_id}` would match `/api/v1/orders/anything/at/all` and inherit that
    sub-tree's authorization by accident.
    """
    segments = [
        "[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment)
        for segment in path_pattern.split("/")
    ]
    return re.compile("^" + "/".join(segments) + "$")


def _specificity(path_pattern: str) -> int:
    return sum(
        0 if segment.startswith("{") and segment.endswith("}") else 1
        for segment in path_pattern.split("/")
    )


def _normalise(path: str) -> str:
    """Drop one trailing slash so `/api/v1/orders/` is the route `/api/v1/orders`.

    nginx hands over `$uri`, which is already decoded and collapsed, so this is the only
    normalisation left. Without it a trailing slash would miss every pattern and fail
    closed — a 403 where FastAPI would have answered with its usual redirect.
    """
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _build(routes: list[dict], role_permissions: dict[str, frozenset[str]]) -> PolicySnapshot:
    compiled = [
        _Route(
            method=row["method"].upper(),
            path_pattern=row["path_pattern"],
            pattern=_compile(row["path_pattern"]),
            permission=row["permission"],
            specificity=_specificity(row["path_pattern"]),
        )
        for row in routes
    ]
    # Most specific first, then longest, so `match` can take the first hit and stop.
    compiled.sort(key=lambda route: (-route.specificity, -len(route.path_pattern)))
    return PolicySnapshot(
        routes=tuple(compiled),
        role_permissions=role_permissions,
        loaded_at=time.monotonic(),
    )


_snapshot: PolicySnapshot | None = None
# Handlers here are synchronous `def`, so FastAPI runs them in a threadpool and two requests
# can find the snapshot stale at the same moment. The lock makes that one reload instead of
# N, and the second check inside it is what stops the queued threads redoing the work.
_lock = threading.Lock()


def _load() -> PolicySnapshot:
    return _build(deps.policy.load_routes(), deps.policy.load_role_permissions())


def load_initial() -> PolicySnapshot:
    """Load the first snapshot, refusing to return an empty policy. Called at startup.

    An empty `route_permissions` under fail-closed matching would refuse every gated request
    in the platform, so this is a boot failure in the same spirit as `common.config.required`
    — the service does not come up half-configured and start denying traffic.
    """
    global _snapshot
    snapshot = _load()
    if not snapshot.routes:
        raise RuntimeError(
            "route_permissions is empty — the authorization policy has not been seeded. "
            "Apply db/user/add_rbac_permissions.sql to this database (D57); every gated "
            "request in the platform would otherwise be refused."
        )
    with _lock:
        _snapshot = snapshot
    deps.logger.info(
        "Authorization policy loaded: %d routes, %d roles, TTL %ds",
        len(snapshot.routes),
        len(snapshot.role_permissions),
        POLICY_CACHE_TTL_SECONDS,
    )
    return snapshot


def current() -> PolicySnapshot:
    """The snapshot, rebuilt first if it has outlived its TTL.

    A failed rebuild is logged and the previous snapshot is returned — see the module
    docstring on why a stale policy beats an unavailable one.
    """
    global _snapshot

    snapshot = _snapshot
    if snapshot is None:  # pragma: no cover - startup guarantees this
        return load_initial()

    if time.monotonic() - snapshot.loaded_at < POLICY_CACHE_TTL_SECONDS:
        return snapshot

    with _lock:
        # Re-check inside the lock: the thread that held it may already have refreshed.
        if time.monotonic() - _snapshot.loaded_at < POLICY_CACHE_TTL_SECONDS:
            return _snapshot
        try:
            _snapshot = _load()
        except Exception:  # noqa: BLE001 - any failure here must not deny traffic
            deps.logger.warning(
                "Authorization policy refresh failed; continuing on the snapshot loaded "
                "%.0fs ago",
                time.monotonic() - _snapshot.loaded_at,
                exc_info=True,
            )
        return _snapshot


def resolve(method: str, path: str, roles: list[str]) -> frozenset[str]:
    """Authorize a request and return the caller's full permission set.

    Raises 403 if the caller may not have this route. Returns the whole granted set rather
    than just the matched permission, because the gateway forwards it to the backend service
    as `X-User-Permissions` — one resolution serves both the decision and the services'
    own second check.
    """
    snapshot = current()
    granted = snapshot.permissions_for(roles)
    is_admin = ADMIN_ROLE in roles

    route = snapshot.match(method.upper(), _normalise(path))
    if route is None:
        # Fail closed, and deliberately *before* the admin bypass below — an unmatched path
        # refuses `system_admin` too. D48 §3.2 has `system_admin` bypassing every gate it
        # meets, but a missing `route_permissions` row is not a gate, it is an unconfigured
        # route. Letting an operator through would make the omission work for exactly the
        # person most likely to be testing the new endpoint, so the one guarantee this
        # whole design rests on — "a gated route with no row is visibly refused" — would
        # hold for everyone except the people who would notice. The cost is real and
        # accepted: ship a route without its row and the operator sees a 403 until the row
        # lands.
        #
        # Not a 404 either: saying "this path is not in the policy" would map the platform's
        # routes for anyone who asks, and the caller cannot act on the distinction anyway.
        raise forbidden("You do not hold the permission required for this action")

    if route.permission is None or route.permission in granted or is_admin:
        return granted

    raise forbidden("You do not hold the permission required for this action")
