"""Activities for `RiderWorkflow` (D55, the mentor's child-workflow split).

Moved out of `OrderActivities` verbatim — bodies unchanged, only which workflow calls them
and which class holds the client changed. See `workflows/rider.py` for the state machine
these back.
"""

from logging import Logger

from temporalio import activity

from orchestrator.clients.rider.rider_service import SagaRiderClient


class RiderActivities:
    def __init__(self, *, riders: SagaRiderClient, logger: Logger):
        self._riders = riders
        self._logger = logger

    @activity.defn
    def dispatch_rider_activity(self, details: dict) -> dict:
        """One attempt at claiming the nearest rider.

        The coordinates arrive in `details` rather than being fetched — captured at
        checkout (D32) and carried through `OrderWorkflow` into this child's own payload.

        An empty fleet returns `{"assigned": false}` rather than raising. Whether to wait
        and try again is a scheduling decision, and scheduling belongs to the workflow,
        which can sleep on a durable timer; an activity can only fail.
        """
        order_id = details["order_id"]
        result = self._riders.dispatch(
            order_id,
            float(details["restaurant_latitude"]),
            float(details["restaurant_longitude"]),
        )
        if not result.get("assigned"):
            self._logger.info(
                "No rider available for order %s (%s)",
                order_id,
                result.get("reason", "unknown"),
            )
        return result

    @activity.defn
    def release_rider_activity(self, details: dict) -> dict:
        """Return a claimed rider to the pool.

        Called by `RiderWorkflow` on every exit path that follows a successful dispatch —
        delivered, or a pickup/delivery timeout with no recovered report — so a rider
        claimed by a saga that later fails is never leaked. Idempotent: releasing an order
        nobody holds is success.
        """
        order_id = details["order_id"]
        released = self._riders.release(order_id)
        self._logger.info(
            "Release for order %s: %s", order_id, released.get("released")
        )
        return released
