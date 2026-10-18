"""Semantic search whose facts are read live (Week 4, D59, reshaped by D66).

The vector store knows what is *close in meaning*; it does not know what a dish costs, whether it
is in stock, what tags the owner declared or whether the restaurant is open — those belong to the
Menu and Restaurant Services and are read from them at search time, so a stale copy cannot be
quoted. The flow:

  1. nearest candidates by vector alone (`SEARCH_CANDIDATES` of them — deliberately more than
     `top_k`, because the filters below discard some);
  2. for the restaurants those candidates belong to, in rank order and a few at a time, read the
     live menu and restaurant (in parallel);
  3. keep a candidate only if its dish still exists and is available, within `max_price`, carries
     every requested tag, and its restaurant is active;
  4. stop as soon as `top_k` survive and no later restaurant could beat the k-th, or the
     candidates run out.

The trade against filtering inside the database: `top_k` is no longer guaranteed to be met when
many candidates are filtered out. If a first pass of `SEARCH_CANDIDATES` leaves it short and the
candidate list was full, one wider pass (`SEARCH_MAX_CANDIDATES`) is made; past that, fewer than
`top_k` results is what is returned. A restaurant or menu that no longer exists (a `404`) simply drops its candidates — the
vector was outliving its source. Any other failure of a sibling service propagates: a search that
silently skipped a restaurant it could not read would look like "nothing matches".

Rows are returned in the shape the response models already build from (`ItemMatch.from_row`,
`RestaurantMatch.row_fields`), so nothing downstream knows where the facts came from.
"""

from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException

from ai import config
from ai.clients.menu import MenuServiceClient
from ai.clients.restaurant import RestaurantServiceClient
from ai.repositories.vectors import VectorRepository


class _Live:
    """The live menu and restaurant for each restaurant id, read once per search."""

    def __init__(self, retriever: "Retriever"):
        self._retriever = retriever
        self._restaurants: dict[str, dict | None] = {}
        self._menus: dict[str, dict[str, tuple[str, dict]] | None] = {}

    def load(self, restaurant_ids: list[str]) -> None:
        missing = [rid for rid in restaurant_ids if rid not in self._restaurants]
        if not missing:
            return
        for rid, restaurant, menu in self._retriever.read_many(missing):
            self._restaurants[rid] = restaurant
            self._menus[rid] = None if menu is None else {
                item["item_id"]: (category["category_name"], item)
                for category in menu["categories"]
                for item in category["items"]
            }

    def restaurant(self, rid: str) -> dict | None:
        restaurant = self._restaurants.get(rid)
        return restaurant if restaurant is not None and restaurant.get("is_active", True) else None

    def menu(self, rid: str) -> dict[str, tuple[str, dict]]:
        return self._menus.get(rid) or {}


def _passes(item: dict, max_price: float | None, tags: list[str]) -> bool:
    if not item.get("is_available", True):
        return False
    if max_price is not None and float(item["base_price"]) > max_price:
        return False
    return set(tags) <= set(item.get("dietary_tags") or [])


class Retriever:
    def __init__(
        self,
        vectors: VectorRepository,
        menus: MenuServiceClient,
        restaurants: RestaurantServiceClient,
        *,
        pool: ThreadPoolExecutor | None = None,
    ):
        self._vectors = vectors
        self._menus = menus
        self._restaurants = restaurants
        self._pool = pool or ThreadPoolExecutor(
            max_workers=config.SEARCH_HYDRATE_WORKERS, thread_name_prefix="ai-hydrate"
        )

    # --- live reads -------------------------------------------------------------------------------

    def _read_one(self, rid: str):
        restaurant = menu = None
        try:
            restaurant = self._restaurants.get_restaurant(rid)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        if restaurant is not None:
            try:
                menu = self._menus.get_menu(rid)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
        return rid, restaurant, menu

    def read_many(self, restaurant_ids: list[str]):
        return list(self._pool.map(self._read_one, restaurant_ids))

    # --- search -----------------------------------------------------------------------------------

    def search(
        self,
        vector: list[float],
        *,
        top_k: int,
        max_price: float | None = None,
        tags: list[str] | None = None,
        restaurant_id: str | None = None,
        min_score: float | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """`(item_rows, restaurant_rows)`, best first, every fact read live."""
        tags = list(tags or [])
        live = _Live(self)  # shared by both passes, so a restaurant is read at most once
        limits = [config.SEARCH_CANDIDATES]
        if config.SEARCH_MAX_CANDIDATES > config.SEARCH_CANDIDATES:
            limits.append(config.SEARCH_MAX_CANDIDATES)
        for limit in limits:
            item_candidates = self._vectors.nearest_items(
                vector, limit=limit, restaurant_id=restaurant_id, min_score=min_score
            )
            restaurant_candidates = self._vectors.nearest_restaurants(
                vector, limit=limit, restaurant_id=restaurant_id, min_score=min_score
            )
            items = self._items(item_candidates, live, top_k, max_price, tags)
            restaurants = self._restaurant_rows(restaurant_candidates, live, top_k, max_price, tags)
            # A full result, or a candidate list that was not even full (so there is nothing more to
            # look at), is final. Otherwise the filters were selective enough to eat the candidates:
            # look again, wider, once.
            short = len(items) < top_k and len(item_candidates) == limit
            short_restaurants = len(restaurants) < top_k and len(restaurant_candidates) == limit
            if not (short or short_restaurants):
                break
        return items, restaurants

    def _items(self, candidates, live: _Live, top_k, max_price, tags) -> list[dict]:
        by_restaurant: dict[str, list[dict]] = {}
        for candidate in candidates:  # already best first, so each list is too
            by_restaurant.setdefault(str(candidate["restaurant_id"]), []).append(candidate)
        order = list(by_restaurant)  # restaurants by their best candidate
        found: list[dict] = []
        position = 0
        while position < len(order):
            wave = order[position : position + config.SEARCH_HYDRATE_WAVE]
            position += len(wave)
            live.load(wave)
            for rid in wave:
                found.extend(self._item_rows(rid, by_restaurant[rid], live, max_price, tags))
            found.sort(key=lambda row: row["score"], reverse=True)
            if len(found) >= top_k:
                # Stop unless a restaurant not yet read could still place a dish above the k-th.
                if position >= len(order) or found[top_k - 1]["score"] >= by_restaurant[order[position]][0]["score"]:
                    break
        return found[:top_k]

    def _item_rows(self, rid, candidates, live: _Live, max_price, tags) -> list[dict]:
        restaurant = live.restaurant(rid)
        if restaurant is None:
            return []
        menu = live.menu(rid)
        rows = []
        for candidate in candidates:
            entry = menu.get(candidate["item_key"])
            if entry is None:
                continue
            category_name, item = entry
            if not _passes(item, max_price, tags):
                continue
            rows.append(
                {
                    "item_key": candidate["item_key"],
                    "item_name": item["name"],
                    "restaurant_id": candidate["restaurant_id"],
                    "restaurant_name": restaurant["name"],
                    "category_name": category_name,
                    "base_price": item["base_price"],
                    "dietary_tags": list(item.get("dietary_tags") or []),
                    "latitude": restaurant.get("latitude"),
                    "longitude": restaurant.get("longitude"),
                    "score": candidate["score"],
                }
            )
        return rows

    def _restaurant_rows(self, candidates, live: _Live, top_k, max_price, tags) -> list[dict]:
        """Active restaurants with at least one dish a customer could order under the same filters."""
        rows: list[dict] = []
        position = 0
        while position < len(candidates) and len(rows) < top_k:
            wave = candidates[position : position + config.SEARCH_HYDRATE_WAVE]
            position += len(wave)
            live.load([str(c["restaurant_id"]) for c in wave])
            for candidate in wave:
                rid = str(candidate["restaurant_id"])
                restaurant = live.restaurant(rid)
                if restaurant is None:
                    continue
                if not any(_passes(item, max_price, tags) for _, item in live.menu(rid).values()):
                    continue
                rows.append(
                    {
                        "restaurant_id": candidate["restaurant_id"],
                        "name": restaurant["name"],
                        "address": restaurant.get("address"),
                        "latitude": restaurant.get("latitude"),
                        "longitude": restaurant.get("longitude"),
                        "score": candidate["score"],
                    }
                )
                if len(rows) == top_k:
                    break
        return rows
