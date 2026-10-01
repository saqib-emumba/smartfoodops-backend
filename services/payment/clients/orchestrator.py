"""Starting `PaymentWorkflow` in manual mode, and waiting for it to finish (D53/
outbox-removal-temporal-design.md ss4b, unified with the saga's own payment step by D55).

The Payment Service holds its own Temporal client for this, the same pattern D47 already
established for the Order Service and Rider Service: `SagaClient` names the workflow by
string, so this service imports nothing from `orchestrator/`.
"""

from common.auth import CurrentUser
from common.config import ORDER_TASK_QUEUE
from common.temporal import SagaClient, manual_payment_workflow_id_for
from payment.schemas.payments import PaymentCreateRequest

PAYMENT_WORKFLOW = "PaymentWorkflow"


class PaymentOrchestratorClient:
    """This service's view of `PaymentWorkflow`: start it in manual mode, wait for the
    result."""

    def __init__(self, saga: SagaClient, *, logger):
        self._saga = saga
        self._logger = logger

    async def create_manual_payment(
        self,
        payload: PaymentCreateRequest,
        *,
        idempotency_key: str,
        current_user: CurrentUser,
    ) -> dict:
        """Run `PaymentWorkflow` (manual mode) to completion and return
        `{payment, created}`.

        Workflow id is derived from `order_id` via `manual_payment_workflow_id_for` — a
        different id than the saga's own payment step uses for the same order (see that
        function's docstring for why two ids, not one, despite being the same workflow
        type) — so a retried request routes to this path's own prior execution
        (`USE_EXISTING`) rather than authorising twice, without ever addressing whatever
        the saga already did for this order. That collision, if the saga already paid, is
        still caught — by the database's own `UNIQUE(order_id)` constraint (D30), not by
        the workflow id.
        """
        return await self._saga.run(
            PAYMENT_WORKFLOW,
            task_queue=ORDER_TASK_QUEUE,
            wf_id=manual_payment_workflow_id_for(payload.order_id),
            payload={
                "order_id": str(payload.order_id),
                "amount": payload.amount,
                "idempotency_key": idempotency_key,
                "customer_id": str(current_user.user_id),
                "customer_roles": current_user.roles,
                "mode": "manual",
            },
        )
