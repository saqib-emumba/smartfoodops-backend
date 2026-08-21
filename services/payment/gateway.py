"""Stand-in for the external card gateway (Stripe, Adyen, …).

Week 1 has no gateway credentials and no PCI scope, so authorisation is simulated. The
seam is real, though: this is the only module that would change when a live gateway is
wired in, and it is deliberately the one thing in this service that does not touch the
database — so a gateway outage can never leave a half-written row behind.

The idempotency key is forwarded rather than ignored because every real gateway accepts
one, and that is what makes a retried charge safe on their side as well as ours.
"""

import time
from dataclasses import dataclass
from decimal import Decimal
from logging import Logger
from uuid import UUID, uuid4

from common.config import MOCK_GATEWAY_LATENCY_SECONDS

# Prefix mirrors Stripe's `ch_` charge ids, with `mock` in the middle so a simulated
# reference is never mistaken for a real one in a log or a database dump.
_REFERENCE_PREFIX = "ch_mock"


_REFUND_PREFIX = "re_mock"


@dataclass(frozen=True)
class Authorization:
    """What the gateway hands back once it has put a hold on the card."""

    reference: str


@dataclass(frozen=True)
class Refund:
    """What the gateway hands back once it has released a hold."""

    reference: str


class MockPaymentGateway:
    """Always authorises, and returns a unique reference for the transaction."""

    name = "mock-gateway"

    def __init__(self, logger: Logger):
        self._logger = logger

    def authorize(
        self, *, order_id: UUID, amount: Decimal, idempotency_key: str
    ) -> Authorization:
        # Stand in for the round trip to a real processor, which takes seconds. Returning
        # instantly made every timeout downstream of here untested: the saga's payment
        # activity, its retry policy and the HTTP ceiling on the call into this service were
        # all sized for a latency that never happened.
        #
        # A blocking sleep is correct rather than lazy. This method is called from a sync
        # FastAPI handler, which runs in the threadpool, so the event loop keeps serving
        # other requests; and the seam deliberately holds no database connection while it
        # waits (see the module docstring), so the delay costs a thread and nothing else.
        self._logger.info(
            "Contacting %s for order %s (%.1fs)",
            self.name,
            order_id,
            MOCK_GATEWAY_LATENCY_SECONDS,
        )
        time.sleep(MOCK_GATEWAY_LATENCY_SECONDS)

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

    def refund(
        self, *, order_id: UUID, amount: Decimal, idempotency_key: str
    ) -> Refund:
        """Release a hold the saga can no longer honour.

        Added in Week 2 for the compensation path. It belongs here for the same reason
        `authorize` does: this is the one file that changes when a live gateway is wired
        in, and keeping it away from the database means a gateway failure can never leave
        a half-written row.

        A distinct `re_` prefix rather than another `ch_`, so a refund and the charge it
        reverses are never confused for each other in a log.
        """
        reference = f"{_REFUND_PREFIX}_{uuid4().hex[:24]}"
        self._logger.info(
            "Refunded %s for order %s via %s (reference %s, key %s)",
            amount,
            order_id,
            self.name,
            reference,
            idempotency_key,
        )
        return Refund(reference=reference)
