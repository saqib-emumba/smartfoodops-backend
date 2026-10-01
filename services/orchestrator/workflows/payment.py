"""`PaymentWorkflow` — one workflow type for both ways a payment gets authorised (D55, the
mentor's child-workflow split; supersedes the narrower `ManualPaymentWorkflow` from D53).

`OrderWorkflow` starts this as a child for the saga's own authorisation step, and
`payment-service`'s direct/manual endpoint (`apis/payments.py::process_payment`, D30) starts
it directly, synchronously awaiting the result the same way it awaited
`ManualPaymentWorkflow` before this merge. `payload["mode"]` is the only thing that decides
which activity does the write; both paths publish through the same activity afterward,
since `PaymentEventData`'s shape never differed between them.

Refunding lives entirely in `CompensationWorkflow`, not here — this workflow authorises and
completes. See workflows/compensation.py for why that split was chosen over a long-lived
`PaymentWorkflow` that stays open waiting for a possible refund signal.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities.payment import PaymentActivities

# A card authorisation is a round trip to an external processor and takes seconds, not
# milliseconds — the same bound OrderWorkflow's own TRANSIENT policy used for this call
# before the move.
AUTHORIZE = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

# D53: no `maximum_attempts` — unlimited retries, on purpose. Temporal's own retry-until-
# success is what replaced payment_outbox's relay; see OrderWorkflow's identical PUBLISH
# policy for the full reasoning.
PUBLISH = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)


@workflow.defn
class PaymentWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        if payload.get("mode") == "manual":
            outcome = await workflow.execute_activity(
                PaymentActivities.create_manual_payment_activity,
                payload,
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=AUTHORIZE,
            )
        else:
            payment = await workflow.execute_activity(
                PaymentActivities.authorize_payment_activity,
                payload,
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=AUTHORIZE,
            )
            # The saga's own authorisation never replays at this layer — each order
            # authorises exactly once, unlike the manual path's client-guessable key, which
            # is why only that path's activity returns an explicit `created` flag at all.
            outcome = {"payment": payment, "created": True}

        if outcome["created"]:
            await workflow.execute_activity(
                PaymentActivities.publish_payment_event_activity,
                {"event_type": "payment.authorized", "payment": outcome["payment"]},
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=PUBLISH,
            )
        return outcome
