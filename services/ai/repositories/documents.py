"""PostgreSQL access for the vector store's document tables (Week 4, D59).

Writes are a per-restaurant *replace*: delete the restaurant's item documents and insert the new
set, in one transaction, matching the Menu Service's own publish semantics (a full replace, D50).
A diff against the existing rows would have to reason about renamed, re-keyed and removed items;
replacing sidesteps all three, and the cost — re-embedding every item of a restaurant — is small
at a menu's size and only paid when its `source_version` actually changed.

Embeddings go in as numpy arrays: `ai.deps.register_vector_types` registers pgvector's psycopg2
adapter once at startup, after which an array is a valid `vector` parameter. The `::vector` casts
below are kept as explicit documentation of the column type, not as a workaround.
"""

from psycopg2.extras import execute_values

from common.repository import Repository
from ai.embeddings import as_vector

_EVENT_PROCESSED = """
    SELECT 1 AS seen FROM processed_events
     WHERE consumer_group = %(group)s AND event_id = %(event_id)s
"""

_MARK_PROCESSED = """
    INSERT INTO processed_events (consumer_group, event_id, event_type)
    VALUES (%(group)s, %(event_id)s, %(event_type)s)
    ON CONFLICT (consumer_group, event_id) DO NOTHING
"""

# The version a restaurant's documents were built from, and by which model. Comparing the model
# too means switching `EMBEDDING_PROVIDER` re-embeds on the next backfill instead of leaving two
# incompatible vector spaces side by side.
_CURRENT_VERSION = """
    SELECT source_version, embedding_model
      FROM restaurant_documents
     WHERE restaurant_id = %s
"""

_DELETE_ITEMS = "DELETE FROM menu_item_documents WHERE restaurant_id = %s"
_DELETE_RESTAURANT = "DELETE FROM restaurant_documents WHERE restaurant_id = %s"

_INSERT_ITEMS = """
    INSERT INTO menu_item_documents
        (restaurant_id, item_key, category_key, category_name, item_name, restaurant_name,
         base_price, is_available, dietary_tags, chunk_text, embedding, embedding_model,
         source_version)
    VALUES %s
"""
_ITEM_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s::timestamptz)"

_UPSERT_RESTAURANT = """
    INSERT INTO restaurant_documents
        (restaurant_id, name, address, latitude, longitude, is_active, chunk_text, embedding,
         embedding_model, source_version)
    VALUES (%(restaurant_id)s, %(name)s, %(address)s, %(latitude)s, %(longitude)s,
            %(is_active)s, %(chunk_text)s, %(embedding)s::vector, %(embedding_model)s,
            %(source_version)s::timestamptz)
    ON CONFLICT (restaurant_id) DO UPDATE SET
        name = EXCLUDED.name,
        address = EXCLUDED.address,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        is_active = EXCLUDED.is_active,
        chunk_text = EXCLUDED.chunk_text,
        embedding = EXCLUDED.embedding,
        embedding_model = EXCLUDED.embedding_model,
        source_version = EXCLUDED.source_version,
        embedded_at = CURRENT_TIMESTAMP
"""

_COUNT_ITEMS = "SELECT count(*) AS n FROM menu_item_documents"
_COUNT_RESTAURANTS = "SELECT count(*) AS n FROM restaurant_documents"


class DocumentRepository(Repository):
    def event_processed(self, group: str, event_id: str) -> bool:
        return self.one(_EVENT_PROCESSED, {"group": group, "event_id": event_id}) is not None

    def mark_processed(self, group: str, event_id: str, event_type: str) -> None:
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _MARK_PROCESSED, {"group": group, "event_id": event_id, "event_type": event_type}
            )

    def current_version(self, restaurant_id: str) -> dict | None:
        """`{source_version, embedding_model}` this restaurant was last indexed under, or None."""
        return self.one(_CURRENT_VERSION, (restaurant_id,))

    def replace_restaurant(
        self,
        restaurant: dict,
        items: list[dict],
        *,
        event: tuple[str, str, str] | None = None,
    ) -> None:
        """Replace one restaurant's documents, and record `event` as processed, atomically.

        `event` is `(group, event_id, event_type)`. Recording it in the same transaction as the
        write is what makes a crash between the two harmless: either both land or neither does,
        so a redelivered event can never be marked done without its effect, or applied twice.
        """
        with self._db.cursor(commit=True) as cur:
            restaurant_id = restaurant["restaurant_id"]
            cur.execute(_DELETE_ITEMS, (restaurant_id,))
            if items:
                execute_values(
                    cur,
                    _INSERT_ITEMS,
                    [
                        (
                            restaurant_id,
                            doc["item_key"],
                            doc["category_key"],
                            doc["category_name"],
                            doc["item_name"],
                            doc["restaurant_name"],
                            doc["base_price"],
                            doc["is_available"],
                            doc["dietary_tags"],
                            doc["chunk_text"],
                            as_vector(doc["embedding"]),
                            doc["embedding_model"],
                            doc["source_version"],
                        )
                        for doc in items
                    ],
                    template=_ITEM_TEMPLATE,
                )
            cur.execute(
                _UPSERT_RESTAURANT,
                {**restaurant, "embedding": as_vector(restaurant["embedding"])},
            )
            if event is not None:
                group, event_id, event_type = event
                cur.execute(
                    _MARK_PROCESSED,
                    {"group": group, "event_id": event_id, "event_type": event_type},
                )

    def remove_restaurant(self, restaurant_id: str) -> None:
        """Drop every document for a restaurant whose menu no longer exists."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(_DELETE_ITEMS, (restaurant_id,))
            cur.execute(_DELETE_RESTAURANT, (restaurant_id,))

    def counts(self) -> dict[str, int]:
        return {
            "menu_item": self.one(_COUNT_ITEMS)["n"],
            "restaurant": self.one(_COUNT_RESTAURANTS)["n"],
        }
