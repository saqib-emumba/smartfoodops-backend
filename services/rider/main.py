"""SmartFoodOps Rider Service — the delivery fleet and proximity dispatch (Port 8006).

Owns the `riders` table in its own PostgreSQL database. That table shipped inside
`sfo_user_core` in Week 1 with no code behind it; Week 2 gave it a service that writes
availability and location on every assignment, and under D01 a service may not write another
service's tables — so it moved here, and its foreign key to `users` became a plain UUID
verified over HTTP (D02, D28).

Two kinds of caller reach this service. Riders themselves, on a bearer token, manage their
own profile and report pickups and deliveries. The order saga, on the internal key, claims
and releases them. No end user may reach `/dispatch` or `/release`: which rider carries which
order is the fleet's business, not a customer's.

Authorisation for a pickup or delivery is settled entirely here, from `current_order_id` on
the rider's own row, rather than by asking the Order Service who was assigned. One place
decides, so there is no second place to drift (D16).
"""

import os
from uuid import UUID

from fastapi import Depends, FastAPI, status

from clients import OrderServiceClient, UserServiceClient
from common.auth import Principal, require_internal, require_role
from common.config import RIDER_MAX_DISTANCE_KM, required
from common.errors import conflict, forbidden, not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import RiderRepository
from schemas import (
    DispatchRequest,
    DispatchResponse,
    ReleaseRequest,
    ReleaseResponse,
    RiderAvailabilityRequest,
    RiderLocationRequest,
    RiderRegisterRequest,
    RiderResponse,
)

SERVICE_NAME = "rider-service"
DATABASE_URL = required("DATABASE_URL")

# Assumed courier speed for the ETA below. A constant rather than anything measured: this
# is a mock estimate, and naming it here keeps that honest instead of burying 30 in a
# formula where it would read like a fact.
AVERAGE_COURIER_SPEED_KMH = 30.0
KITCHEN_PREP_PADDING_MINUTES = 5
MINIMUM_ETA_MINUTES = 5

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(
    DATABASE_URL,
    logger=logger,
    exhausted_detail="Database connection pool exhausted; no rider was dispatched",
)
riders = RiderRepository(db, logger=logger)
user_service = UserServiceClient(logger)
order_service = OrderServiceClient(logger)

app = FastAPI(title="SmartFoodOps Rider Service", lifespan=db.lifespan)


def _eta_minutes(distance_km: float | None) -> int:
    """Rough arrival estimate from the dispatch distance."""
    if distance_km is None:
        return MINIMUM_ETA_MINUTES
    travel = (distance_km / AVERAGE_COURIER_SPEED_KMH) * 60
    return max(MINIMUM_ETA_MINUTES, int(travel) + KITCHEN_PREP_PADDING_MINUTES)


def _own_profile(principal: Principal) -> dict:
    """The calling rider's own row, or 404.

    Every rider-facing endpoint below starts here, which is what makes `user_id` from the
    token the only way to address a profile — there is no path that takes a rider id from
    the caller.
    """
    rider = riders.find_by_user(principal.user_id)
    if rider is None:
        raise not_found(
            "You have no rider profile; register one with POST /api/v1/riders first"
        )
    return rider


def _report(rider: dict, order_id: UUID, signal: str) -> None:
    """Relay a delivery event for an order this rider is actually carrying.

    The ownership check is the whole security model for these two endpoints: a rider may
    only move the order their own row says they hold, so one rider cannot mark another's
    delivery complete.
    """
    held = rider.get("current_order_id")
    if held is None or str(held) != str(order_id):
        raise forbidden(f"You are not carrying order {order_id}")
    order_service.signal(order_id, signal, {"rider_id": str(rider["id"])})


@app.get("/api/v1/riders/health")
def health():
    return {
        "status": "Rider Service is operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        "user_service_url": user_service.base_url,
        "order_service_url": order_service.base_url,
    }


@app.post(
    "/api/v1/riders",
    response_model=RiderResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_rider(
    payload: RiderRegisterRequest,
    principal: Principal = Depends(require_role("rider")),
) -> RiderResponse:
    """Enrol the calling account into the delivery fleet.

    The rider is the token's subject. There is no way to enrol anybody else — `user_id` is
    not a field a client can send (D13).
    """
    user_service.verify_rider(principal.user_id, principal.token)
    return RiderResponse(**riders.register(payload, principal.user_id))


@app.get("/api/v1/riders/me", response_model=RiderResponse)
def get_own_profile(
    principal: Principal = Depends(require_role("rider")),
) -> RiderResponse:
    return RiderResponse(**_own_profile(principal))


@app.patch("/api/v1/riders/me/location", response_model=RiderResponse)
def update_location(
    payload: RiderLocationRequest,
    principal: Principal = Depends(require_role("rider")),
) -> RiderResponse:
    """Report the rider's current position.

    Until a rider has reported one they are invisible to dispatch: the partial index that
    backs the search excludes rows with a null coordinate, because a rider whose location
    is unknown cannot be measured against a restaurant.
    """
    updated = riders.update_location(
        principal.user_id, payload.current_latitude, payload.current_longitude
    )
    if updated is None:
        raise not_found(
            "You have no rider profile; register one with POST /api/v1/riders first"
        )
    return RiderResponse(**updated)


@app.patch("/api/v1/riders/me/availability", response_model=RiderResponse)
def set_availability(
    payload: RiderAvailabilityRequest,
    principal: Principal = Depends(require_role("rider")),
) -> RiderResponse:
    """Go on or off shift.

    Refused while an order is in hand: a rider cannot go off shift holding somebody's
    dinner. The saga is what releases them, on delivery or on compensation — which is also
    why this returning nothing has two possible causes, separated below.
    """
    updated = riders.set_availability(principal.user_id, payload.is_available)
    if updated is not None:
        return RiderResponse(**updated)

    # The UPDATE matched no row. Either there is no profile, or there is one mid-delivery.
    # Distinguishing them costs one read and is the difference between "register first"
    # and "finish your delivery first".
    rider = _own_profile(principal)
    raise conflict(
        f"You are carrying order {rider['current_order_id']}; "
        "complete or cancel the delivery before changing availability"
    )


@app.post("/api/v1/riders/me/orders/{order_id}/picked-up", status_code=status.HTTP_204_NO_CONTENT)
def mark_picked_up(
    order_id: UUID,
    principal: Principal = Depends(require_role("rider")),
) -> None:
    """Report collecting an order from the kitchen."""
    _report(_own_profile(principal), order_id, "rider_pickup")


@app.post("/api/v1/riders/me/orders/{order_id}/delivered", status_code=status.HTTP_204_NO_CONTENT)
def mark_delivered(
    order_id: UUID,
    principal: Principal = Depends(require_role("rider")),
) -> None:
    """Report handing an order to the customer.

    The rider is *not* released here. The saga releases them, in the same step that records
    the `delivered` transition, so availability and order state can never disagree — and so
    a delivery reported for a workflow that has already been cancelled does not hand a rider
    back twice.
    """
    _report(_own_profile(principal), order_id, "rider_delivery")


@app.post(
    "/api/v1/riders/dispatch",
    response_model=DispatchResponse,
    dependencies=[Depends(require_internal)],
)
def dispatch_rider(payload: DispatchRequest) -> DispatchResponse:
    """Claim the nearest available rider for an order.

    Service-to-service only, on the internal key: which rider carries which order is not a
    decision a customer may make, and the caller is a workflow rather than a user, so there
    is no bearer token to forward (D26).

    An empty fleet answers `200 {"assigned": false}` rather than an error status. The
    workflow treats those differently — no rider available means wait and ask again, while a
    `503` means the Rider Service itself is broken — and collapsing them into one status
    would make the saga retry the wrong thing.
    """
    max_km = payload.max_distance_km or RIDER_MAX_DISTANCE_KM
    claimed = riders.dispatch(
        payload.order_id,
        payload.restaurant_latitude,
        payload.restaurant_longitude,
        max_km,
    )

    if claimed is None:
        logger.info(
            "No rider within %skm of (%s, %s) for order %s",
            max_km,
            payload.restaurant_latitude,
            payload.restaurant_longitude,
            payload.order_id,
        )
        return DispatchResponse(
            assigned=False,
            order_id=payload.order_id,
            reason="no_rider_in_range",
        )

    distance = claimed.get("distance_km")
    logger.info(
        "Assigned rider %s to order %s (%.2fkm)",
        claimed["id"],
        payload.order_id,
        distance if distance is not None else -1.0,
    )
    return DispatchResponse(
        assigned=True,
        order_id=payload.order_id,
        rider_id=claimed["id"],
        user_id=claimed["user_id"],
        distance_km=round(distance, 2) if distance is not None else None,
        eta_minutes=_eta_minutes(distance),
    )


@app.post(
    "/api/v1/riders/release",
    response_model=ReleaseResponse,
    dependencies=[Depends(require_internal)],
)
def release_rider(payload: ReleaseRequest) -> ReleaseResponse:
    """Return whichever rider holds this order to the available pool.

    The saga's compensating action, and idempotent by design: nothing holding the order is
    success, not a `404`. A compensation that failed because it had already succeeded would
    be retried until the workflow gave up, which is the opposite of what a rollback needs.
    """
    released = riders.release(payload.order_id)
    if released is None:
        logger.info("No rider held order %s; release is a no-op", payload.order_id)
        return ReleaseResponse(released=False, order_id=payload.order_id)

    logger.info("Released rider %s from order %s", released["id"], payload.order_id)
    return ReleaseResponse(
        released=True, order_id=payload.order_id, rider_id=released["id"]
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8006")))
