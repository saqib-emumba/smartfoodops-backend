"""The controlled vocabulary for a menu item's tags (Week 4, D60).

One list, in a field still named `dietary_tags` because that is what the spec calls it, but it
holds more than diets: dietary restrictions and allergens, cuisines, formats, dish types and
meal styles. Shared by the Menu Service, which validates what an owner declares, and the AI
Service, which validates what a customer filters by — because the point of a tag filter is
that "keto" means the same string on both sides, and a filter for a tag no menu can carry would
silently return nothing.

Owners declare tags; nothing infers them (D60). A tag the platform guessed from a dish's name
("spicy" from "jalapeño") is exactly the kind of fact a zero-hallucination assistant must not
serve as one.

Two things follow from mixing the groups in one field, worth knowing before filtering on it:
  - The restriction and allergen groups are the restaurant's word, not a guarantee, and
    anything that shows them to a customer should say so.
  - A filter requires *every* tag asked for (array containment). Asking for two cuisines at once
    matches only an item tagged with both, so a client should send one cuisine per search.
"""

# Grouped by what a customer is filtering *for*. Extend rather than edit: removing or renaming a
# tag after owners have used it orphans stored items.
DIETARY_TAGS: tuple[str, ...] = (
    # Diets
    "vegan",
    "vegetarian",
    "pescatarian",
    "keto",
    "low-carb",
    # Religious
    "halal",
    "kosher",
    # Allergen-free
    "gluten-free",
    "dairy-free",
    "nut-free",
    "egg-free",
    "soy-free",
    "shellfish-free",
    # Cuisine
    "pakistani",
    "desi",
    "indian",
    "arabic",
    "turkish",
    "italian",
    "chinese",
    "thai",
    "japanese",
    "mexican",
    "american",
    "continental",
    # Format
    "fast-food",
    "bbq",
    # Dish type
    "steak",
    "burger",
    "pizza",
    "pasta",
    "sandwich",
    "biryani",
    "karahi",
    "sushi",
    # Meal style
    "light-meal",
    "comfort-meal",
    "hot",
    "warm",
    "spicy",
)


def normalize_dietary_tags(raw: list[str]) -> list[str]:
    """Lower-case, trim and de-duplicate `raw`, keeping first-seen order.

    Raises `ValueError` naming every tag outside the vocabulary — callers turn that into a 422,
    so a typo ("gluten free") is rejected at the door instead of stored and never matched.
    """
    seen: list[str] = []
    unknown: list[str] = []
    for tag in raw:
        cleaned = tag.strip().lower()
        if cleaned not in DIETARY_TAGS:
            unknown.append(tag)
        elif cleaned not in seen:
            seen.append(cleaned)
    if unknown:
        raise ValueError(
            f"Unknown dietary tag(s) {unknown}; allowed: {', '.join(DIETARY_TAGS)}"
        )
    return seen
