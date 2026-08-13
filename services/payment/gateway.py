"""Stand-in for the external card gateway (Stripe, Adyen, …).

Week 1 has no gateway credentials and no PCI scope, so authorisation is simulated. The
seam is real, though: this is the only module that would change when a live gateway is
wired in, and it is deliberately the one thing in this service that does not touch the
database — so a gateway outage can never leave a half-written row behind.

The idempotency key is forwarded rather than ignored because every real gateway accepts
one, and that is what makes a retried charge safe on their side as well as ours.
"""

from dataclasses import dataclass
from decimal import Decimal
from logging import Logger
from uuid import UUID, uuid4

# Prefix mirrors Stripe's `ch_` charge ids, with `mock` in the middle so a simulated
# reference is never mistaken for a real one in a log or a database dump.
_REFERENCE_PREFIX = "ch_mock"


@dataclass(frozen=True)
class Authorization:
    """What the gateway hands back once it has put a hold on the card."""

    reference: str


class MockPaymentGateway:
    """Always authorises, and returns a unique reference for the transaction."""

    name = "mock-gateway"

    def __init__(self, logger: Logger):
        self._logger = logger

    def authorize(
        self, *, order_id: UUID, amount: Decimal, idempotency_key: str
    ) -> Authorization:
        reference = f"{_REFERENCE_PREFIX}_{uuid4().hex[:24]}"
        self._logger.info(
            "Authorised %s for order %s via %s (reference %s, key %s)",
            amount,
            order_id,
            self.name,
            reference,
            idempotency_key,
        )
        return Authorization(reference=reference)
