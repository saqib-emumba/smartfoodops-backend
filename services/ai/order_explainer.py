"""Grounded facts about an order, for the AI to explain (Week 5, D64).

Gathers what the platform recorded — the order, its transition trail, whether and how far away a
courier is — and reduces it to a small dictionary of plain facts. Everything the explanation may
say comes from that dictionary: the model is handed it as data, the guardrail checks the answer's
numbers against it, and the deterministic fallback is rendered from it. Nothing is estimated here,
and a source that could not be read is named in `unavailable` rather than quietly left out.

What it will not say: a cause the saga did not record (`reason` codes are mapped through a fixed
table, an unknown code reads as "no recorded reason"), an arrival time (the platform has no real
one), or anything from a log's free-text `detail`, which can carry technical error text.
"""

import asyncio
from datetime import datetime, timezone
from logging import Logger

from common.auth import CurrentUser, require_self_or_admin
from ai.clients.order import OrderServiceClient
from ai.clients.restaurant import RestaurantServiceClient
from ai.clients.rider import RiderServiceClient

TERMINAL = {"delivered", "cancelled", "compensation_failed"}

STATUS_MEANING = {
    "created": "The order has been placed and is waiting to be confirmed.",
    "confirmed": "The order is paid for and with the restaurant.",
    "assigned": "A courier has been assigned to the order.",
    "picked_up": "The courier has collected the order and is on the way.",
    "delivered": "The order has been delivered.",
    "cancelled": "The order was cancelled.",
    "compensation_failed": (
        "The order was cancelled, but its refund could not be completed automatically "
        "and is with our team to resolve."
    ),
}

# The saga's machine codes, in words a customer can read. A code not listed is not guessed at.
REASON_TEXT = {
    "kitchen_at_capacity": "the restaurant was too busy to take the order",
    "kitchen_timeout": "the restaurant did not respond in time",
    "kitchen_rejected": "the restaurant was unable to accept the order",
    "no_rider_available": "no courier was available nearby",
    "pickup_timeout": "the courier did not collect the order in time",
    "delivery_timeout": "the delivery was not completed in time",
}

# What a still-running fulfilment is waiting on, from the workflow's own stage name.
WAITING_TEXT = {
    "awaiting_kitchen": "the restaurant to accept the order",
    "recovering_kitchen_decision": "the restaurant to accept the order",
    "dispatching_rider": "a courier to take the order",
}


def _minutes(delta_seconds: float) -> int:
    return max(0, round(delta_seconds / 60))


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


class OrderExplainer:
    def __init__(
        self,
        orders: OrderServiceClient,
        restaurants: RestaurantServiceClient,
        riders: RiderServiceClient,
        *,
        logger: Logger,
    ):
        self._orders = orders
        self._restaurants = restaurants
        self._riders = riders
        self._logger = logger

    async def latest_order_id(self, current_user: CurrentUser) -> str | None:
        return await asyncio.to_thread(self._orders.latest_order_id, str(current_user.user_id))

    async def facts(self, order_id: str, current_user: CurrentUser) -> dict:
        """The grounded facts for one order. `403` unless it is the caller's own (or an admin's call)."""
        context = await asyncio.to_thread(self._orders.explain_context, order_id)
        require_self_or_admin(
            current_user,
            context["customer_id"],
            detail="You may only ask about your own orders",
        )

        unavailable: list[str] = []
        restaurant = await self._restaurant(context["restaurant_id"], unavailable)
        courier = await self._courier(context, restaurant, unavailable)
        return self._build(context, restaurant, courier, unavailable)

    async def _restaurant(self, restaurant_id: str, unavailable: list[str]) -> dict | None:
        try:
            return await asyncio.to_thread(self._restaurants.get_restaurant, restaurant_id)
        except Exception as exc:  # noqa: BLE001 - a name is decoration; the explanation still stands
            self._logger.warning("Could not read restaurant %s: %s", restaurant_id, exc)
            unavailable.append("restaurant details")
            return None

    async def _courier(self, context: dict, restaurant: dict | None, unavailable: list[str]) -> dict | None:
        """Courier distance, only while a rider holds an order that has not been delivered."""
        if not context["rider_assigned"] or context["status"] in TERMINAL:
            return None
        if restaurant is None or restaurant.get("latitude") is None:
            return {"assigned": True, "distance_km": None}
        try:
            result = await asyncio.to_thread(
                self._riders.order_courier,
                context["order_id"],
                float(restaurant["latitude"]),
                float(restaurant["longitude"]),
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Could not read courier distance for %s: %s", context["order_id"], exc)
            unavailable.append("courier distance")
            return {"assigned": True, "distance_km": None}
        return {"assigned": result["assigned"], "distance_km": result.get("distance_km")}

    def _build(self, context: dict, restaurant: dict | None, courier: dict | None, unavailable: list[str]) -> dict:
        status = context["status"]
        timeline = context["timeline"]
        stamps = [_parse(entry["occurred_at"]) for entry in timeline]

        steps = []
        for index, entry in enumerate(timeline):
            steps.append(
                {
                    "status": entry["status"],
                    "minutes_after_previous": _minutes((stamps[index] - stamps[index - 1]).total_seconds())
                    if index
                    else 0,
                }
            )

        facts: dict = {
            "restaurant": restaurant.get("name") if restaurant else None,
            "status": status,
            "status_meaning": STATUS_MEANING.get(status, "The order is being processed."),
            "steps": steps,
            "kitchen_decision": context["kitchen_decision"],
        }
        if status in TERMINAL and stamps:
            facts["total_minutes"] = _minutes((stamps[-1] - stamps[0]).total_seconds())
        elif stamps:
            facts["placed_minutes_ago"] = _minutes((datetime.now(timezone.utc) - stamps[0]).total_seconds())

        reasons = [entry["reason"] for entry in timeline if entry.get("reason")]
        if status in ("cancelled", "compensation_failed"):
            facts["cause"] = REASON_TEXT.get(reasons[-1]) if reasons else None
        stage = (context["saga"] or {}).get("fulfillment_stage")
        if status not in TERMINAL and stage in WAITING_TEXT:
            facts["waiting_on"] = WAITING_TEXT[stage]
        if courier is not None:
            facts["courier"] = courier
        facts["unavailable"] = unavailable
        facts["plain_summary"] = render_summary(facts)
        return facts


def quantities_in(facts: dict) -> set[float]:
    """Every figure an explanation may quote: the numbers the facts carry, nothing derived."""
    figures: set[float] = set()
    for key in ("total_minutes", "placed_minutes_ago"):
        if facts.get(key) is not None:
            figures.add(float(facts[key]))
    for step in facts["steps"]:
        figures.add(float(step["minutes_after_previous"]))
    courier = facts.get("courier") or {}
    if courier.get("distance_km") is not None:
        figures.add(float(courier["distance_km"]))
    return figures


def render_summary(facts: dict) -> str:
    """The explanation, written by rules from the facts alone.

    Used as the answer when the model is not used or the guardrail rejects the model's own — so
    it is built to be unarguably true, plain rather than warm.
    """
    where = f" from {facts['restaurant']}" if facts.get("restaurant") else ""
    parts = [f"Your order{where}: {facts['status_meaning']}"]
    if facts.get("cause"):
        parts.append(f"The reason recorded is that {facts['cause']}.")
    elif facts["status"] in ("cancelled", "compensation_failed"):
        parts.append("We do not have a recorded reason.")
    if facts.get("waiting_on"):
        parts.append(f"It is currently waiting for {facts['waiting_on']}.")
    if facts.get("placed_minutes_ago") is not None:
        parts.append(f"It was placed {facts['placed_minutes_ago']} minutes ago.")
    courier = facts.get("courier")
    if courier and courier.get("distance_km") is not None:
        parts.append(f"The assigned courier is {courier['distance_km']} km from the restaurant.")
    if facts["unavailable"]:
        parts.append(f"I could not load: {', '.join(facts['unavailable'])}.")
    return " ".join(parts)


async def explain(model, facts: dict) -> tuple[str, bool]:
    """The model's explanation of `facts`, or the rule-written one if it cannot be trusted.

    Returns `(text, grounded)`. Imported lazily by callers that already hold a model; kept here,
    beside the facts it explains, so the assistant's order path and `POST /explain-order` cannot
    diverge in how they check an answer.
    """
    import json

    from langchain_core.messages import HumanMessage, SystemMessage

    from ai.agents import prompts
    from ai.guardrails import verify_quantities
    from ai.llm import call_model
    from ai.metrics import GUARDRAIL_VIOLATIONS_TOTAL

    text = await call_model(
        model,
        [SystemMessage(content=prompts.EXPLAIN), HumanMessage(content=f"FACTS: {json.dumps(facts, default=str)}")],
        node="explain",
    )
    text = text.strip()
    verdict = verify_quantities(text, quantities=quantities_in(facts))
    for kind, _ in verdict.violations:
        GUARDRAIL_VIOLATIONS_TOTAL.labels(kind=kind).inc()
    if not text or not verdict.ok:
        return facts["plain_summary"], False
    return text, True
