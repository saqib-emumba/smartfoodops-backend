"""The one authorisation algorithm, used by both endpoints that charge a card.

The customer-facing endpoint and the saga's endpoint ran the same five steps in two copies:
replay check, amount check against the order, claim the key with a `pending` row, charge,
record the verdict. Two copies of the double-charge guarantee is one guarantee that can
drift, so there is one here instead.

Exactly two things differed between them, and both are parameters rather than branches:

`fetch_order` — the customer path reads the order as the caller (forwarding their bearer
token, which is also what enforces ownership); the saga path reads it on the internal key,
because a workflow has no user behind it (D26).

`verify_replay` — on a replay the customer path re-reads the order to confirm the caller
owns it, since idempotency keys are client-chosen and therefore guessable. The saga path
does not: its key is derived from the order id by the workflow, so there is no guess to
make.
"""

from decimal import Decimal
from typing import Callable
from uuid import UUID

from payment.amounts import assert_settles_order, to_cents


def authorise(
    payload,
    *,
    idempotency_key: str,
    amount: float | str | Decimal,
    fetch_order: Callable[[UUID], dict],
    verify_replay: bool,
    payments,
    gateway,
    logger,
    replay_log: str,
) -> tuple[dict, bool]:
    """Authorise at most once for `idempotency_key`. Returns `(payment, replayed)`.

    The caller turns `replayed` into a 200 on a route declared 201 — that status decision
    is HTTP's business and stays in the router.
    """
    # (a) Replay protection, before anything reaches the gateway. This is the whole
    # double-charge guarantee.
    existing = payments.find_by_idempotency_key(idempotency_key)
    if existing is not None:
        if verify_replay:
            fetch_order(existing["order_id"])
        logger.info(replay_log, idempotency_key)
        return existing, True

    # (b) `payments.order_id` lost its foreign key when this table moved out of the Order
    # Service's database. The HTTP check that replaces it sits here, immediately before the
    # write, and doubles as the guard that the amount settles the order exactly.
    settled = to_cents(amount)
    assert_settles_order(fetch_order(payload.order_id), settled)

    # (c) Record the intent first: the insert claims the idempotency key, so a concurrent
    # retry is rejected by the unique index here rather than at the gateway.
    payment = payments.create_pending(payload, settled)

    # (d) Charge, then store what the gateway gave back. A gateway failure leaves the row
    # `pending` with nothing charged — the saga's refund is what reconciles those, which is
    # exactly why these are two steps and not one.
    authorization = gateway.authorize(
        order_id=payload.order_id, amount=settled, idempotency_key=idempotency_key
    )
    return payments.mark_authorized(payment["id"], authorization.reference), False
