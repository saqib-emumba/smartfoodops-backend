"""Rider-facing helpers shared by more than one router.

Both live here rather than beside a single route because `own_profile` is used by four
routes across two routers, and `report_event` by two.
"""

from uuid import UUID

from common.auth import CurrentUser
from common.errors import forbidden, not_found
from common.temporal import workflow_id_for


def own_profile(riders, current_user: CurrentUser) -> dict:
    """The calling rider's own row, or 404.

    Every rider-facing endpoint starts here, which is what makes `user_id` from the token
    the only way to address a profile — there is no path that takes a rider id from the
    caller.
    """
    rider = riders.find_by_user(current_user.user_id)
    if rider is None:
        raise not_found(
            "You have no rider profile; register one with POST /api/v1/riders first"
        )
    return rider


def with_location(row: dict, geo) -> dict:
    """Merge a rider's Postgres row with its Redis-resident coordinates (D49) before
    serialisation into RiderResponse.

    Fails soft: a cache miss or a Redis outage here returns the rider profile with a null
    location rather than a 404/503 — the schema already treats "no reported location" as
    legitimate, so a lookup failure degrades to that same, already-modelled state.
    """
    location = geo.get_location(row["user_id"])
    lat, lon = location if location is not None else (None, None)
    return {**row, "current_latitude": lat, "current_longitude": lon}


async def report_event(
    order_service, saga, rider: dict, order_id: UUID, *, stage: str, signal: str
) -> None:
    """Record a delivery event for an order this rider is carrying, then tell the saga.

    The ownership check is the whole security model for the two reporting endpoints: a rider
    may only move the order their own row says they hold, so one rider cannot mark another's
    delivery complete.

    **Record first, signal second, and never the other way round.** The record is what makes
    a lost signal survivable: `orders.rider_reported_stage` is read back by the saga's
    `read_rider_report_activity` when the delivery timer expires, which is how a timeout
    tells "the rider genuinely never reported" apart from "they reported and the signal
    never landed" (D43 — much of the Week 3 code cites this as "D46", a miscitation; the
    record is D43). Signalling first and then failing to record leaves a timeout
    with nothing to find, and the saga refunds a delivered order an hour later.

    Both calls raise rather than being swallowed, which is the opposite of how
    `order/clients/orchestrator.py` treats the kitchen's decision — and deliberately so. A
    kitchen decision is committed by the time its signal is attempted, and the admin who
    made it must not see an error for something that worked. A rider is mid-report: they are
    entitled to know whether it landed, and a failure they can see is a failure they can
    retry. Both halves are idempotent, so retrying is safe — the report's guard is
    forward-only and a duplicate signal for a stage already recorded is a no-op in the
    workflow.

    Until D47 this function did neither of these directly: it called
    `order_service.signal(...)` and let the Order Service both record and relay. This
    service now holds its own Temporal client, so the two halves are visible here, in the
    order that matters.
    """
    held = rider.get("current_order_id")
    if held is None or str(held) != str(order_id):
        raise forbidden(f"You are not carrying order {order_id}")

    await order_service.record_rider_report(order_id, stage)
    await saga.signal(workflow_id_for(order_id), signal, {"rider_id": str(rider["id"])})
