"""PostgreSQL access for `menus` and its four child tables.

One `menus` row per restaurant, normalized into categories -> items -> customization
groups -> options (D50). Publishing stays a full-tree replace — delete every existing
category for this menu (cascading through items/groups/options) and insert the new tree,
all inside one transaction, so two owners publishing at once still cannot interleave into
a half-updated menu. The `menus.restaurant_id` UNIQUE constraint is still what enforces
"one live menu per restaurant"; the engine, not the application.

`restaurant_id` is a plain UUID into the Restaurant Service's database; main.py verifies
it over HTTP before calling in here (see D02).
"""

from uuid import UUID

from common.repository import Repository

_UPSERT_MENU_ANCHOR = """
    INSERT INTO menus (restaurant_id)
    VALUES (%(restaurant_id)s)
    ON CONFLICT (restaurant_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
    RETURNING id
"""

_SELECT_MENU_BY_RESTAURANT = "SELECT id FROM menus WHERE restaurant_id = %s"

# Cascades through menu_items -> menu_item_customization_groups ->
# menu_item_customization_options via their own ON DELETE CASCADE foreign keys — one
# statement clears the whole tree.
_DELETE_CATEGORIES_FOR_MENU = "DELETE FROM menu_categories WHERE menu_id = %(menu_id)s"

_INSERT_CATEGORY = """
    INSERT INTO menu_categories (menu_id, category_key, name, display_order, position)
    VALUES (%(menu_id)s, %(category_key)s, %(name)s, %(display_order)s, %(position)s)
    RETURNING id
"""

_INSERT_ITEM = """
    INSERT INTO menu_items
        (category_id, item_key, name, description, base_price, is_available, dietary_flags, position)
    VALUES (%(category_id)s, %(item_key)s, %(name)s, %(description)s, %(base_price)s,
            %(is_available)s, %(dietary_flags)s, %(position)s)
    RETURNING id
"""

_INSERT_CUSTOMIZATION_GROUP = """
    INSERT INTO menu_item_customization_groups
        (item_id, group_key, name, min_selection, max_selection, position)
    VALUES (%(item_id)s, %(group_key)s, %(name)s, %(min_selection)s, %(max_selection)s, %(position)s)
    RETURNING id
"""

_INSERT_CUSTOMIZATION_OPTION = """
    INSERT INTO menu_item_customization_options (group_id, name, extra_price, position)
    VALUES (%(group_id)s, %(name)s, %(extra_price)s, %(position)s)
"""

_SELECT_CATEGORIES_FOR_MENU = """
    SELECT id, category_key, name, display_order
      FROM menu_categories
     WHERE menu_id = %s
     ORDER BY position
"""

_SELECT_ITEMS_FOR_CATEGORIES = """
    SELECT id, category_id, item_key, name, description, base_price, is_available, dietary_flags
      FROM menu_items
     WHERE category_id = ANY(%(category_ids)s::uuid[])
     ORDER BY position
"""

_SELECT_GROUPS_FOR_ITEMS = """
    SELECT id, item_id, group_key, name, min_selection, max_selection
      FROM menu_item_customization_groups
     WHERE item_id = ANY(%(item_ids)s::uuid[])
     ORDER BY position
"""

_SELECT_OPTIONS_FOR_GROUPS = """
    SELECT group_id, name, extra_price
      FROM menu_item_customization_options
     WHERE group_id = ANY(%(group_ids)s::uuid[])
     ORDER BY position
"""


class MenuRepository(Repository):
    def find(self, restaurant_id: UUID) -> dict | None:
        menu = self.one(_SELECT_MENU_BY_RESTAURANT, (str(restaurant_id),))
        if menu is None:
            return None
        return {
            "restaurant_id": restaurant_id,
            "categories": self._load_categories(menu["id"]),
        }

    def _load_categories(self, menu_id) -> list[dict]:
        """Reassemble the nested category/item/group/option tree for one menu.

        Four queries, one per level, each batched over every id its parent level
        produced — not one query per row, which would cost a query per item on top of
        one per category. `position` (set at publish time, see `upsert`) is what makes
        this reconstruction order-faithful to whatever order the tree was submitted in,
        independent of the client's own `display_order` field.
        """
        category_rows = self.all(_SELECT_CATEGORIES_FOR_MENU, (menu_id,))
        category_ids = [row["id"] for row in category_rows]
        item_rows = (
            self.all(_SELECT_ITEMS_FOR_CATEGORIES, {"category_ids": category_ids})
            if category_ids
            else []
        )
        item_ids = [row["id"] for row in item_rows]
        group_rows = (
            self.all(_SELECT_GROUPS_FOR_ITEMS, {"item_ids": item_ids}) if item_ids else []
        )
        group_ids = [row["id"] for row in group_rows]
        option_rows = (
            self.all(_SELECT_OPTIONS_FOR_GROUPS, {"group_ids": group_ids})
            if group_ids
            else []
        )

        options_by_group: dict[str, list[dict]] = {}
        for row in option_rows:
            options_by_group.setdefault(str(row["group_id"]), []).append(
                {"name": row["name"], "extra_price": float(row["extra_price"])}
            )

        groups_by_item: dict[str, list[dict]] = {}
        for row in group_rows:
            groups_by_item.setdefault(str(row["item_id"]), []).append(
                {
                    "group_id": row["group_key"],
                    "group_name": row["name"],
                    "min_selection": row["min_selection"],
                    "max_selection": row["max_selection"],
                    "options": options_by_group.get(str(row["id"]), []),
                }
            )

        items_by_category: dict[str, list[dict]] = {}
        for row in item_rows:
            items_by_category.setdefault(str(row["category_id"]), []).append(
                {
                    "item_id": row["item_key"],
                    "name": row["name"],
                    "description": row["description"],
                    "base_price": float(row["base_price"]),
                    "is_available": row["is_available"],
                    "dietary_flags": list(row["dietary_flags"]),
                    "customization_groups": groups_by_item.get(str(row["id"]), []),
                }
            )

        return [
            {
                "category_id": row["category_key"],
                "category_name": row["name"],
                "display_order": row["display_order"],
                "items": items_by_category.get(str(row["id"]), []),
            }
            for row in category_rows
        ]

    def upsert(self, restaurant_id: UUID, categories: list[dict]) -> dict:
        """Replace the category tree for one restaurant, inserting it if it is new.

        Delete-then-reinsert rather than a diff against the existing tree: a publish is
        already a full replace at the API layer (`MenuUpsertRequest` takes the whole
        tree), so there is no partial state to preserve, and it is what keeps this one
        transaction instead of a reconciliation pass.

        Returns the tree exactly as submitted rather than re-reading it back — this
        transaction is the source of truth for what is now stored, so there is nothing a
        second read could tell the caller that this call does not already know.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(_UPSERT_MENU_ANCHOR, {"restaurant_id": str(restaurant_id)})
            menu_id = cur.fetchone()["id"]

            cur.execute(_DELETE_CATEGORIES_FOR_MENU, {"menu_id": menu_id})

            for category_position, category in enumerate(categories):
                cur.execute(
                    _INSERT_CATEGORY,
                    {
                        "menu_id": menu_id,
                        "category_key": category["category_id"],
                        "name": category["category_name"],
                        "display_order": category["display_order"],
                        "position": category_position,
                    },
                )
                category_id = cur.fetchone()["id"]

                for item_position, item in enumerate(category["items"]):
                    cur.execute(
                        _INSERT_ITEM,
                        {
                            "category_id": category_id,
                            "item_key": item["item_id"],
                            "name": item["name"],
                            "description": item["description"],
                            "base_price": item["base_price"],
                            "is_available": item["is_available"],
                            "dietary_flags": item["dietary_flags"],
                            "position": item_position,
                        },
                    )
                    item_id = cur.fetchone()["id"]

                    for group_position, group in enumerate(item["customization_groups"]):
                        cur.execute(
                            _INSERT_CUSTOMIZATION_GROUP,
                            {
                                "item_id": item_id,
                                "group_key": group["group_id"],
                                "name": group["group_name"],
                                "min_selection": group["min_selection"],
                                "max_selection": group["max_selection"],
                                "position": group_position,
                            },
                        )
                        group_id = cur.fetchone()["id"]

                        for option_position, option in enumerate(group["options"]):
                            cur.execute(
                                _INSERT_CUSTOMIZATION_OPTION,
                                {
                                    "group_id": group_id,
                                    "name": option["name"],
                                    "extra_price": option["extra_price"],
                                    "position": option_position,
                                },
                            )

        return {"restaurant_id": restaurant_id, "categories": categories}
