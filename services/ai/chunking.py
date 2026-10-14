"""Turning normalized menu rows into the natural-language text that gets embedded (Week 4, D59).

Pure functions, no I/O: given the menu tree and the restaurant record, produce strings. What an
embedding "knows" about a dish is exactly what is written here, so this is where retrieval
quality is decided — and where grounding is enforced. Every fact in a chunk comes from a field an
owner or the platform recorded; nothing is inferred, so a later answer built on a retrieved chunk
cannot cite a price, tag or availability the data does not hold.
"""

from decimal import Decimal


def _money(value) -> str:
    return f"${Decimal(str(value)).quantize(Decimal('0.01'))}"


def _options(groups: list[dict]) -> str:
    """`Cheese (cheddar +$1.50, none); Extras (bacon +$2.00)` — the choices a customer can make."""
    parts = []
    for group in groups:
        options = []
        for option in group.get("options", []):
            extra = option.get("extra_price") or 0
            options.append(
                f"{option['name']} +{_money(extra)}" if extra else option["name"]
            )
        if options:
            parts.append(f"{group['group_name']} ({', '.join(options)})")
    return "; ".join(parts)


def item_chunk(restaurant_name: str, category_name: str, item: dict) -> str:
    """One dish as a retrievable document, in the spec's `Dish: … | Category: …` shape.

    Segments with nothing to say are omitted rather than filled with a placeholder: a
    "Tags: none" segment would be an assertion, and an embedding of it would pull dishes with no
    declared tags toward queries that mention tags.
    """
    segments = [
        f"Dish: {item['name']} at {restaurant_name} ({_money(item['base_price'])})",
        f"Category: {category_name}",
    ]
    if item.get("description"):
        segments.append(f"Description: {item['description']}")
    options = _options(item.get("customization_groups", []))
    if options:
        segments.append(f"Options: {options}")
    if item.get("dietary_tags"):
        segments.append(f"Tags: {', '.join(item['dietary_tags'])}")
    segments.append("Availability: In Stock" if item.get("is_available") else "Availability: Out of Stock")
    return " | ".join(segments)


# A restaurant chunk names a few dishes so a query like "somewhere that does burgers" can match
# the restaurant itself, not only through its dishes. Capped so one huge menu cannot dilute it.
_SAMPLE_DISHES = 6


def restaurant_chunk(restaurant: dict, categories: list[dict]) -> str:
    """A restaurant as a retrievable document: where it is, what it serves, what it is tagged.

    Tags are the union of what its owners declared on its items — an aggregation of declared
    facts, not a guess about the kitchen. A restaurant with no tagged items simply has none.
    """
    items = [item for category in categories for item in category.get("items", [])]
    tags = sorted({tag for item in items for tag in item.get("dietary_tags", [])})
    available = [item["name"] for item in items if item.get("is_available")]

    segments = [f"Restaurant: {restaurant['name']}"]
    if restaurant.get("address"):
        segments.append(f"Address: {restaurant['address']}")
    names = [category["category_name"] for category in categories]
    if names:
        segments.append(f"Menu sections: {', '.join(names)}")
    if available:
        segments.append(f"Dishes include: {', '.join(available[:_SAMPLE_DISHES])}")
    if tags:
        segments.append(f"Tags: {', '.join(tags)}")
    segments.append("Status: Open" if restaurant.get("is_active") else "Status: Not accepting orders")
    return " | ".join(segments)
