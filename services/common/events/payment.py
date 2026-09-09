"""`data` shape for `payment.*` events.

`payment.authorized` and `payment.refunded` share one shape too — see
`PaymentRepository.mark_authorized`/`mark_refunded` in
`services/payment/repositories/payments.py`, which build the exact same payload for both.
"""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class PaymentEventData(BaseModel):
    payment_id: UUID
    order_id: UUID
    amount: Decimal
    transaction_reference: str | None = None


EVENT_DATA_MODELS: dict[str, tuple[type[BaseModel], int]] = {
    "payment.authorized": (PaymentEventData, 1),
    "payment.refunded": (PaymentEventData, 1),
}
