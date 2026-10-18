"""The router's output, parsed defensively (D63).

The router asks the model for one JSON object. Models sometimes wrap it in prose or a code fence,
and a user can try to talk the model into a different shape, so the parse never trusts it:
the intent must be one of the five, tags are intersected with the shared vocabulary (a tag the
menu cannot carry would only filter everything out), the budget must be a positive number and the
order id must be a UUID. A reply that cannot be parsed falls back to food discovery with no
constraints — retrieval and its similarity floor, not the router, are what stop a nonsense
question producing a recommendation.
"""

import json
import re
from dataclasses import dataclass, field
from uuid import UUID

from common.dietary import DIETARY_TAGS

INTENTS = ("food_discovery", "order_tracking", "order_history", "restaurant_support", "out_of_scope")
_OBJECT = re.compile(r"\{.*\}", re.S)


@dataclass
class RouterDecision:
    intent: str = "food_discovery"
    max_price: float | None = None
    dietary_filters: list[str] = field(default_factory=list)
    order_id: str | None = None


def parse_router(text: str) -> RouterDecision:
    match = _OBJECT.search(text)
    if match is None:
        return RouterDecision()
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return RouterDecision()
    if not isinstance(raw, dict):
        return RouterDecision()

    decision = RouterDecision()
    if raw.get("intent") in INTENTS:
        decision.intent = raw["intent"]

    price = raw.get("max_price")
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        decision.max_price = float(price)

    tags = raw.get("dietary_filters")
    if isinstance(tags, list):
        decision.dietary_filters = [t for t in dict.fromkeys(tags) if t in DIETARY_TAGS]

    order_id = raw.get("order_id")
    if isinstance(order_id, str):
        try:
            decision.order_id = str(UUID(order_id))
        except ValueError:
            pass
    return decision
