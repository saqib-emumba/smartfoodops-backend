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

from orchestrator.utils.policies import RELEASE_POLICY, TRANSIENT_POLICY

with workflow.unsafe.imports_passed_through():
    from orchestrator.workflows.order.activities import OrderActivities
    from orchestrator.workflows.rider.activities import RiderActivities
    from orchestrator.utils.transitions import recover_via_read, transition_and_publish
    from common.config import (
        DELIVERY_TIMEOUT_SECONDS,
        RIDER_SEARCH_ATTEMPTS,
        RIDER_SEARCH_INTERVAL_SECONDS,
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
        await transition_and_publish(
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

        await transition_and_publish(order_id, "picked_up", "rider-service")

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

        await transition_and_publish(
            order_id, "delivered", "rider-service", metadata={"rider_id": self._rider_id}
        )
        # Released *after* the terminal transition, so availability can never say "free"
        # while the order still says "in transit".
        await self._release_rider(order_id)
        return {"status": "delivered", "order_id": order_id, "rider_id": self._rider_id}

    # --- helpers ------------------------------------------------------------------------

    async def _recover_rider_report(self, order_id: str) -> str | None:
        return await recover_via_read(
            OrderActivities.read_rider_report_activity,
            {"order_id": order_id},
            field="stage",
            allowed=("picked_up", "delivered"),
            order_id=order_id,
            what="rider report",
        )

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
                retry_policy=TRANSIENT_POLICY,
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
            retry_policy=RELEASE_POLICY,
        )
