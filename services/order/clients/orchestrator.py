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
"""

from uuid import UUID

from common.config import ORDER_TASK_QUEUE
from common.temporal import SagaClient, workflow_id_for
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
        """Create an order via Update-with-Start: one round trip for "the order exists and
        its saga is running", closing the gap D25 accepted as a named cost.

        `amount` (here, `total_amount`) is a plain float rather than stringified: unlike the
        old `start_saga` payload, this one is validated by `OrderInternalCreateRequest` on
        arrival and never has to survive an intermediate JSON hop as the authoritative
        figure — it exists only to be checked against the server's own recalculation
        (D06), never persisted from this value directly.
        """
        return await self._saga.start_with_update(
            ORDER_WORKFLOW,
            task_queue=ORDER_TASK_QUEUE,
            wf_id=workflow_id_for(order_id),
            update="create_order",
            update_payload={
                "order_id": str(order_id),
                "customer_id": str(current_user.user_id),
                "customer_roles": current_user.roles,
                "restaurant_id": str(payload.restaurant_id),
                "items": [item.model_dump() for item in payload.items],
                "total_amount": payload.total_amount,
                "idempotency_key": idempotency_key,
            },
        )

    async def decide_kitchen(self, order_id: UUID, decision: str) -> dict:
        """Record the kitchen's decision via a Temporal Update.

        Replaces D53's removed write-then-best-effort-signal: a lost signal used to be
        recovered later by the saga's own timeout read-back (D32); now there is nothing to
        lose; the write and the saga's awareness of it are the same round trip.
        """
        return await self._saga.update(
            workflow_id_for(order_id),
            "kitchen_decision",
            {"order_id": str(order_id), "decision": decision},
        )
