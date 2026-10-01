"""`RiderWorkflow` — dispatch, pickup, delivery, for one order (D55, the mentor's
child-workflow split).

Started as a child of `OrderWorkflow` once the kitchen has accepted. Owns the rider's whole
lifecycle for this order end to end, including releasing whatever it claimed on *every* exit
path — delivered, or a pickup/delivery timeout with nothing recovered — so `OrderWorkflow`'s
own compensation path never needs to know whether a rider was ever assigned at all: by the
time `CompensationWorkflow` might start, this workflow has already cleaned up after itself,
or there was never anything to clean up (dispatch never found one, or never ran because the
kitchen rejected first).

Signalled directly by the Rider Service now (`services/rider/fleet.py`), not relayed through
`OrderWorkflow` — see `common.temporal.rider_workflow_id_for`.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities.order import OrderActivities
    from orchestrator.activities.rider import RiderActivities
    from common.config import (
        DELIVERY_TIMEOUT_SECONDS,
        RIDER_SEARCH_ATTEMPTS,
        RIDER_SEARCH_INTERVAL_SECONDS,
    )

TRANSIENT = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

STATE = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)

# A release is a compensating action in spirit even on the happy path — giving up on it
# leaves a rider permanently unavailable — so it retries under the same patient policy
# OrderWorkflow's own COMPENSATION used before this move.
RELEASE = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)

PUBLISH = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)


@workflow.defn
class RiderWorkflow:
    def __init__(self) -> None:
        self._rider_id: str | None = None
        self._picked_up = False
        self._delivered = False

    @workflow.signal
    def rider_pickup(self, payload: dict) -> None:
        self._picked_up = True

    @workflow.signal
    def rider_delivery(self, payload: dict) -> None:
        # A delivery implies a pickup. Setting both means a lost pickup signal cannot
        # deadlock this workflow one step short of finishing.
        self._picked_up = True
        self._delivered = True

    @workflow.query
    def stage(self) -> dict:
        return {
            "rider_id": self._rider_id,
            "picked_up": self._picked_up,
            "delivered": self._delivered,
        }

    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]

        assignment = await self._find_rider(order_id, payload)
        if assignment is None:
            return {
                "status": "failed",
                "order_id": order_id,
                "reason": "no_rider_available",
                "detail": f"No rider found after {RIDER_SEARCH_ATTEMPTS} attempts",
            }

        self._rider_id = assignment.get("rider_id")
        await self._transition(
            order_id,
            "assigned",
            "rider-service",
            metadata={
                "rider_id": self._rider_id,
                "distance_km": assignment.get("distance_km"),
                "eta_minutes": assignment.get("eta_minutes"),
            },
            rider_id=self._rider_id,
        )

        # Week 3 (D46): a timeout here no longer fails outright. The signal that would have
        # set `self._picked_up`/`self._delivered` can be lost the same way a kitchen
        # decision's used to be (both commit the fact locally, then signal — see
        # order/apis/rider_reports.py), so a genuine timeout and a lost signal look
        # identical from inside `wait_condition`. `_recover_rider_report` tells them apart
        # with one local-database read on the Order Service's side.
        try:
            await workflow.wait_condition(
                lambda: self._picked_up, timeout=timedelta(seconds=DELIVERY_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            recovered = await self._recover_rider_report(order_id)
            if recovered in ("picked_up", "delivered"):
                self._picked_up = True
                if recovered == "delivered":
                    self._delivered = True
            else:
                await self._release_rider(order_id)
                return {
                    "status": "failed",
                    "order_id": order_id,
                    "reason": "pickup_timeout",
                    "detail": "Rider never collected the order",
                }

        await self._transition(order_id, "picked_up", "rider-service")

        if not self._delivered:
            try:
                await workflow.wait_condition(
                    lambda: self._delivered,
                    timeout=timedelta(seconds=DELIVERY_TIMEOUT_SECONDS),
                )
            except asyncio.TimeoutError:
                recovered = await self._recover_rider_report(order_id)
                if recovered == "delivered":
                    self._delivered = True
                else:
                    await self._release_rider(order_id)
                    return {
                        "status": "failed",
                        "order_id": order_id,
                        "reason": "delivery_timeout",
                        "detail": "Rider never completed the delivery",
                    }

        await self._transition(
            order_id, "delivered", "rider-service", metadata={"rider_id": self._rider_id}
        )
        # Released *after* the terminal transition, so availability can never say "free"
        # while the order still says "in transit".
        await self._release_rider(order_id)
        return {"status": "delivered", "order_id": order_id, "rider_id": self._rider_id}

    # --- helpers ------------------------------------------------------------------------

    async def _transition(
        self,
        order_id: str,
        new_status: str,
        updated_by: str,
        *,
        metadata: dict | None = None,
        rider_id: str | None = None,
    ) -> None:
        await workflow.execute_activity(
            OrderActivities.transition_order_activity,
            {
                "order_id": order_id,
                "status": new_status,
                "updated_by": updated_by,
                "event": {"event": f"order_{new_status}", "order_id": order_id},
                "metadata": metadata or {},
                "rider_id": rider_id,
                "capacity_limit": None,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE,
        )
        await workflow.execute_activity(
            OrderActivities.publish_order_event_activity,
            {"order_id": order_id, "event_type": f"order.{new_status}", "metadata": metadata or {}},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=PUBLISH,
        )

    async def _recover_rider_report(self, order_id: str) -> str | None:
        try:
            recorded_on_order = await workflow.execute_activity(
                OrderActivities.read_rider_report_activity,
                {"order_id": order_id},
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=STATE,
            )
        except ActivityError as exc:
            workflow.logger.error(
                "Could not read the rider report for order %s after the timeout; "
                "treating it as no report: %s",
                order_id,
                exc.cause or exc,
            )
            return None

        recorded = recorded_on_order.get("stage")
        if recorded in ("picked_up", "delivered"):
            workflow.logger.info(
                "Recovered a lost rider report for order %s: '%s'", order_id, recorded
            )
            return recorded

        workflow.logger.info(
            "Order %s has no rider report on record; the wait was genuine silence",
            order_id,
        )
        return None

    async def _find_rider(self, order_id: str, payload: dict) -> dict | None:
        """Try repeatedly to claim a rider, sleeping on a durable timer between attempts."""
        for attempt in range(RIDER_SEARCH_ATTEMPTS):
            result = await workflow.execute_activity(
                RiderActivities.dispatch_rider_activity,
                {
                    "order_id": order_id,
                    "restaurant_latitude": payload["restaurant_latitude"],
                    "restaurant_longitude": payload["restaurant_longitude"],
                },
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=TRANSIENT,
            )
            if result.get("assigned"):
                return result
            if attempt < RIDER_SEARCH_ATTEMPTS - 1:
                # asyncio.sleep inside a workflow is a Temporal timer, not a blocked
                # thread: the worker can be restarted during it and the wait resumes.
                await asyncio.sleep(RIDER_SEARCH_INTERVAL_SECONDS)
        return None

    async def _release_rider(self, order_id: str) -> None:
        await workflow.execute_activity(
            RiderActivities.release_rider_activity,
            {"order_id": order_id},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RELEASE,
        )
