"""`PaymentWorkflow`'s calls into the Payment Service: authorise (saga or manual mode),
refund, and publish.

D55 (the mentor's child-workflow split) merged this with what used to be
`clients/order/payment.py`'s `SagaPaymentClient` — one payment entity, one client, whether
the caller is the order saga's `PaymentWorkflow` child or `CompensationWorkflow`'s refund
step. Every call here carries the internal key, never a forwarded bearer token, for the
reason D26 gives everywhere else in `orchestrator/`: an activity has no user behind it.
"""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_PAYMENT_SERVICE_URL, PAYMENT_HTTP_TIMEOUT
from common.errors import unprocessable
from common.service_client import ServiceFacade


class ManualPaymentRejected(Exception):
    """The amount does not settle the order, or the order does not exist — `422`."""


class ManualPaymentConflict(Exception):
    """This order already has a payment, or a concurrent replay claimed the key — `409`."""


class PaymentServiceClient(ServiceFacade):
    display_name = "Payment Service"
    env_var = "PAYMENT_SERVICE_URL"
    default_url = DEFAULT_PAYMENT_SERVICE_URL
    # A card authorisation is a round trip to an external processor and takes seconds, not
    # milliseconds.
    timeout = PAYMENT_HTTP_TIMEOUT

    def authorize(self, order_id: UUID, amount: str, idempotency_key: str) -> dict:
        """The saga's authorisation — moved verbatim from `SagaPaymentClient.authorize`."""
        return self._client.post(
            "/api/v1/payments/authorize",
            json={
                "order_id": str(order_id),
                # A string, so an exact decimal survives the JSON boundary (D07).
                "amount": amount,
                "idempotency_key": idempotency_key,
            },
            missing=f"Order {order_id} is unknown to the Payment Service",
            missing_error=unprocessable,
            unreachable_hint="cannot authorise the payment",
            bad_gateway_hint="authorising the payment",
            headers=internal_headers(),
        )

    def refund(self, order_id: UUID, reason: str) -> dict:
        """The saga's compensating action — moved verbatim from `SagaPaymentClient.refund`."""
        return self._client.post(
            "/api/v1/payments/refund",
            json={"order_id": str(order_id), "reason": reason},
            missing=f"Order {order_id} has no payment to refund",
            missing_error=unprocessable,
            unreachable_hint="cannot refund the payment",
            bad_gateway_hint="refunding the payment",
            headers=internal_headers(),
        )

    def create_manual(self, payload: dict) -> dict:
        """The write behind `PaymentWorkflow.run`'s manual mode — see
        payment/apis/saga.py::create_manual_payment. Returns `{payment, created}`.

        `passthrough` is what keeps a business rejection (unsettled amount, an order
        already paid) from flattening into a generic `502` here — see
        `activities/payment.py::create_manual_payment_activity`, which converts these into
        non-retryable `ApplicationError`s Temporal can actually stop retrying on."""
        return self._client.post(
            "/api/v1/payments/manual",
            json=payload,
            missing="Unknown order referenced by this payment",
            missing_error=unprocessable,
            unreachable_hint="cannot create the payment",
            bad_gateway_hint="creating the payment",
            headers=internal_headers(),
            passthrough={409: ManualPaymentConflict, 422: ManualPaymentRejected},
        )

    def publish_event(self, *, event_type: str, data: dict) -> None:
        self._client.post(
            "/api/v1/payments/internal/events",
            json={"event_type": event_type, "data": data},
            missing="Payment Service unreachable",
            unreachable_hint="cannot publish the event",
            bad_gateway_hint="publishing the event",
            headers=internal_headers(),
        )
