"""The order lifecycle as a durable state machine — now the thin parent of three child
workflows (D55, the mentor's child-workflow split).

Deterministic by construction: every side effect is an activity or a child workflow, every
wait is a Temporal timer, and every external event arrives as an Update. Nothing here reads
a clock, opens a socket or touches a database. That is what lets the worker be killed
mid-saga and resume exactly where it stopped, which is the property the whole of Week 2
exists to demonstrate.

D54/order-creation-temporal-update-design.md moved order creation inside this workflow: the
order does not exist when this workflow starts, and `run()` opens by waiting on `self._order`,
which the `create_order` Update populates. D53/outbox-removal-temporal-design.md chained a
publish activity after every write, replacing `order_outbox`/`payment_outbox` and their
relays. D55 goes one step further: payment, rider dispatch/delivery, and compensation each
moved out of this file into their own workflow — `PaymentWorkflow`, `RiderWorkflow`,
`CompensationWorkflow` — started here as children and awaited for their result. What remains
in this file is what is genuinely about the *order* as a whole: creating it, holding the
kitchen's decision, and deciding which child runs next based on what the last one returned.

Read alongside activities/order.py: the division of labour is that activities decide what a
*service* said, and this file decides what the *saga* does about it.

Lives at `workflows/order.py`, not a flat `workflows.py`, so each entity this service
orchestrates gets its own `workflows/<entity>.py` — see `workflows/__init__.py` for why that
package boundary carries the same sandbox constraint this file's own import block does.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ChildWorkflowError

from orchestrator.utils.constants import DETAIL_TRUNCATE_LENGTH, PAYMENT_MODE_SAGA
from orchestrator.utils.policies import PUBLISH_POLICY, STATE_POLICY

with workflow.unsafe.imports_passed_through():
    # Every service-local import belongs inside this block, and the reason is not
    # style. Reaching `activities` pulls in `clients`, and through them `common.auth`,
    # which calls `required("JWT_PUBLIC_KEY_B64")` at import time. Passed through, the
    # sandbox reuses the already-loaded modules; outside the block it would re-execute
    # them under restriction and fail the workflow task — forever, since Temporal
    # retries it. Add imports here, never above.
    from orchestrator.activities.order import OrderActivities
    from orchestrator.workflows.compensation import CompensationWorkflow
    from orchestrator.workflows.payment import PaymentWorkflow
    from orchestrator.workflows.rider import RiderWorkflow
    from orchestrator.utils.transitions import recover_via_read, transition_and_publish
    from common.config import ORDER_TASK_QUEUE, RESTAURANT_DECISION_TIMEOUT_SECONDS
    from common.temporal import (
        compensation_workflow_id_for,
        payment_workflow_id_for,
        rider_workflow_id_for,
    )


@workflow.defn
class OrderWorkflow:
    def __init__(self) -> None:
        self._order: dict | None = None
        self._restaurant_decision: str | None = None
        self._order_status: str | None = None
        self._rider_id: str | None = None
        self._stage = "starting"

    # --- updates: synchronous RPCs into a running (or, for create_order, not-yet-running)
    # workflow, each returning a result — D53's replacement for a write this service used
    # to make directly, outside Temporal, followed by a best-effort signal. --------------

    @workflow.update
    async def create_order(self, payload: dict) -> dict:
        """Create the order this workflow is *for* — the write `checkout.py` used to make
        directly before handing the result to the saga as a start argument. Idempotent on
        replay: `execute_update_with_start_workflow` routes a retried checkout to this same
        running workflow (`USE_EXISTING`), and the early return below means it answers with
        the same result rather than trying to create the order a second time.
        """
        if self._order is not None:
            # A genuine replay of this Update, arriving after the first call already
            # created the order (D08: a replay answers 200) — as opposed to the DB-level
            # race `create_order_activity`'s own `created=False` already covers, which is
            # two *concurrent* calls racing before either commits. `self._order["created"]`
            # would otherwise still say `True` from the original call, forever.
            return {**self._order, "created": False}

        result = await workflow.execute_activity(
            OrderActivities.create_order_activity,
            payload,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE_POLICY,
        )
        self._order = result
        if result.get("created"):
            await workflow.execute_activity(
                OrderActivities.publish_order_event_activity,
                {"order_id": result["order"]["id"], "event_type": "order.created"},
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=PUBLISH_POLICY,
            )
        return self._order

    @workflow.update
    async def kitchen_decision(self, payload: dict) -> dict:
        """Record the kitchen's accept or reject — a Temporal Update rather than a direct
        write followed by a best-effort signal (D53). Recording the decision and unblocking
        this workflow's own wait are now the same round trip.
        """
        if self._restaurant_decision is not None:
            return {
                "decision": self._restaurant_decision,
                "status": self._order_status,
                "changed": False,
            }

        order_id = payload["order_id"]
        decided = await workflow.execute_activity(
            OrderActivities.decide_kitchen_activity,
            {"order_id": order_id, "decision": payload["decision"]},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE_POLICY,
        )
        self._order_status = decided.get("status")
        if not decided.get("changed"):
            return {
                "decision": decided.get("decision"),
                "status": self._order_status,
                "changed": False,
            }

        await workflow.execute_activity(
            OrderActivities.publish_order_event_activity,
            {"order_id": order_id, "event_type": "order.kitchen.decided"},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=PUBLISH_POLICY,
        )
        self._restaurant_decision = decided["decision"]
        return {"decision": decided["decision"], "status": self._order_status, "changed": True}

    @workflow.query
    def stage(self) -> dict:
        """Where this saga has got to, without touching the database.

        Cheap observability: `temporal workflow query` answers "why is this order stuck?"
        without a psql session, and the Web UI renders it inline. Rider-specific detail
        (picked up? delivered?) lives on `RiderWorkflow`'s own `stage` query now (D55) —
        this one only reports `rider_id` once dispatch has actually succeeded.
        """
        return {
            "stage": self._stage,
            "rider_id": self._rider_id,
            "restaurant_decision": self._restaurant_decision,
        }

    # --- the run -------------------------------------------------------------------------

    @workflow.run
    async def run(self) -> dict:
        # No start argument any more (D54): the order does not exist yet when this workflow
        # starts (Update-with-Start starts it and delivers `create_order` in one round trip),
        # so the very first thing to do is wait for that Update to populate `self._order`.
        await workflow.wait_condition(lambda: self._order is not None)

        order = self._order["order"]
        order_id = order["id"]
        amount = str(order["total_amount"])
        capacity = self._order["capacity"]
        restaurant_latitude = self._order["restaurant_latitude"]
        restaurant_longitude = self._order["restaurant_longitude"]

        # 1. Payment — a child workflow (D55). Nothing has been charged if this fails, so a
        #    failure here needs no compensation, only a cancellation — the one branch that
        #    never starts CompensationWorkflow.
        self._stage = "authorizing_payment"
        try:
            await workflow.execute_child_workflow(
                PaymentWorkflow.run,
                {"order_id": order_id, "amount": amount, "mode": PAYMENT_MODE_SAGA},
                id=payment_workflow_id_for(order_id),
                task_queue=ORDER_TASK_QUEUE,
            )
        except (ChildWorkflowError, ActivityError) as exc:
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
            await transition_and_publish(
                order_id, "confirmed", "payment-service", capacity_limit=capacity
            )
        except ActivityError as exc:
            return await self._compensate(
                order_id, "kitchen_at_capacity", str(exc.cause or exc)
            )

        #    Then wait on a durable timer for a human to answer, via the `kitchen_decision`
        #    Update (D53) — the workflow is idle here — no thread, no connection, no memory
        #    in any service — and it survives a worker restart.
        try:
            await workflow.wait_condition(
                lambda: self._restaurant_decision is not None,
                timeout=timedelta(seconds=RESTAURANT_DECISION_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            # The timer expired without an Update. That usually means the kitchen never
            # looked at its rail — but it can also mean the kitchen *did* answer and the
            # Update was never delivered. Read the order before concluding anything:
            # refunding an order the kitchen actually accepted is a real customer-visible
            # failure, avoidable with one local lookup.
            self._stage = "recovering_kitchen_decision"
            self._restaurant_decision = await recover_via_read(
                OrderActivities.read_kitchen_decision_activity,
                {"order_id": order_id},
                field="decision",
                allowed=("accepted", "rejected"),
                order_id=order_id,
                what="kitchen decision",
            )

            if self._restaurant_decision is None:
                return await self._compensate(
                    order_id,
                    "kitchen_timeout",
                    f"No decision within {RESTAURANT_DECISION_TIMEOUT_SECONDS}s",
                )

        if self._restaurant_decision == "rejected":
            return await self._compensate(
                order_id, "kitchen_rejected", "Restaurant declined the order"
            )

        # 3. Rider dispatch, pickup, delivery — a child workflow (D55) that owns its own
        #    retry loop, its own signals, and its own cleanup on every exit path.
        self._stage = "dispatching_rider"
        rider_result = await workflow.execute_child_workflow(
            RiderWorkflow.run,
            {
                "order_id": order_id,
                "restaurant_latitude": restaurant_latitude,
                "restaurant_longitude": restaurant_longitude,
            },
            id=rider_workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
        )
        if rider_result["status"] != "delivered":
            return await self._compensate(
                order_id, rider_result["reason"], rider_result.get("detail", "")
            )

        self._rider_id = rider_result.get("rider_id")
        self._stage = "delivered"
        return {"status": "delivered", "order_id": order_id, "rider_id": self._rider_id}

    # --- helpers ------------------------------------------------------------------------

    async def _cancel(self, order_id: str, reason: str, detail: str) -> None:
        """Mark the order cancelled. No money moved, so nothing to give back — this is the
        one cancellation path that does not go through `CompensationWorkflow`."""
        await transition_and_publish(
            order_id,
            "cancelled",
            "order-workflow",
            metadata={"reason": reason, "detail": detail[:DETAIL_TRUNCATE_LENGTH]},
        )

    async def _compensate(self, order_id: str, reason: str, detail: str) -> dict:
        """Start `CompensationWorkflow` as a child and wait for its result (D55)."""
        self._stage = f"compensating:{reason}"
        result = await workflow.execute_child_workflow(
            CompensationWorkflow.run,
            {"order_id": order_id, "reason": reason, "detail": detail},
            id=compensation_workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
        )
        self._stage = f"cancelled:{reason}"
        return result
