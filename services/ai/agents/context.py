"""What the generation step is allowed to say, assembled from a RAG response (D63).

`build_food_context` reduces `RagContextResponse` to names, prices and counts — no ids, no
coordinates, nothing a prompt does not need. Alongside the prompt-facing dictionary it returns the
sets the guardrail checks the answer against (names, prices) and the catalogues that map a name the
answer mentions back to an id, so the dishes a customer was *actually shown* can be recorded.

`render_food_fallback` is the answer used when the model is bypassed or its answer is rejected. It
is written from the same context by rules, so it contains only names and prices that exist.
"""

from dataclasses import dataclass, field

from ai.guardrails import normalize
from ai.schemas.rag import RagContextResponse


@dataclass
class FoodContext:
    prompt: dict
    names: set[str] = field(default_factory=set)
    prices: set[float] = field(default_factory=set)
    item_ids: dict[str, str] = field(default_factory=dict)
    restaurant_ids: dict[str, str] = field(default_factory=dict)


def build_food_context(body: RagContextResponse, ratings: dict[str, dict], max_price: float | None) -> FoodContext:
    """`ratings` maps restaurant id to `{average_rating, review_count}` for restaurant questions."""
    restaurant_names: dict[str, str] = {}
    for item in body.items:
        restaurant_names[str(item.restaurant_id)] = item.restaurant_name
    for restaurant in body.restaurants:
        restaurant_names[str(restaurant.restaurant_id)] = restaurant.name

    context = FoodContext(prompt={})
    items = []
    for item in body.items:
        items.append(
            {
                "name": item.item_name,
                "restaurant": item.restaurant_name,
                "price": item.price,
                "category": item.category_name,
                "tags": item.dietary_tags,
            }
        )
        context.names.add(normalize(item.item_name))
        context.names.add(normalize(item.restaurant_name))
        context.prices.add(round(item.price, 2))
        context.item_ids[normalize(item.item_name)] = item.item_id
        context.restaurant_ids[normalize(item.restaurant_name)] = str(item.restaurant_id)

    restaurants = []
    for restaurant in body.restaurants:
        entry = {"name": restaurant.name, "address": restaurant.address, "distance_km": restaurant.distance_km}
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

    couriers = [
        {
            "restaurant": restaurant_names[str(c.restaurant_id)],
            "available_riders": c.available_riders,
            "nearest_km": c.nearest_available_km,
        }
        for c in body.courier_availability
        if str(c.restaurant_id) in restaurant_names
    ]

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
                f"**{item['name']}** at **{item['restaurant']}** (${item['price']:.2f})"
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
