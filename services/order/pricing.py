"""Server-side re-pricing of a checkout against the restaurant's published menu.

The client's `total_amount` is never trusted: every line is recomputed from the menu the
Menu Service currently serves, and the order is rejected if the two disagree. Money is
handled as Decimal and only converted to float at the persistence boundary.
"""

from collections.abc import Iterable
from decimal import Decimal

from common.errors import unprocessable
from schemas import OrderCreateRequest, OrderItemSelection

# Currency resolution used for every rounding step.
CENTS = Decimal("0.01")


def flatten_catalogue(menu: dict) -> dict:
    """Index every menu item by item_id across all categories."""
    return {
        item["item_id"]: item
        for category in menu.get("categories", [])
        for item in category.get("items", [])
    }


def selected_names(raw) -> list[str]:
    """Normalise a customization selection into a list of option names.

    Accepts a bare name, a single option object, or a list of either.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, Iterable):
        raise unprocessable(f"Unsupported customization selection: {raw!r}")

    names = []
    for entry in raw:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and "name" in entry:
            names.append(entry["name"])
        else:
            raise unprocessable(f"Unsupported customization selection: {entry!r}")
    return names


def _apply_customizations(
    item: dict, selection: OrderItemSelection
) -> tuple[Decimal, list[dict]]:
    """Validate each chosen group against the menu and total up the option surcharges."""
    groups = {g["group_id"]: g for g in item.get("customization_groups", [])}
    customizations = selection.customizations or {}
    surcharge = Decimal("0.00")
    chosen_options: list[dict] = []

    for group_id, raw in customizations.items():
        group = groups.get(group_id)
        if group is None:
            raise unprocessable(
                f"Item '{selection.item_id}' has no customization group '{group_id}'"
            )

        names = selected_names(raw)
        if not group["min_selection"] <= len(names) <= group["max_selection"]:
            raise unprocessable(
                f"Group '{group_id}' on item '{selection.item_id}' accepts between "
                f"{group['min_selection']} and {group['max_selection']} selections, got {len(names)}"
            )

        options = {option["name"]: option for option in group["options"]}
        for name in names:
            option = options.get(name)
            if option is None:
                raise unprocessable(
                    f"Option '{name}' is not offered by group '{group_id}' on item '{selection.item_id}'"
                )
            extra = Decimal(str(option.get("extra_price", 0)))
            surcharge += extra
            chosen_options.append(
                {"group_id": group_id, "name": name, "extra_price": float(extra)}
            )

    # A required group the client omitted entirely is just as invalid as an empty one.
    for group_id, group in groups.items():
        if group["min_selection"] > 0 and group_id not in customizations:
            raise unprocessable(
                f"Group '{group_id}' on item '{selection.item_id}' requires at least "
                f"{group['min_selection']} selection(s)"
            )

    return surcharge, chosen_options


def price_line(item: dict, selection: OrderItemSelection) -> tuple[dict, Decimal]:
    """Recalculate one line's price from the menu, rejecting anything inconsistent."""
    if not item.get("is_available", False):
        raise unprocessable(f"Item '{selection.item_id}' is currently unavailable")

    surcharge, chosen_options = _apply_customizations(item, selection)
    unit_price = (Decimal(str(item["base_price"])) + surcharge).quantize(CENTS)
    line_total = (unit_price * selection.quantity).quantize(CENTS)

    snapshot = {
        "item_id": selection.item_id,
        "name": item.get("name"),
        "quantity": selection.quantity,
        "customizations": selection.customizations,
        "unit_price": float(unit_price),
        "line_total": float(line_total),
        "selected_options": chosen_options,
    }
    return snapshot, line_total


def build_order_snapshot(
    menu: dict, payload: OrderCreateRequest
) -> tuple[list[dict], Decimal]:
    """Validate availability and recompute the authoritative total for the checkout."""
    catalogue = flatten_catalogue(menu)
    snapshot: list[dict] = []
    total = Decimal("0.00")

    for selection in payload.items:
        item = catalogue.get(selection.item_id)
        if item is None:
            raise unprocessable(
                f"Item '{selection.item_id}' is not on the restaurant's active menu"
            )
        line, line_total = price_line(item, selection)
        snapshot.append(line)
        total += line_total

    total = total.quantize(CENTS)
    claimed = Decimal(str(payload.total_amount)).quantize(CENTS)
    if total != claimed:
        raise unprocessable(
            f"total_amount mismatch: client sent {claimed}, server recalculated {total}"
        )
    return snapshot, total
