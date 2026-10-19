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
from common.delivery import MAX_DELIVERY_KM, delivery_estimate, minutes_remaining
from ai.retrieval.geo import haversine_km
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
    "payment_failed": "the payment could not be authorised",
    "kitchen_at_capacity": "the restaurant was too busy to take the order",
    "kitchen_timeout": "the restaurant did not respond in time",
    "kitchen_rejected": "the restaurant was unable to accept the order",
    "no_rider_available": "no courier was available nearby",
    "pickup_timeout": "the courier did not collect the order in time",
    "delivery_timeout": "the delivery was not completed in time",
}

# The reasons that cancel an order through `CompensationWorkflow`. That workflow refunds *first* and
# writes `cancelled` only once the refund has succeeded (a refund that fails every retry becomes
# `compensation_failed` instead), so a `cancelled` row with one of these reasons means the payment was
# refunded. `payment_failed` is the other kind of cancellation: it happens before anything is charged and
# never reaches the refund step. Checked against the data: every order cancelled for a reason below has a
# `refunded` payment, and every `payment_failed` order has no payment row. A reason in neither set (a new
# one, say) claims nothing about money.
REFUNDED_REASONS = {
    "kitchen_at_capacity",
    "kitchen_timeout",
    "kitchen_rejected",
    "no_rider_available",
    "pickup_timeout",
    "delivery_timeout",
}

# What a still-running fulfilment is waiting on, from the workflow's own stage name.
WAITING_TEXT = {
    "awaiting_kitchen": "the restaurant to accept the order",
    "recovering_kitchen_decision": "the restaurant to accept the order",
    "dispatching_rider": "a courier to take the order",
}


# How many of a customer's newest orders the history path itemises.
HISTORY_LIMIT = 5

# An order's state as a customer reads it in a list.
HISTORY_STATUS = {
    "created": "in progress (placed, waiting to be confirmed)",
    "confirmed": "in progress (with the restaurant)",
    "assigned": "in progress (a courier has been assigned)",
    "picked_up": "in progress (on its way to you)",
    "delivered": "delivered",
    "cancelled": "cancelled",
    "compensation_failed": "cancelled (the refund could not be completed automatically and is with our team)",
}


def _minutes(delta_seconds: float) -> int:
    return max(0, round(delta_seconds / 60))


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _span(minutes: int) -> str:
    """A whole-minute duration in words. Under a minute reads as that, not "0 minutes"."""
    if minutes < 1:
        return "less than a minute"
    return f"about {minutes} minute" + ("" if minutes == 1 else "s")


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

    async def latest_order_id(self, current_user: CurrentUser, status: str | None = None) -> str | None:
        return await asyncio.to_thread(self._orders.latest_order_id, str(current_user.user_id), status)

    async def history_facts(self, current_user: CurrentUser) -> dict | None:
        """Facts about the caller's orders as a whole: counts by state and their newest few orders.

        For "how many pending orders do I have?" and "what did I order last time?". The customer is whoever
        the token says, so there is no id to check ownership against. `None` when they have never ordered.
        Counts come from the Order Service's own grouped query and cover every order; only the newest few are
        itemised. Restaurant names are read live; a name that could not be read becomes "a restaurant" and is
        listed in `unavailable`, never guessed.
        """
        summary = await asyncio.to_thread(self._orders.customer_summary, str(current_user.user_id), HISTORY_LIMIT)
        counts = summary["counts"]
        if not counts["total"]:
            return None
        unavailable: list[str] = []
        names: dict[str, dict] = {}
        restaurant_ids = list({order["restaurant_id"] for order in summary["recent"]})
        if restaurant_ids:
            try:
                names = await asyncio.to_thread(self._restaurants.names, restaurant_ids)
            except Exception as exc:  # noqa: BLE001 - a name is decoration; the counts still stand
                self._logger.warning("Could not read restaurant names for an order history: %s", exc)
                unavailable.append("restaurant names")
        now = datetime.now(timezone.utc)
        recent = []
        for order in summary["recent"]:
            ago = _minutes((now - _parse(order["created_at"])).total_seconds())
            recent.append(
                {
                    "restaurant": (names.get(order["restaurant_id"]) or {}).get("name") or "a restaurant",
                    "status": HISTORY_STATUS.get(order["status"], "in an unusual state"),
                    "total": f"${order['total_amount']:.2f}",
                    "items": [f"{item['quantity']} x {item['name']}" for item in order["items"]],
                    # Minutes only while recent; an exact date after that, so a stale "about 4000 minutes ago"
                    # never reaches the customer.
                    "placed": (
                        f"{_span(ago)} ago" if ago < 90 else f"on {_parse(order['created_at']):%d %B %Y}"
                    ),
                    "placed_minutes_ago": ago if ago < 90 else None,
                }
            )
        facts = {
            "kind": "order_history",
            "total_orders": counts["total"],
            "in_progress": counts["in_progress"],
            "delivered": counts["delivered"],
            "cancelled": counts["cancelled"],
            "recent_orders_shown": len(recent),
            "recent_orders": recent,
            "unavailable": unavailable,
        }
        facts["plain_summary"] = render_history(facts)
        return facts

    async def facts(
        self, order_id: str, current_user: CurrentUser, destination: tuple[float, float] | None = None
    ) -> dict:
        """The grounded facts for one order. `403` unless it is the caller's own (or an admin's call).

        `destination` is the customer's shared `(latitude, longitude)`, if any. Orders carry no delivery address,
        so it is the only way the delivery estimate can use the real trip to the customer; without it a typical
        trip is assumed and the facts say so.
        """
        context = await asyncio.to_thread(self._orders.explain_context, order_id)
        require_self_or_admin(
            current_user,
            context["customer_id"],
            detail="You may only ask about your own orders",
        )

        unavailable: list[str] = []
        restaurant = await self._restaurant(context["restaurant_id"], unavailable)
        courier = await self._courier(context, restaurant, unavailable)
        return self._build(context, restaurant, courier, unavailable, destination)

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

    def _build(
        self,
        context: dict,
        restaurant: dict | None,
        courier: dict | None,
        unavailable: list[str],
        destination: tuple[float, float] | None = None,
    ) -> dict:
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

        # What the platform estimated when the courier was assigned (recorded on that trail row). Kept
        # as an *estimate* and nothing more: it is a mock for the courier reaching the restaurant, so
        # it is never turned into "late by N minutes".
        for index, entry in enumerate(timeline):
            if entry["status"] == "assigned" and entry.get("estimated_courier_minutes") is not None:
                estimate = {
                    "courier_minutes": entry["estimated_courier_minutes"],
                    "courier_distance_km": entry.get("courier_distance_at_assignment_km"),
                }
                if status not in TERMINAL:
                    estimate["minutes_since_assignment"] = _minutes(
                        (datetime.now(timezone.utc) - stamps[index]).total_seconds()
                    )
                facts["estimate_at_assignment"] = estimate
                break

        # A food-app-style estimate of the time left (common/delivery.py), for an order still on its way. The
        # courier's distance is the one recorded at assignment; the trip to the customer is the real one if a
        # location was shared and a typical one otherwise. `minutes_remaining` takes the time already spent off
        # and floors at 0-5, so an order past its window reads "0 to 5 minutes", never a negative or a verdict.
        if status not in TERMINAL and "placed_minutes_ago" in facts:
            courier_km = (facts.get("estimate_at_assignment") or {}).get("courier_distance_km")
            trip_km = None
            if destination is not None and restaurant and restaurant.get("latitude") is not None:
                trip_km = haversine_km(
                    destination[0], destination[1], float(restaurant["latitude"]), float(restaurant["longitude"])
                )
                # The order was placed, so the platform accepted it: it cannot be "out of range". A location
                # beyond range is simply not where this order is going (orders store no address, and the page
                # sends where the customer is *now*), so it is ignored and a typical trip is assumed.
                if trip_km > MAX_DELIVERY_KM:
                    trip_km = None
            facts["delivery_estimate"] = minutes_remaining(
                delivery_estimate(courier_km, trip_km, (restaurant or {}).get("prep_time_minutes")),
                facts["placed_minutes_ago"],
            )

        # Splitting the wait. `confirmed` -> `assigned` lumps two things together: the restaurant taking
        # the order, then a courier being found. The kitchen's answer is not a trail row (accepting does
        # not move an order along its lifecycle), so its moment comes from the order itself. Both
        # timestamps are written by the same database, so they compare directly.
        decided_at = _parse(context["kitchen_decided_at"]) if context.get("kitchen_decided_at") else None
        confirmed_at = next((stamps[i] for i, e in enumerate(timeline) if e["status"] == "confirmed"), None)
        assigned_at = next((stamps[i] for i, e in enumerate(timeline) if e["status"] == "assigned"), None)
        if decided_at and confirmed_at and decided_at >= confirmed_at:
            facts["kitchen_response_minutes"] = _minutes((decided_at - confirmed_at).total_seconds())
        if context["kitchen_decision"] == "accepted" and decided_at and assigned_at and assigned_at >= decided_at:
            facts["courier_search_minutes"] = _minutes((assigned_at - decided_at).total_seconds())

        reasons = [entry["reason"] for entry in timeline if entry.get("reason")]
        if status in ("cancelled", "compensation_failed"):
            facts["cause"] = REASON_TEXT.get(reasons[-1]) if reasons else None
        # What happened to the customer's money, from the saga's own ordering (see REFUNDED_REASONS) —
        # never an amount, a time or a method: the gateway is simulated and the guardrail checks none of those.
        money = None
        if status == "compensation_failed":
            money = "refund_incomplete"
        elif status == "cancelled" and reasons:
            if reasons[-1] in REFUNDED_REASONS:
                money = "refunded"
            elif reasons[-1] == "payment_failed":
                money = "not_charged"
        facts["money"] = money
        stage = (context["saga"] or {}).get("fulfillment_stage")
        if status not in TERMINAL and stage in WAITING_TEXT:
            facts["waiting_on"] = WAITING_TEXT[stage]
            # How long it has been waiting on *that*: for the restaurant, since the order was confirmed;
            # for a courier, since the restaurant accepted.
            since = decided_at if stage == "dispatching_rider" else confirmed_at
            if since:
                facts["waiting_minutes"] = _minutes((datetime.now(timezone.utc) - since).total_seconds())
        if courier is not None:
            facts["courier"] = courier
        facts["unavailable"] = unavailable
        facts["plain_summary"] = render_summary(facts)
        return facts


def quantities_in(facts: dict) -> set[float]:
    """Every figure an explanation may quote: the numbers the facts carry, nothing derived."""
    figures: set[float] = set()
    for key in ("total_minutes", "placed_minutes_ago", "kitchen_response_minutes", "courier_search_minutes", "waiting_minutes"):
        if facts.get(key) is not None:
            figures.add(float(facts[key]))
    for step in facts["steps"]:
        figures.add(float(step["minutes_after_previous"]))
    courier = facts.get("courier") or {}
    if courier.get("distance_km") is not None:
        figures.add(float(courier["distance_km"]))
    estimate = facts.get("estimate_at_assignment") or {}
    for key in ("courier_minutes", "courier_distance_km", "minutes_since_assignment"):
        if estimate.get(key) is not None:
            figures.add(float(estimate[key]))
    window = facts.get("delivery_estimate") or {}
    for key in ("minutes_low", "minutes_high", "prep_minutes"):
        if window.get(key) is not None:
            figures.add(float(window[key]))
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
    if facts.get("money") == "refunded":
        parts.append("Your payment was refunded.")
    elif facts.get("money") == "not_charged":
        parts.append("You were not charged.")
    if facts.get("kitchen_response_minutes") is not None:
        if facts.get("kitchen_decision") == "accepted":
            parts.append(f"The restaurant took {_span(facts['kitchen_response_minutes'])} to accept the order.")
        else:
            parts.append(f"The restaurant responded after {_span(facts['kitchen_response_minutes'])}.")
    if facts.get("courier_search_minutes") is not None:
        parts.append(f"Finding a courier took {_span(facts['courier_search_minutes'])}.")
    if facts.get("waiting_on"):
        so_far = f" ({_span(facts['waiting_minutes'])} so far)" if facts.get("waiting_minutes") is not None else ""
        parts.append(f"It is currently waiting for {facts['waiting_on']}{so_far}.")
    if facts.get("placed_minutes_ago") is not None:
        parts.append(f"It was placed {_span(facts['placed_minutes_ago'])} ago.")
    if facts["status"] not in TERMINAL and not facts.get("delivery_estimate"):
        # Honest about why no verdict is given — and worded without any word `verify_no_verdict` rejects,
        # so this fallback can never fail its own check. With an estimate, its own "(an estimate, not a
        # promise)" says it.
        parts.append("No promised delivery time is on record to compare this against.")
    window = facts.get("delivery_estimate")
    if window:
        basis = " based on a typical trip" if window["trip_assumed"] else ""
        parts.append(
            f"The platform estimates about {window['minutes_low']} to {window['minutes_high']} more minutes"
            f"{basis} (an estimate, not a promise)."
        )
    estimate = facts.get("estimate_at_assignment")
    if estimate:
        minutes = f"{estimate['courier_minutes']:g}"
        parts.append(
            f"When the courier was assigned, the platform roughly estimated {minutes} minutes for them to "
            "reach the restaurant (an estimate, not a promise)."
        )
    courier = facts.get("courier")
    if courier and courier.get("distance_km") is not None:
        parts.append(f"The assigned courier is {courier['distance_km']} km from the restaurant.")
    if facts["unavailable"]:
        parts.append(f"I could not load: {', '.join(facts['unavailable'])}.")
    return " ".join(parts)


def render_history(facts: dict) -> str:
    """The order-history answer, written by rules from the facts alone (the fallback if the model is rejected).

    Every number it states is one of the facts' own counts, so it always passes `check_history`.
    """
    total = facts["total_orders"]
    parts = [
        f"You have {total} order{'' if total == 1 else 's'} in total: {facts['in_progress']} in progress, "
        f"{facts['delivered']} delivered and {facts['cancelled']} cancelled."
    ]
    shown = facts["recent_orders"]
    if shown:
        lines = []
        for order in shown:
            items = ", ".join(order["items"]) or "items not listed"
            lines.append(f"{order['restaurant']} ({items}, {order['total']}): {order['status']}, placed {order['placed']}")
        label = "Your most recent order is" if len(shown) == 1 else f"Your {len(shown)} most recent orders are"
        parts.append(f"{label}: " + "; ".join(lines) + ".")
    if facts["unavailable"]:
        parts.append(f"I could not load: {', '.join(facts['unavailable'])}.")
    return " ".join(parts)


def history_counts(facts: dict) -> set[int]:
    """The counts an order-history answer may state: the totals, and how many orders are listed."""
    return {
        facts["total_orders"],
        facts["in_progress"],
        facts["delivered"],
        facts["cancelled"],
        facts["recent_orders_shown"],
    }


def check_history(text: str, facts: dict):
    """Hold an order-history answer against its facts: every count, price and "N minutes" must be one the
    facts carry, and it may not judge an order late or on time.

    Not checked: which state belongs to which order. A model that said the wrong order was delivered would
    pass if the counts add up; the prompt forbids it and the ceiling is stated, as for food answers (D63).
    """
    from ai.llm.guardrails import verify_counts, verify_no_internal_terms, verify_no_verdict, verify_prices, verify_quantities

    prices = {float(order["total"].lstrip("$")) for order in facts["recent_orders"]}
    minutes = {float(o["placed_minutes_ago"]) for o in facts["recent_orders"] if o["placed_minutes_ago"] is not None}
    verdict = verify_counts(text, counts=history_counts(facts))
    for extra in (
        verify_prices(text, prices=prices),
        verify_quantities(text, quantities=minutes),
        verify_no_verdict(text),
        verify_no_internal_terms(text),
    ):
        verdict.ok = verdict.ok and extra.ok
        verdict.violations += extra.violations
    return verdict


def check_explanation(text: str, facts: dict):
    """Hold an explanation against its facts: every figure must be one the facts carry, and a claim about
    the customer's money (refunded, not charged) or how the order ended (cancelled, delivered) must be one the
    facts support.

    The one check both `explain` and the assistant's order path run, so the two cannot disagree about what
    counts as grounded.
    """
    from ai.llm.guardrails import (
        verify_cause,
        verify_money,
        verify_no_internal_terms,
        verify_no_verdict,
        verify_quantities,
        verify_status,
    )

    verdict = verify_quantities(text, quantities=quantities_in(facts))
    for extra in (
        verify_money(text, money=facts.get("money")),
        verify_status(text, status=facts["status"]),
        verify_cause(text, cause=facts.get("cause")),
        verify_no_verdict(text),
        verify_no_internal_terms(text),
    ):
        verdict.ok = verdict.ok and extra.ok
        verdict.violations += extra.violations
    return verdict


async def explain(model, facts: dict) -> tuple[str, bool]:
    """The model's explanation of `facts`, or the rule-written one if it cannot be trusted.

    Returns `(text, grounded)`. Imported lazily by callers that already hold a model; kept here,
    beside the facts it explains, so the assistant's order path and `POST /explain-order` cannot
    diverge in how they check an answer.
    """
    import json

    from langchain_core.messages import HumanMessage, SystemMessage

    from ai.assistant.agents import prompts
    from ai.llm.provider import call_model
    from ai.metrics import GUARDRAIL_VIOLATIONS_TOTAL

    text = await call_model(
        model,
        [SystemMessage(content=prompts.EXPLAIN), HumanMessage(content=f"FACTS: {json.dumps(facts, default=str)}")],
        node="explain",
    )
    text = text.strip()
    verdict = check_explanation(text, facts)
    for kind, _ in verdict.violations:
        GUARDRAIL_VIOLATIONS_TOTAL.labels(kind=kind).inc()
    if not text or not verdict.ok:
        return facts["plain_summary"], False
    return text, True
