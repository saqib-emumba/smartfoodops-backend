"""Activities for `PaymentWorkflow` (D55, the mentor's child-workflow split).

Holds every payment-side activity now: the saga's authorise and refund (moved out of
`OrderActivities` verbatim), the manual/direct path's create (D53), and one publish activity
both paths and `CompensationWorkflow`'s refund step share — `PaymentEventData`'s shape never
differed between authorize/refund/manual, so there is no reason for three near-identical
publish methods where one, taking the already-fetched payment dict, already covers all of
them.
"""

from logging import Logger
from typing import Callable

from fastapi import HTTPException
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.clients.payment.payment_service import (
    ManualPaymentConflict,
    ManualPaymentRejected,
    PaymentServiceClient,
)


def payment_key(order_id: str) -> str:
    """The idempotency key for this order's authorisation.

    Derived, never generated. A retried activity must present the key of the attempt it is
    replacing, or the unique index that prevents double charging never sees a collision and
    the customer pays twice.
    """
    return f"wf-pay-{order_id}"


class PaymentActivities:
    def __init__(self, *, payments: PaymentServiceClient, logger: Logger):
        self._payments = payments
        self._logger = logger

    def all_activities(self) -> list[Callable]:
        """Every activity Temporal should register for this class — see
        `OrderActivities.all_activities`'s docstring for why this list is explicit rather
        than reflected. Used by both `PaymentWorkflow` (authorize/create/publish) and
        `CompensationWorkflow` (refund/publish)."""
        return [
            self.authorize_payment_activity,
            self.refund_payment_activity,
            self.create_manual_payment_activity,
            self.publish_payment_event_activity,
        ]

    # --- saga -----------------------------------------------------------------------------

    @activity.defn
    def authorize_payment_activity(self, details: dict) -> dict:
        """Charge the card, on behalf of the order saga's `PaymentWorkflow`.

        A declined card is final; an unreachable Payment Service is not. The Payment
        Service answers `422` for an amount that does not settle the order and `409` for an
        order already paid — both arrive here as a `bad_gateway`/`unprocessable`
        HTTPException from ServiceClient, which is a *retryable* exception by default. That
        is wrong for a decline, so the status of the returned payment is what this checks:
        anything other than `authorized` is a non-retryable failure.
        """
        order_id = details["order_id"]
        payment = self._payments.authorize(
            order_id, details["amount"], payment_key(order_id)
        )
        if payment.get("status") != "authorized":
            raise ApplicationError(
                f"Payment for order {order_id} came back '{payment.get('status')}' "
                "rather than authorized",
                non_retryable=True,
            )
        self._logger.info("Authorised payment for order %s", order_id)
        return payment

    @activity.defn
    def refund_payment_activity(self, details: dict) -> dict:
        """Compensating action, called by `CompensationWorkflow`.

        Carries no idempotency key of its own because the Payment Service makes this
        idempotent by *status* — a payment already `refunded` is returned untouched. That is
        the safer guarantee for money: a key can be lost, but the row's state cannot.
        """
        order_id = details["order_id"]
        refunded = self._payments.refund(order_id, details.get("reason", "saga_failure"))
        self._logger.info(
            "Refund for order %s resolved as '%s'", order_id, refunded.get("status")
        )
        return refunded

    # --- manual/direct (D53) ---------------------------------------------------------------

    @activity.defn
    def create_manual_payment_activity(self, payload: dict) -> dict:
        """Idempotent by the payload's own `idempotency_key`, the same guarantee
        `authorise()` already gives every caller — a retry of this activity replays rather
        than double-charging. A rejection or a genuine conflict is a business answer, not a
        transport failure, so both are non-retryable — `type=` lets
        `common.temporal._raise_mapped` recover the right HTTP status on the client side."""
        try:
            return self._payments.create_manual(payload)
        except ManualPaymentRejected as exc:
            raise ApplicationError(str(exc), type="ManualPaymentRejected", non_retryable=True) from exc
        except ManualPaymentConflict as exc:
            raise ApplicationError(str(exc), type="ManualPaymentConflict", non_retryable=True) from exc
        except HTTPException as exc:
            # Same reasoning as OrderActivities.create_order_activity's identical clause: a
            # status this method has no named exception for (a 403 from the order-ownership
            # check inside `authorise()`, say) is still a business answer, tagged generically
            # by status rather than by a bespoke exception type per code.
            raise ApplicationError(
                str(exc.detail), type=f"http_{exc.status_code}", non_retryable=True
            ) from exc

    # --- publishing (D53: replaces payment_outbox and its relay) ---------------------------

    @activity.defn
    def publish_payment_event_activity(self, details: dict) -> None:
        """Tell Kafka about a payment fact. `details["payment"]` is always the full payment
        row — `authorize_payment_activity`/`refund_payment_activity`/
        `create_manual_payment_activity` (via `result["payment"]`) all already return
        everything `PaymentEventData` needs, so this never has to re-read anything."""
        payment = details["payment"]
        data = {
            "payment_id": payment["id"],
            "order_id": payment["order_id"],
            "amount": payment["amount"],
            "transaction_reference": payment.get("transaction_reference"),
        }
        self._payments.publish_event(event_type=details["event_type"], data=data)
