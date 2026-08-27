"""The order saga's payment calls: authorise and refund, on the internal key.

Used only by activities/order.py. An activity has no user behind it, so these carry the
internal key rather than a forwarded bearer token — see clients/order/__init__.py.
"""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_PAYMENT_SERVICE_URL, PAYMENT_HTTP_TIMEOUT
from common.errors import unprocessable
from common.service_client import ServiceFacade


class SagaPaymentClient(ServiceFacade):
    display_name = "Payment Service"
    env_var = "PAYMENT_SERVICE_URL"
    default_url = DEFAULT_PAYMENT_SERVICE_URL
    # The only client here with a non-default timeout. Authorising a card is a round trip
    # to an external processor and takes seconds, so the platform-wide 5s HTTP_TIMEOUT
    # would abort a call that was going to succeed.
    timeout = PAYMENT_HTTP_TIMEOUT

    def authorize(self, order_id: UUID, amount: str, idempotency_key: str) -> dict:
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
        return self._client.post(
            "/api/v1/payments/refund",
            json={"order_id": str(order_id), "reason": reason},
            missing=f"Order {order_id} has no payment to refund",
            missing_error=unprocessable,
            unreachable_hint="cannot refund the payment",
            bad_gateway_hint="refunding the payment",
            headers=internal_headers(),
        )
