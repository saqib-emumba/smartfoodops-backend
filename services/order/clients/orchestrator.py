"""Handing an order to the saga, and telling it what the kitchen decided.

Three shapes have held this job. Before D36 it was `order/saga.py`, with a Temporal client
built here and `client.start_workflow(OrderWorkflow.run, ...)` naming the workflow by class
reference — which imported `orchestrator.workflows.order`, which imported the activities,
which loaded the payment and rider clients into this process with neither
`PAYMENT_SERVICE_URL` nor `RIDER_SERVICE_URL` set. D36 fixed that by putting the
Orchestrator Service's REST facade in front of Temporal and making this an HTTP client.

D47 removes the facade instead. `SagaClient` names the workflow by *string*
(`"OrderWorkflow"`), so this service holds a Temporal client again while importing nothing
from `orchestrator` — the property D36 actually needed, obtained without the extra hop.

D53/order-creation-temporal-update-design.md replaces both methods this class used to hold.
Starting a saga is no longer a separate, best-effort step after the order already exists —
`create_order` below *is* how the order comes to exist, via Update-with-Start, so a failure
here is no longer swallowed: the order genuinely was not created and the caller sees a `503`.
Signalling the kitchen's decision is no longer best-effort either, for the same reason —
`decide_kitchen` is a Temporal Update now, not a write followed by a signal the saga might
never see.

D58 takes the Update out of creation entirely, at the mentors' direction: the cart is the
workflow's *start* argument, `OrderWorkflow.run()` creates the order and authorises its
payment itself and ends once the order is paid for, and this client only starts it and
waits for its result. The kitchen's
decision now goes to `FulfillmentWorkflow`, the child that is actually waiting for it.
"""

from uuid import UUID

from common.config import (
    CHECKOUT_DEADLINE_SECONDS,
    ORDER_TASK_QUEUE,
)
from common.temporal import SagaClient, fulfillment_workflow_id_for, workflow_id_for
from order.schemas.orders import OrderCreateRequest

ORDER_WORKFLOW = "OrderWorkflow"


class OrchestratorClient:
    """This service's view of the order saga: create an order through it, and tell it the
    kitchen's answer — both now synchronous, both now genuinely fail when Temporal cannot
    be reached, rather than degrading gracefully the way D25's fire-and-forget start did."""

    def __init__(self, saga: SagaClient, *, logger):
        self._saga = saga
        self._logger = logger

    async def create_order(
        self,
        order_id: UUID,
        payload: OrderCreateRequest,
        *,
        current_user,
        idempotency_key: str,
    ) -> dict:
        """Start the order's workflow and wait for its result: the order, created and paid
        for (D58). Nothing is sent into the workflow and nothing is polled.

        Waits through payment authorisation, so this call is as slow as the Payment
        Service — the price of answering 402 on a declined payment instead of a 201 for an
        order that is about to be cancelled.

        `amount` (here, `total_amount`) is a plain float rather than stringified: unlike the
        old `start_saga` payload, this one is validated by `OrderInternalCreateRequest` on
        arrival and never has to survive an intermediate JSON hop as the authoritative
        figure — it exists only to be checked against the server's own recalculation
        (D06), never persisted from this value directly.
        """
        order, started = await self._saga.start_and_wait(
            ORDER_WORKFLOW,
            task_queue=ORDER_TASK_QUEUE,
            wf_id=workflow_id_for(order_id),
            deadline=CHECKOUT_DEADLINE_SECONDS,
            payload={
                "order_id": str(order_id),
                "customer_id": str(current_user.user_id),
                "customer_roles": current_user.roles,
                "restaurant_id": str(payload.restaurant_id),
                "items": [item.model_dump() for item in payload.items],
                "total_amount": payload.total_amount,
                "idempotency_key": idempotency_key,
            },
        )
        # A replay is either a retry that found the first request's run still going
        # (`started` is False) or one that arrived after it finished and started a fresh run
        # whose insert found the order already there (`created` is False). Either way the
        # caller answers 200 rather than 201 (D08).
        return {**order, "created": started and order["created"]}

    async def decide_kitchen(self, order_id: UUID, decision: str) -> dict:
        """Record the kitchen's decision via a Temporal Update.

        Replaces D53's removed write-then-best-effort-signal: a lost signal used to be
        recovered later by the saga's own timeout read-back (D32); now there is nothing to
        lose; the write and the saga's awareness of it are the same round trip.

        Addressed to `FulfillmentWorkflow` (D58), not `OrderWorkflow`: an order is only on
        the kitchen's rail once that child has confirmed it, so the child is always running
        by the time a kitchen can decide.
        """
        return await self._saga.update(
            fulfillment_workflow_id_for(order_id),
            "kitchen_decision",
            {"order_id": str(order_id), "decision": decision},
        )
