"""What the generation step is allowed to say, assembled from a RAG response (D63).

`build_food_context` reduces `RagContextResponse` to names, prices and counts — no ids, no
coordinates, nothing a prompt does not need. Alongside the prompt-facing dictionary it returns the
sets the guardrail checks the answer against (names, prices) and the catalogues that map a name the
answer mentions back to an id, so the dishes a customer was *actually shown* can be recorded.

`render_food_fallback` is the answer used when the model is bypassed or its answer is rejected. It
is written from the same context by rules, so it contains only names and prices that exist.
"""

from dataclasses import dataclass, field

from ai import config
from ai.llm.guardrails import normalize
from ai.schemas.rag import RagContextResponse


@dataclass
class FoodContext:
    prompt: dict
    names: set[str] = field(default_factory=set)
    prices: set[float] = field(default_factory=set)
    item_ids: dict[str, str] = field(default_factory=dict)
    restaurant_ids: dict[str, str] = field(default_factory=dict)
    # (dish, restaurant, price), names normalised: what the guardrail checks pairings against.
    offers: list[tuple[str, str, float]] = field(default_factory=list)
    budget: float | None = None
    # Every "N minutes" / "N km" the context carries: the courier estimates and distances. What the guardrail
    # lets an answer quote.
    quantities: set[float] = field(default_factory=set)


def _distinct(rows, key):
    """`rows` without repeats of `key`, first (best-ranked) occurrence kept.

    Retrieval ranks by similarity, so several restaurants serving an identically named dish at the same price
    arrive as consecutive, identical-looking rows and can fill the whole top-K. The model then sees one option
    six times. Found when a test database full of identical test kitchens made every generic question return
    the same salad six times.
    """
    seen, out = set(), []
    for row in rows:
        k = key(row)
        if k not in seen:
            seen.add(k)
            out.append(row)
    return out


def build_food_context(body: RagContextResponse, ratings: dict[str, dict], max_price: float | None) -> FoodContext:
    """`ratings` maps restaurant id to `{average_rating, review_count}` for restaurant questions."""
    restaurant_names: dict[str, str] = {}
    for item in body.items:
        restaurant_names[str(item.restaurant_id)] = item.restaurant_name
    for restaurant in body.restaurants:
        restaurant_names[str(restaurant.restaurant_id)] = restaurant.name

    context = FoodContext(prompt={})
    items = []
    for item in _distinct(body.items, lambda i: (normalize(i.item_name), normalize(i.restaurant_name), round(i.price, 2)))[
        : config.ASSISTANT_TOP_K
    ]:
        items.append(
            {
                "name": item.item_name,
                "restaurant": item.restaurant_name,
                # Dollars and cents as text with the symbol, so a model copies "$12.00" — not "12.0", and not a bare
                # number the price check would have to guess at.
                "price": f"${item.price:.2f}",
                "category": item.category_name,
                "tags": item.dietary_tags,
            }
        )
        context.names.add(normalize(item.item_name))
        context.names.add(normalize(item.restaurant_name))
        context.prices.add(round(item.price, 2))
        context.offers.append((normalize(item.item_name), normalize(item.restaurant_name), round(item.price, 2)))
        context.item_ids[normalize(item.item_name)] = item.item_id
        context.restaurant_ids[normalize(item.restaurant_name)] = str(item.restaurant_id)

    restaurants = []
    for restaurant in _distinct(body.restaurants, lambda r: (normalize(r.name), normalize(r.address or "")))[
        : config.ASSISTANT_TOP_K
    ]:
        entry = {"name": restaurant.name, "address": restaurant.address, "distance_km": restaurant.distance_km}
        if restaurant.distance_km is not None:
            context.quantities.add(float(restaurant.distance_km))
        stats = ratings.get(str(restaurant.restaurant_id))
        if stats and stats["review_count"]:
            entry["average_rating"] = stats["average_rating"]
            entry["review_count"] = stats["review_count"]
        restaurants.append(entry)
        context.names.add(normalize(restaurant.name))
        context.restaurant_ids[normalize(restaurant.name)] = str(restaurant.restaurant_id)

    profile = body.customer_profile
    customer = None
    if profile is not None and profile.total_orders:
        favourites = [v.name for v in profile.favourite_vendors if v.name]
        customer = {"previous_orders": profile.total_orders, "favourite_restaurants": favourites}
        context.names.update(normalize(name) for name in favourites)

    # Only for restaurants the context actually shows: a courier line for a restaurant that was dropped (as a
    # duplicate, or below the top few) would hand the model a name it has nothing else to say about.
    shown = {normalize(r["restaurant"]) for r in items} | {normalize(r["name"]) for r in restaurants}
    couriers = _distinct(
        [
            {
                "restaurant": restaurant_names[str(c.restaurant_id)],
                "available_riders": c.available_riders,
                "nearest_km": c.nearest_available_km,
                "delivery_estimate": c.delivery_estimate,
            }
            for c in body.courier_availability
            if str(c.restaurant_id) in restaurant_names
            and normalize(restaurant_names[str(c.restaurant_id)]) in shown
        ],
        lambda c: (normalize(c["restaurant"]), c["available_riders"], c["nearest_km"]),
    )
    for c in couriers:
        if c["nearest_km"] is not None:
            context.quantities.add(float(c["nearest_km"]))
        for key in ("minutes_low", "minutes_high", "prep_minutes"):
            if (c["delivery_estimate"] or {}).get(key) is not None:
                context.quantities.add(float(c["delivery_estimate"][key]))

    context.budget = max_price
    if max_price is not None:
        context.prices.add(round(max_price, 2))  # restating the customer's own budget is not inventing
    context.prompt = {
        "items": items,
        "restaurants": restaurants,
        "customer": customer,
        "couriers": couriers,
        "sources_failed": body.sources_failed,
    }
    return context


def render_food_fallback(context: FoodContext) -> str:
    prompt = context.prompt
    if prompt["items"]:
        # Distinct, because two menus (or two entries) can carry an identical dish and price.
        lines = list(
            dict.fromkeys(
                f"**{item['name']}** at **{item['restaurant']}** ({item['price']})"
                for item in prompt["items"]
            )
        )[:3]
        text = "Here is what is on the menus right now: " + "; ".join(lines) + "."
    elif prompt["restaurants"]:
        names = ", ".join(f"**{r['name']}**" for r in prompt["restaurants"][:3])
        text = f"These restaurants may suit you: {names}."
    else:
        text = "I could not find anything matching that on the current menus."
    if prompt["sources_failed"]:
        text += f" Some information could not be loaded ({', '.join(prompt['sources_failed'])})."
    return text


NO_RESULTS = (
    "I couldn't find anything on the current menus that matches that. "
    "You could try a different cuisine, a higher budget, or fewer dietary filters."
)
SEARCH_UNAVAILABLE = "I couldn't search the menus just now. Please try again in a moment."
NO_ORDERS = "I can't see any orders on your account yet."
REFUSAL = (
    "I can help with food recommendations, your orders and questions about restaurants on "
    "SmartFoodOps. What would you like to eat?"
)
