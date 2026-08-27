"""Rider-facing profile routes: enrol, read, move, go on or off shift.

The health probe lives in health.py instead. All four routes here address the caller's own
row via the token's subject — there is no path that takes a rider id from the client (D13).
"""

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, require_role
from common.errors import conflict, not_found
from common.responses import Envelope, ok
from rider import deps
from rider.fleet import own_profile
from rider.schemas.riders import (
    RiderAvailabilityRequest,
    RiderLocationRequest,
    RiderRegisterRequest,
    RiderResponse,
)

router = APIRouter(prefix="/api/v1/riders")


@router.post("", response_model=Envelope[RiderResponse], status_code=status.HTTP_201_CREATED)
def register_rider(
    payload: RiderRegisterRequest,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> Envelope[RiderResponse]:
    """Enrol the calling account into the delivery fleet.

    The rider is the token's subject. There is no way to enrol anybody else — `user_id` is
    not a field a client can send (D13).
    """
    deps.user_service.verify_rider(current_user.user_id, current_user.token)
    row = deps.riders.register(payload, current_user.user_id)
    return ok(RiderResponse(**row), message="Rider enrolled", status=201)


@router.get("/me", response_model=Envelope[RiderResponse])
def get_own_profile(
    current_user: CurrentUser = Depends(require_role("rider")),
) -> Envelope[RiderResponse]:
    return ok(RiderResponse(**own_profile(deps.riders, current_user)), message="Rider found")


@router.patch("/me/location", response_model=Envelope[RiderResponse])
def update_location(
    payload: RiderLocationRequest,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> Envelope[RiderResponse]:
    """Report the rider's current position.

    Until a rider has reported one they are invisible to dispatch: the partial index that
    backs the search excludes rows with a null coordinate, because a rider whose location
    is unknown cannot be measured against a restaurant.
    """
    updated = deps.riders.update_location(
        current_user.user_id, payload.current_latitude, payload.current_longitude
    )
    if updated is None:
        raise not_found(
            "You have no rider profile; register one with POST /api/v1/riders first"
        )
    return ok(RiderResponse(**updated), message="Location updated")


@router.patch("/me/availability", response_model=Envelope[RiderResponse])
def set_availability(
    payload: RiderAvailabilityRequest,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> Envelope[RiderResponse]:
    """Go on or off shift.

    Refused while an order is in hand: a rider cannot go off shift holding somebody's
    dinner. The saga is what releases them, on delivery or on compensation — which is also
    why this returning nothing has two possible causes, separated below.
    """
    updated = deps.riders.set_availability(current_user.user_id, payload.is_available)
    if updated is not None:
        return ok(RiderResponse(**updated), message="Availability updated")

    # The UPDATE matched no row. Either there is no profile, or there is one mid-delivery.
    # Distinguishing them costs one read and is the difference between "register first"
    # and "finish your delivery first".
    rider = own_profile(deps.riders, current_user)
    raise conflict(
        f"You are carrying order {rider['current_order_id']}; "
        "complete or cancel the delivery before changing availability"
    )
