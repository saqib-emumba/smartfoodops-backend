"""`CompensationWorkflow` — undo a saga that cannot proceed (D55, the mentor's
child-workflow split).

Started as a child of `OrderWorkflow` whenever forward progress stops: the kitchen's rail is
full, it never answers, it rejects the order, or `RiderWorkflow` reports it could not
complete (no rider found, or a pickup/delivery that never happened). Refunds the payment,
then cancels the order — in that order, because the refund is the customer's money.

Deliberately never touches a rider. `RiderWorkflow` releases whatever it claimed on every one
of its own exit paths before this workflow ever starts (see workflows/rider.py) — either a
rider was never assigned (the kitchen never got that far), or it has already been released.
Keeping that cleanup inside `RiderWorkflow` itself, rather than passing a `rider_id` here for
this workflow to release, is what lets this workflow stay a pure "undo the money and the
order" concern with no fleet awareness at all.
"""

from datetime import timedelta

from temporalio import workflow

from orchestrator.utils.constants import DETAIL_TRUNCATE_LENGTH
from orchestrator.utils.policies import COMPENSATION_POLICY, PUBLISH_POLICY

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities.payment import PaymentActivities
    from orchestrator.utils.transitions import transition_and_publish


@workflow.defn
class CompensationWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]
        reason = payload["reason"]
        detail = payload.get("detail", "")

        workflow.logger.info("Compensating order %s: %s (%s)", order_id, reason, detail)

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
