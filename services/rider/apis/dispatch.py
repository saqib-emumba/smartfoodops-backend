"""Fleet routes for the order saga: claim a rider, and give one back.

Internal key only, never a bearer token. Which rider carries which order is not a decision
a customer may make, and the caller is a workflow rather than a user, so there is no token
to forward (D26).
"""

from fastapi import APIRouter, Depends

from common.auth import require_internal
from common.config import RIDER_DISPATCH_CANDIDATE_CAP, RIDER_MAX_DISTANCE_KM
from common.responses import Envelope, ok
from rider import deps
from rider.eta import eta_minutes
from rider.schemas.riders import (
    DispatchRequest,
    DispatchResponse,
    ReleaseRequest,
    ReleaseResponse,
)

router = APIRouter(prefix="/api/v1/riders", dependencies=[Depends(require_internal)])


@router.post("/dispatch", response_model=Envelope[DispatchResponse])
def dispatch_rider(payload: DispatchRequest) -> Envelope[DispatchResponse]:
    """Claim the nearest available rider for an order.

    Two steps as of D49: Redis (`deps.geo.nearby`) answers "nearest, regardless of busy or
    free" with a distance-ordered candidate list bounded by `RIDER_DISPATCH_CANDIDATE_CAP`;
    Postgres (`deps.riders.dispatch`) then claims the nearest *available* rider from within
    that list, transactionally. Availability is decided in exactly one place, same as before
    — Redis only ever contributes candidate identity and order.

    An empty fleet answers `200` with body `{"assigned": false, ...}` rather than an error
    status. The workflow treats those differently — no rider available means wait and ask
    again, while a `503` means the Rider Service itself is broken — and collapsing them into
    one status would make the saga retry the wrong thing.
    """
    max_km = payload.max_distance_km or RIDER_MAX_DISTANCE_KM
    candidates = deps.geo.nearby(
        payload.restaurant_latitude,
        payload.restaurant_longitude,
        max_km,
        RIDER_DISPATCH_CANDIDATE_CAP,
    )
    distance_by_user_id = dict(candidates)
    claimed = deps.riders.dispatch(payload.order_id, [user_id for user_id, _ in candidates])

    if claimed is None:
        deps.logger.info(
            "No rider within %skm of (%s, %s) for order %s",
            max_km,
            payload.restaurant_latitude,
            payload.restaurant_longitude,
            payload.order_id,
        )
        return ok(
            DispatchResponse(
                assigned=False,
                order_id=payload.order_id,
                reason="no_rider_in_range",
            ),
            message="No rider in range",
        )

    # None for a rider claimed on a retry via the already-held branch: Redis was never
    # consulted for that path, so there is no candidate distance to look up.
    distance = distance_by_user_id.get(str(claimed["user_id"]))
    deps.logger.info(
        "Assigned rider %s to order %s (%.2fkm)",
        claimed["id"],
        payload.order_id,
        distance if distance is not None else -1.0,
    )
    return ok(
        DispatchResponse(
            assigned=True,
            order_id=payload.order_id,
            rider_id=claimed["id"],
            user_id=claimed["user_id"],
            distance_km=round(distance, 2) if distance is not None else None,
            eta_minutes=eta_minutes(distance),
        ),
        message="Rider assigned",
    )


@router.post("/release", response_model=Envelope[ReleaseResponse])
def release_rider(payload: ReleaseRequest) -> Envelope[ReleaseResponse]:
    """Return whichever rider holds this order to the available pool.

    The saga's compensating action, and idempotent by design: nothing holding the order is
    success, not a `404`. A compensation that failed because it had already succeeded would
    be retried until the workflow gave up, which is the opposite of what a rollback needs.
    """
    released = deps.riders.release(payload.order_id)
    if released is None:
        deps.logger.info(
            "No rider held order %s; release is a no-op", payload.order_id
        )
        return ok(
            ReleaseResponse(released=False, order_id=payload.order_id),
            message="Nothing to release",
        )

    deps.logger.info(
        "Released rider %s from order %s", released["id"], payload.order_id
    )
    return ok(
        ReleaseResponse(released=True, order_id=payload.order_id, rider_id=released["id"]),
        message="Rider released",
    )
