"""Handing an order to the orchestrator, and telling it about events afterward.

Before D36 this was `order/saga.py`, holding a Temporal client directly — this service and
its worker were the only two processes in the platform that did. Now this service holds
none: starting a saga and relaying a signal into one are both HTTP calls into the
Orchestrator Service, on the internal key, exactly like every other cross-service write this
service makes.

Two shapes coexist here because the platform already needed both. `start_saga` and
`signal_saga_best_effort` swallow every failure — the order or the decision is already
committed, and D09's argument holds regardless of which process is doing the committing:
a write that already succeeded must not be reported to the client as a failure. `signal`
does the opposite, because `apis/signals.py` is itself a relay another service's request
depends on succeeding or failing honestly — a rider reporting a pickup needs to know whether
it landed, not have this service decide on their behalf that it does not matter.
"""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_ORCHESTRATOR_SERVICE_URL
from common.errors import conflict, not_found
from common.service_client import ServiceFacade


class OrchestratorClient(ServiceFacade):
    display_name = "Orchestrator Service"
    env_var = "ORCHESTRATOR_SERVICE_URL"
    default_url = DEFAULT_ORCHESTRATOR_SERVICE_URL

    async def start_saga(self, order: dict, restaurant: dict) -> None:
        """Hand a committed order to the orchestrator. Deliberately after the commit, and
        deliberately not fatal — see the module docstring and D09.

        `capacity`, `latitude` and `longitude` are snapshots taken at checkout (D32), so
        this call carries everything the saga needs and the orchestrator never has to ask
        the Restaurant Service anything.
        """
        order_id = order["id"]
        try:
            await self._client.apost(
                "/api/v1/orchestrator/sagas",
                json={
                    "order_id": str(order_id),
                    "restaurant_id": str(order["restaurant_id"]),
                    "amount": str(order["total_amount"]),
                    "capacity": restaurant["capacity"],
                    "restaurant_latitude": restaurant["latitude"],
                    "restaurant_longitude": restaurant["longitude"],
                },
                missing=f"Order {order_id} was not found by the orchestrator",
                unreachable_hint="the saga was not started",
                headers=internal_headers(),
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
            await self.signal(order_id, signal, body)
        except Exception as exc:  # noqa: BLE001 - the decision is committed; do not undo it
            self._logger.error(
                "Recorded the decision for order %s but could not signal the saga; "
                "its timeout will read the decision back instead: %s",
                order_id,
                exc,
            )

    async def signal(self, order_id: UUID, signal: str, body: dict) -> None:
        """Relay one event, honestly — the caller decides whether a failure matters.

        Used by `apis/signals.py`, whose own caller (a sibling reporting a pickup or a
        delivery) is entitled to a real `404`/`409` rather than a swallowed failure: unlike
        a kitchen decision, there is no local record for a lost rider event to be recovered
        from (see the Open Questions entry on this).
        """
        await self._client.apost(
            f"/api/v1/orchestrator/sagas/{order_id}/signals",
            json={"signal": signal, "payload": body},
            missing=(
                f"Order {order_id} has no running saga to signal; it may have already "
                "finished or been cancelled"
            ),
            missing_error=not_found,
            unreachable_hint="cannot signal the saga",
            headers=internal_headers(),
            passthrough={409: conflict},
        )
