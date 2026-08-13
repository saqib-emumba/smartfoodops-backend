"""Money handling and the one amount rule this service enforces.

A payment must settle its order exactly. The client sends an amount, but the authoritative
figure is the `total_amount` the Order Service already recalculated from the live menu, so
the two are compared before anything is charged — the same "never trust the caller's
total" discipline the Order Service applies when it re-prices a cart.

Amounts are Decimal everywhere in between, and only become float at the JSON boundary.
"""

from decimal import Decimal, InvalidOperation

from common.errors import bad_gateway, unprocessable

# Currency resolution used for every rounding step.
CENTS = Decimal("0.01")


def to_cents(amount: float) -> Decimal:
    """Convert a JSON amount into an exact two-decimal Decimal."""
    return Decimal(str(amount)).quantize(CENTS)


def order_total(order: dict) -> Decimal:
    """Read the order's authoritative total out of an Order Service response."""
    try:
        return to_cents(order["total_amount"])
    except (KeyError, TypeError, InvalidOperation) as exc:
        # The Order Service answered, but not with something we can charge against.
        raise bad_gateway(
            "Order Service returned an order without a usable total_amount"
        ) from exc


def assert_settles_order(order: dict, amount: Decimal) -> None:
    """Reject a payment that does not cover its order to the cent."""
    total = order_total(order)
    if amount != total:
        raise unprocessable(
            f"amount mismatch: payment of {amount} does not settle order "
            f"{order.get('id')}, whose total is {total}"
        )
