"""Handing an order to the saga, and telling it what the kitchen decided.

Three shapes have held this job. Before D36 it was `order/saga.py`, with a Temporal client
built here and `client.start_workflow(OrderWorkflow.run, ...)` naming the workflow by class
reference — which imported `orchestrator.workflows.order`, which imported the activities,
which loaded the payment and rider clients into this process with neither
`PAYMENT_SERVICE_URL` nor `RIDER_SERVICE_URL` set. D36 fixed that by putting the
Orchestrator Service's REST facade in front of Temporal and making this an HTTP client.

D47 removes the facade instead. `SagaClient` names the workflow by *string*
(`"OrderWorkflow"`), so this service holds a Temporal client again while importing nothing
from `orchestrator` — the property D36 actually needed, obtained without the extra hop. The
smoke check for it is in the plan: `sys.modules` inside the running container must list no
`orchestrator.*` module.

Both methods here swallow every failure, and that is deliberate rather than lazy. The order
or the decision is already committed by the time either runs, and D09's argument holds
regardless of which process is doing the committing: a write that already succeeded must not
be reported to the client as a failure. The relay that used to raise honestly —
`signal(order_id, ...)`, called by the rider report route — is gone with that route; the
Rider Service now signals Temporal itself, so it is the one that learns whether its signal
landed.
"""

from uuid import UUID

from common.config import ORDER_TASK_QUEUE
from common.temporal import SagaClient, workflow_id_for

ORDER_WORKFLOW = "OrderWorkflow"


class OrchestratorClient:
    """This service's view of the order saga: start it, and signal the kitchen's answer."""

    def __init__(self, saga: SagaClient, *, logger):
        self._saga = saga
        self._logger = logger

    async def start_saga(self, order: dict, restaurant: dict) -> None:
        """Hand a committed order to the saga. Deliberately after the commit, and
        deliberately not fatal — see the module docstring and D09.

        `capacity`, `latitude` and `longitude` are snapshots taken at checkout (D32), so
        this call carries everything the saga needs and the orchestrator never has to ask
        the Restaurant Service anything.

        `amount` is stringified rather than passed as a `Decimal`: it has to survive JSON
        into workflow history exactly (D07), and a float would not. That used to be enforced
        by `OrderSagaStartRequest.amount: str` on the facade's Pydantic model; with the
        facade gone there is no validator left to catch it, so it rests on this line.
        """
        order_id = order["id"]
        try:
            await self._saga.start(
                ORDER_WORKFLOW,
                task_queue=ORDER_TASK_QUEUE,
                wf_id=workflow_id_for(order_id),
                payload={
                    "order_id": str(order_id),
                    "restaurant_id": str(order["restaurant_id"]),
                    "amount": str(order["total_amount"]),
                    "capacity": restaurant["capacity"],
                    "restaurant_latitude": restaurant["latitude"],
                    "restaurant_longitude": restaurant["longitude"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - the order is committed; never fail on this
            # Error rather than warning: an order with no saga stays at `created` forever
            # until something retries it, which is worth an alert even though it is not
            # worth a 500 to a client whose order was in fact created.
            self._logger.error("Could not start the saga for order %s: %s", order_id, exc)

    async def signal_saga_best_effort(self, order_id: UUID, signal: str, body: dict) -> None:
        """Tell the saga about something already committed, without being able to undo it.

        Best-effort on purpose. The kitchen's decision is in the database by the time this
        runs, and the admin must not see an error for something that worked — so a signal
        that cannot be delivered is logged, not raised.

        Losing it is survivable precisely because of the read-back: when the saga's timer
        expires it asks the Order Service for `orders.kitchen_decision` and finds the
        decision anyway (D32, D36). This is the one place where those two mechanisms are
        designed as a pair.
        """
        try:
            await self._saga.signal(workflow_id_for(order_id), signal, body)
        except Exception as exc:  # noqa: BLE001 - the decision is committed; do not undo it
            self._logger.error(
                "Recorded the decision for order %s but could not signal the saga; "
                "its timeout will read the decision back instead: %s",
                order_id,
                exc,
            )
