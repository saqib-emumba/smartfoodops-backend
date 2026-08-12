"""MongoDB access for the `menus` and `order_tracking_logs` collections.

Every driver call is funnelled through here so that a PyMongo failure becomes a 503 in
exactly one place rather than in each route handler.
"""

from datetime import datetime, timezone
from uuid import UUID

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from datastores import DocumentStores


class MenuRepository:
    def __init__(self, stores: DocumentStores):
        self._stores = stores

    async def upsert_menu(self, restaurant_id: UUID, categories: list[dict]) -> dict:
        """Replace the category tree for one restaurant, creating the document if new."""
        now = datetime.now(timezone.utc)
        try:
            return await self._stores.db.menus.find_one_and_update(
                {"restaurant_id": str(restaurant_id)},
                {
                    "$set": {"categories": categories, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as exc:
            raise self._stores.unavailable(exc) from exc

    async def find_menu(self, restaurant_id: UUID) -> dict | None:
        try:
            return await self._stores.db.menus.find_one(
                {"restaurant_id": str(restaurant_id)}
            )
        except PyMongoError as exc:
            raise self._stores.unavailable(exc) from exc

    async def append_status_log(self, order_id: UUID, entry: dict) -> bool:
        """Append one status transition; returns whether a new document was created."""
        try:
            result = await self._stores.db.order_tracking_logs.update_one(
                {"order_id": str(order_id)},
                {"$push": {"status_history": entry}},
                upsert=True,
            )
        except PyMongoError as exc:
            raise self._stores.unavailable(exc) from exc
        return result.upserted_id is not None
