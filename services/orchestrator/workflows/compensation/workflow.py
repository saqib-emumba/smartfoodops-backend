"""`CompensationWorkflow` — undo a saga that cannot proceed (D55, the mentor's
child-workflow split).

Started as a child of `FulfillmentWorkflow` (D58) whenever forward progress stops: the kitchen's rail is
full, it never answers, it rejects the order, or `RiderWorkflow` reports it could not
complete (no rider found, or a pickup/delivery that never happened). Refunds the payment,
then cancels the order — in that order, because the refund is the customer's money.

Deliberately never touches a rider. `RiderWorkflow` releases whatever it claimed on every one
of its own exit paths before this workflow ever starts (see workflows/rider/workflow.py) — either a
rider was never assigned (the kitchen never got that far), or it has already been released.
Keeping that cleanup inside `RiderWorkflow` itself, rather than passing a `rider_id` here for
this workflow to release, is what lets this workflow stay a pure "undo the money and the
order" concern with no fleet awareness at all.

If the refund itself cannot be completed — `COMPENSATION_POLICY`'s own retries exhausted,
and `FulfillmentWorkflow._compensate`'s workflow-level retry of this whole workflow exhausted on
top of that — that is treated as a business outcome, not a technical failure left to kill
this workflow unhandled: `'compensation_failed'` is a real `order_status` value precisely so
this case is visible the same way every other transition is (`order_tracking_logs`, the
`GET /api/v1/orders?status=compensation_failed` admin listing), not only discoverable by
knowing to check Temporal's own failed-workflow list.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

from orchestrator.utils.constants import DETAIL_TRUNCATE_LENGTH
from orchestrator.utils.policies import COMPENSATION_POLICY, PUBLISH_POLICY

with workflow.unsafe.imports_passed_through():
    from orchestrator.workflows.payment.activities import PaymentActivities
    from orchestrator.utils.transitions import transition_and_publish


@workflow.defn
class CompensationWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]
        reason = payload["reason"]
        detail = payload.get("detail", "")

        workflow.logger.info("Compensating order %s: %s (%s)", order_id, reason, detail)

        try:
            refund = await workflow.execute_activity(
                PaymentActivities.refund_payment_activity,
                {"order_id": order_id, "reason": reason},
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=COMPENSATION_POLICY,
            )
            await workflow.execute_activity(
                PaymentActivities.publish_payment_event_activity,
                {"event_type": "payment.refunded", "payment": refund},
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=PUBLISH_POLICY,
            )
        except ActivityError as exc:
            # Not wrapped around the 'cancelled' transition below: a failure writing that
            # (the local order DB itself unreachable after STATE_POLICY's own retries) is a
            # deeper outage nothing here can route around either, so that one is left to
            # surface as a failed Temporal workflow same as before — the last resort this
            # whole mechanism exists to avoid reaching for the refund specifically.
            workflow.logger.error(
                "Compensation for order %s could not refund the payment after every "
                "retry; marking 'compensation_failed' for manual review: %s",
                order_id,
                exc.cause or exc,
            )
            failure_detail = f"refund failed: {exc.cause or exc}"
            await transition_and_publish(
                order_id,
                "compensation_failed",
                "order-workflow",
                metadata={
                    "reason": reason,
                    "detail": failure_detail[:DETAIL_TRUNCATE_LENGTH],
                },
            )
            return {"status": "compensation_failed", "order_id": order_id, "reason": reason}

        # Nothing frees the kitchen's capacity slot explicitly, because nothing has to: the
        # rail is defined as `status = 'confirmed' AND kitchen_decision IS NULL` (D32), so
        # the transition below removes this order from it as a side effect of being
        # cancelled.
        await transition_and_publish(
            order_id,
            "cancelled",
            "order-workflow",
            metadata={"reason": reason, "detail": detail[:DETAIL_TRUNCATE_LENGTH]},
        )

        return {"status": "cancelled", "order_id": order_id, "reason": reason}
