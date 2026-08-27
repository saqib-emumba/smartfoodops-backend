"""Currency resolution, shared because two services must round identically.

The Order Service recalculates a cart total and the Payment Service refuses anything that
does not settle it to the cent. Two `quantize` implementations either side of that boundary
is one rounding rule with two answers, and a disagreement rejects a *correct* payment.

The `str()` is load-bearing: `Decimal(0.1)` is the binary float, `Decimal("0.1")` is a tenth.
"""

from decimal import Decimal

CENTS = Decimal("0.01")


def to_cents(amount: float | str | Decimal) -> Decimal:
    """Convert a JSON amount into an exact two-decimal Decimal."""
    return Decimal(str(amount)).quantize(CENTS)
