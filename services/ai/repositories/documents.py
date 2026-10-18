"""PostgreSQL access for the vector store's document tables (Week 4, D59, reshaped by D66).

The tables hold a chunk, its hash and its embedding, keyed by the Menu Service's own ids — no copy
of any name, price or tag. Ingestion therefore only has to decide one thing per document: has the
*text* changed? `stored_hashes` returns what is there; the caller embeds only what differs and
`apply` writes just that, plus the removal of dishes that left the menu, in one transaction. A
restaurant whose menu was republished unchanged costs a read and nothing else.

Embeddings go in as numpy arrays: `ai.deps.register_vector_types` registers pgvector's psycopg2
adapter once at startup, after which an array is a valid `vector` parameter. The `::vector` casts
below are kept as explicit documentation of the column type, not as a workaround.
"""

from psycopg2.extras import execute_values

from common.repository import Repository
from ai.retrieval.embeddings import as_vector

_EVENT_PROCESSED = """
    SELECT 1 AS seen FROM processed_events
     WHERE consumer_group = %(group)s AND event_id = %(event_id)s
"""

_MARK_PROCESSED = """
    INSERT INTO processed_events (consumer_group, event_id, event_type)
    VALUES (%(group)s, %(event_id)s, %(event_type)s)
    ON CONFLICT (consumer_group, event_id) DO NOTHING
"""

# What is stored for a restaurant, to decide what needs re-embedding. Comparing the model as well as
# the hash means switching `EMBEDDING_PROVIDER` re-embeds on the next backfill instead of leaving two
# incompatible vector spaces side by side.
_STORED_ITEMS = """
    SELECT item_key, chunk_hash, embedding_model
      FROM menu_item_documents
     WHERE restaurant_id = %s
"""
_STORED_RESTAURANT = """
    SELECT chunk_hash, embedding_model
      FROM restaurant_documents
     WHERE restaurant_id = %s
"""

_DELETE_ITEMS = "DELETE FROM menu_item_documents WHERE restaurant_id = %s"
_DELETE_SOME_ITEMS = "DELETE FROM menu_item_documents WHERE restaurant_id = %s AND item_key = ANY(%s)"
_DELETE_RESTAURANT = "DELETE FROM restaurant_documents WHERE restaurant_id = %s"

_UPSERT_ITEMS = """
    INSERT INTO menu_item_documents
        (restaurant_id, item_key, chunk_text, chunk_hash, embedding, embedding_model)
    VALUES %s
    ON CONFLICT (restaurant_id, item_key) DO UPDATE SET
        chunk_text = EXCLUDED.chunk_text,
        chunk_hash = EXCLUDED.chunk_hash,
        embedding = EXCLUDED.embedding,
        embedding_model = EXCLUDED.embedding_model,
        embedded_at = CURRENT_TIMESTAMP
"""
_ITEM_TEMPLATE = "(%s, %s, %s, %s, %s::vector, %s)"

_UPSERT_RESTAURANT = """
    INSERT INTO restaurant_documents
        (restaurant_id, chunk_text, chunk_hash, embedding, embedding_model)
    VALUES (%(restaurant_id)s, %(chunk_text)s, %(chunk_hash)s, %(embedding)s::vector, %(embedding_model)s)
    ON CONFLICT (restaurant_id) DO UPDATE SET
        chunk_text = EXCLUDED.chunk_text,
        chunk_hash = EXCLUDED.chunk_hash,
        embedding = EXCLUDED.embedding,
        embedding_model = EXCLUDED.embedding_model,
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

    def stored_hashes(self, restaurant_id: str) -> tuple[dict[str, dict], dict | None]:
        """`({item_key: {chunk_hash, embedding_model}}, restaurant {chunk_hash, embedding_model} | None)`."""
        items = {
            row["item_key"]: {"chunk_hash": row["chunk_hash"].strip(), "embedding_model": row["embedding_model"]}
            for row in self.all(_STORED_ITEMS, (restaurant_id,))
        }
        stored = self.one(_STORED_RESTAURANT, (restaurant_id,))
        if stored is not None:
            stored = {"chunk_hash": stored["chunk_hash"].strip(), "embedding_model": stored["embedding_model"]}
        return items, stored

    def apply(
        self,
        restaurant_id: str,
        *,
        items: list[dict],
        removed_keys: list[str],
        restaurant: dict | None,
        event: tuple[str, str, str] | None = None,
    ) -> None:
        """Write only what changed, and record `event` as processed, atomically.

        `items` are new or changed dishes (`item_key`, `chunk_text`, `chunk_hash`, `embedding`,
        `embedding_model`); `removed_keys` are dishes no longer on the menu; `restaurant` is the
        restaurant document if it changed, else None. `event` is `(group, event_id, event_type)`.
        Recording it in the same transaction as the write is what makes a crash between the two
        harmless: either both land or neither does, so a redelivered event can never be marked
        done without its effect, or applied twice.
        """
        with self._db.cursor(commit=True) as cur:
            if removed_keys:
                cur.execute(_DELETE_SOME_ITEMS, (restaurant_id, removed_keys))
            if items:
                execute_values(
                    cur,
                    _UPSERT_ITEMS,
                    [
                        (
                            restaurant_id,
                            doc["item_key"],
                            doc["chunk_text"],
                            doc["chunk_hash"],
                            as_vector(doc["embedding"]),
                            doc["embedding_model"],
                        )
                        for doc in items
                    ],
                    template=_ITEM_TEMPLATE,
                )
            if restaurant is not None:
                cur.execute(
                    _UPSERT_RESTAURANT,
                    {**restaurant, "restaurant_id": restaurant_id, "embedding": as_vector(restaurant["embedding"])},
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
