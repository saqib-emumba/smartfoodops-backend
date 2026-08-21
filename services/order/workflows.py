"""The order lifecycle as a durable state machine.

Deterministic by construction: every side effect is an activity, every wait is a Temporal
timer, and every external event arrives as a signal. Nothing here reads a clock, opens a
socket or touches a database. That is what lets the worker be killed mid-saga and resume
exactly where it stopped, which is the property the whole of Week 2 exists to demonstrate.

The order is already `created` and committed before this starts, with the opening entry of
its audit trail written in the same transaction (D24). The first revision of the Week 2
blueprint began by setting the status to `created` again, recording a `created -> created`
transition that never happened.

Read alongside activities.py: the division of labour is that activities decide what a
*service* said, and this file decides what the *saga* does about it.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    # Flat imports: the Dockerfile copies this service to /app, so there is no
    # `services.order` package. The first revision's `from services.order.activities
    # import ...` would have raised ModuleNotFoundError in the container.
    from activities import OrderActivities
    from common.config import (
        DELIVERY_TIMEOUT_SECONDS,
        RESTAURANT_DECISION_TIMEOUT_SECONDS,
        RIDER_SEARCH_ATTEMPTS,
        RIDER_SEARCH_INTERVAL_SECONDS,
    )

# Transport-level retries only. Business outcomes are raised non-retryably by the
# activities, so they are never re-sent — the bug in the first revision, where a restaurant
# rejection was retried three times before the saga compensated.
TRANSIENT = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

# Compensations retry harder than forward progress, and the asymmetry is deliberate: a
# failed refund leaves a customer charged for an order that will never arrive, which is the
# worst state this system can be in. Better to keep trying for minutes than to give up.
COMPENSATION = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)

# A state write is local to the order database, so it is quick and safe to retry hard.
STATE = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)


@workflow.defn
class OrderWorkflow:
    def __init__(self) -> None:
        self._restaurant_decision: str | None = None
        self._picked_up = False
        self._delivered = False
        self._stage = "starting"
        self._rider_id: str | None = None

    # --- signals: the outside world reporting something it observed ----------------------
    #
    # Handlers only record; they never call activities. A signal handler that did real work
    # would run interleaved with the main coroutine and make the ordering of side effects
    # depend on delivery timing, which is exactly the non-determinism Temporal forbids.

    @workflow.signal
    def restaurant_decision(self, payload: dict) -> None:
        decision = payload.get("decision")
        # Anything unrecognised is ignored rather than raised: an exception in a signal
        # handler fails the workflow task, so a malformed report from a sibling service
        # would take down an order that is otherwise fine.
        if decision in ("accepted", "rejected"):
            self._restaurant_decision = decision
        else:
            workflow.logger.warning("Ignoring unknown kitchen decision %r", decision)

    @workflow.signal
    def rider_pickup(self, payload: dict) -> None:
        self._picked_up = True

    @workflow.signal
    def rider_delivery(self, payload: dict) -> None:
        # A delivery implies a pickup. Setting both means a lost pickup signal cannot
        # deadlock the saga one step short of finishing.
        self._picked_up = True
        self._delivered = True

    @workflow.query
    def stage(self) -> dict:
        """Where this saga has got to, without touching the database.

        Cheap observability: `temporal workflow query` answers "why is this order stuck?"
        without a psql session, and the Web UI renders it inline.
        """
        return {
            "stage": self._stage,
            "rider_id": self._rider_id,
            "restaurant_decision": self._restaurant_decision,
            "picked_up": self._picked_up,
            "delivered": self._delivered,
        }

    # --- the run -------------------------------------------------------------------------

    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]

        # 1. Payment. Nothing has been charged yet, so a failure here needs no refund —
        #    only a cancellation. This is the one branch that does not compensate.
        self._stage = "authorizing_payment"
        try:
            await workflow.execute_activity(
                OrderActivities.authorize_payment_activity,
                {"order_id": order_id, "amount": payload["amount"]},
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=TRANSIENT,
            )
        except ActivityError as exc:
            self._stage = "payment_failed"
            await self._cancel(order_id, "payment_failed", str(exc.cause or exc))
            return {"status": "cancelled", "order_id": order_id, "reason": "payment_failed"}

        # 2. The kitchen. Entering 'confirmed' *is* joining the kitchen's rail, so this
        #    single transition both records the payment and claims a capacity slot — one
        #    local transaction, so two orders cannot both take the last one. A full kitchen
        #    comes back as a non-retryable failure and compensates; there is no separate
        #    "send the ticket" call any more, because there is no ticket (D32).
        self._stage = "awaiting_kitchen"
        try:
            await self._transition(
                order_id,
                "confirmed",
                "payment-service",
                capacity_limit=payload["capacity"],
            )
        except ActivityError as exc:
            return await self._compensate(
                order_id, "kitchen_at_capacity", str(exc.cause or exc)
            )

        #    Then wait on a durable timer for a human to answer. The workflow is idle here —
        #    no thread, no connection, no memory in any service — and it survives a worker
        #    restart. The first revision modelled this as a synchronous HTTP call that
        #    returned the decision, which no kitchen can do.

        try:
            await workflow.wait_condition(
                lambda: self._restaurant_decision is not None,
                timeout=timedelta(seconds=RESTAURANT_DECISION_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            # The timer expired without a signal. That usually means the kitchen never
            # looked at its rail — but it can also mean the kitchen *did* answer and the
            # signal was lost, because the decision is committed before it is relayed. Read
            # the order before concluding anything: refunding an order the kitchen actually
            # accepted is a real customer-visible failure, avoidable with one local lookup.
            self._stage = "recovering_kitchen_decision"
            self._restaurant_decision = await self._recover_kitchen_decision(order_id)

            if self._restaurant_decision is None:
                # Genuinely no decision on record. Silence is a refusal — the customer
                # must not wait indefinitely on a kitchen that ignored the order.
                return await self._compensate(
                    order_id,
                    "kitchen_timeout",
                    f"No decision within {RESTAURANT_DECISION_TIMEOUT_SECONDS}s",
                )

        if self._restaurant_decision == "rejected":
            return await self._compensate(
                order_id, "kitchen_rejected", "Restaurant declined the order"
            )

        # 3. Rider search. Repeated attempts separated by durable timers, because "no rider
        #    free right now" is a condition that resolves with time and a single blocking
        #    call cannot wait for it. The first revision gave one attempt a 120-second
        #    start_to_close_timeout and called that the allocation window; that bounds an
        #    attempt, not a search.
        self._stage = "dispatching_rider"
        assignment = await self._find_rider(order_id, payload)
        if assignment is None:
            return await self._compensate(
                order_id,
                "no_rider_available",
                f"No rider found after {RIDER_SEARCH_ATTEMPTS} attempts",
            )

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

        # 4. Collection, then delivery. Both wait on the rider reporting in.
        self._stage = "awaiting_pickup"
        try:
            await workflow.wait_condition(
                lambda: self._picked_up,
                timeout=timedelta(seconds=DELIVERY_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            return await self._compensate(
                order_id, "pickup_timeout", "Rider never collected the order"
            )

        await self._transition(order_id, "picked_up", "rider-service")

        self._stage = "awaiting_delivery"
        try:
            await workflow.wait_condition(
                lambda: self._delivered,
                timeout=timedelta(seconds=DELIVERY_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            return await self._compensate(
                order_id, "delivery_timeout", "Rider never completed the delivery"
            )

        await self._transition(
            order_id, "delivered", "rider-service", metadata={"rider_id": self._rider_id}
        )

        # The rider is released *after* the terminal transition, so availability can never
        # say "free" while the order still says "in transit".
        await self._release_rider(order_id)

        self._stage = "delivered"
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
        capacity_limit: int | None = None,
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
                "capacity_limit": capacity_limit,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE,
        )

    async def _recover_kitchen_decision(self, order_id: str) -> str | None:
        """Ask the Restaurant Service what the ticket says, after waiting timed out.

        Returns `"accepted"`, `"rejected"`, or `None` for "no decision on record".

        This is the recovery for the one hole the signal design leaves open: the decision is
        committed before the signal carrying it is sent, so a lost signal used to mean a
        refund for an order that had actually been accepted. Reading the record turns that
        from a silent wrong answer into a self-correcting one.

        Since D32 the read is local — `orders.kitchen_decision`, in this service's own
        database — so it uses the STATE retry policy rather than TRANSIENT, and the old
        "the other service is unreachable so we cannot tell" branch is gone. What remains
        is only a database that will not answer, and that is treated as no decision: the
        safe default, because refunding an accepted order is recoverable by a human while
        leaving a charged customer on a saga that never finishes is not.
        """
        try:
            recorded_on_order = await workflow.execute_activity(
                OrderActivities.read_kitchen_decision_activity,
                {"order_id": order_id},
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=STATE,
            )
        except ActivityError as exc:
            workflow.logger.error(
                "Could not read the kitchen decision for order %s after the timeout; "
                "treating it as no decision: %s",
                order_id,
                exc.cause or exc,
            )
            return None

        recorded = recorded_on_order.get("decision")
        if recorded in ("accepted", "rejected"):
            # The kitchen had answered all along; only the signal went missing.
            workflow.logger.info(
                "Recovered a lost kitchen decision for order %s: '%s'",
                order_id,
                recorded,
            )
            return recorded

        workflow.logger.info(
            "Order %s has no kitchen decision on record; the wait was genuine silence",
            order_id,
        )
        return None

    async def _find_rider(self, order_id: str, payload: dict) -> dict | None:
        """Try repeatedly to claim a rider, sleeping on a durable timer between attempts."""
        for attempt in range(RIDER_SEARCH_ATTEMPTS):
            result = await workflow.execute_activity(
                OrderActivities.dispatch_rider_activity,
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
            OrderActivities.release_rider_activity,
            {"order_id": order_id},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=COMPENSATION,
        )

    async def _cancel(self, order_id: str, reason: str, detail: str) -> None:
        """Mark the order cancelled. No money moved, so nothing to give back."""
        await self._transition(
            order_id,
            "cancelled",
            "order-workflow",
            metadata={"reason": reason, "detail": detail[:500]},
        )

    async def _compensate(self, order_id: str, reason: str, detail: str) -> dict:
        """Undo everything the saga has done, then cancel the order.

        Order matters. The refund goes first because it is the customer's money. The rider
        release follows, and only if one was claimed — the first revision refunded and
        stopped there, leaking every rider whose order later failed.

        Both retry under COMPENSATION rather than TRANSIENT: giving up on a rollback is
        worse than giving up on forward progress.
        """
        self._stage = f"compensating:{reason}"
        workflow.logger.info("Compensating order %s: %s (%s)", order_id, reason, detail)

        await workflow.execute_activity(
            OrderActivities.refund_payment_activity,
            {"order_id": order_id, "reason": reason},
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=COMPENSATION,
        )

        if self._rider_id is not None:
            await self._release_rider(order_id)

        # Nothing frees the kitchen's capacity slot, because nothing has to: the rail is
        # defined as `status = 'confirmed' AND kitchen_decision IS NULL`, so the cancel
        # below removes this order from it as a side effect of being cancelled. That is the
        # whole reason D32 could delete the expire-ticket step — and with it an entire class
        # of leak, where a slot stayed occupied by an order that no longer existed.
        await self._cancel(order_id, reason, detail)
        self._stage = f"cancelled:{reason}"
        return {"status": "cancelled", "order_id": order_id, "reason": reason}
