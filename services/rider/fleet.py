"""Rider-facing helpers shared by more than one router.

Both live here rather than beside a single route because `own_profile` is used by four
routes across two routers, and `report_event` by two.
"""

from uuid import UUID

from common.auth import CurrentUser
from common.errors import forbidden, not_found


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


def report_event(order_service, rider: dict, order_id: UUID, signal: str) -> None:
    """Relay a delivery event for an order this rider is actually carrying.

    The ownership check is the whole security model for the two reporting endpoints: a rider
    may only move the order their own row says they hold, so one rider cannot mark another's
    delivery complete.
    """
    held = rider.get("current_order_id")
    if held is None or str(held) != str(order_id):
        raise forbidden(f"You are not carrying order {order_id}")
    order_service.signal(order_id, signal, {"rider_id": str(rider["id"])})
