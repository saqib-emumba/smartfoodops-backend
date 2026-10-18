"""Nearest-neighbour candidates from the document tables (Week 4, D59, reshaped by D66).

The tables hold only ids, chunk text and embeddings, so this repository can answer one question —
*which documents are closest in meaning to this vector?* — and nothing about price, availability,
tags or whether a restaurant is open. Those are read live from the Menu and Restaurant Services by
`ai.retrieval.retriever`, which asks this class for more candidates than it needs and filters them.

`min_score` bounds the cosine *distance*, the same expression the HNSW index orders by, so a
similarity floor costs nothing extra. `restaurant_id` scopes a search to one restaurant; that is
a filter, so the iterative scan (pgvector 0.8) is switched on to keep it from leaving the result
short. Both settings are `SET LOCAL`: they apply to this transaction only and never leak into
another request sharing the pooled connection.
"""

from common.repository import Repository
from ai import config
from ai.retrieval.embeddings import as_vector


def _settings(limit: int) -> tuple[str, ...]:
    # Ints from our own config and arguments, never user input — safe to format into a statement.
    # `ef_search` must be at least the LIMIT or the index cannot return that many rows.
    return (
        "SET LOCAL hnsw.iterative_scan = relaxed_order",
        f"SET LOCAL hnsw.ef_search = {max(int(config.HNSW_EF_SEARCH), int(limit))}",
    )


class VectorRepository(Repository):
    def nearest_items(
        self,
        vector: list[float],
        *,
        limit: int,
        restaurant_id: str | None = None,
        min_score: float | None = None,
    ) -> list[dict]:
        """`[{item_key, restaurant_id, score}]` nearest to `vector`, best first. `score` is cosine similarity."""
        return self._nearest(
            "menu_item_documents", "item_key, restaurant_id", vector, limit, restaurant_id, min_score
        )

    def nearest_restaurants(
        self,
        vector: list[float],
        *,
        limit: int,
        restaurant_id: str | None = None,
        min_score: float | None = None,
    ) -> list[dict]:
        """`[{restaurant_id, score}]` nearest to `vector`, best first."""
        return self._nearest("restaurant_documents", "restaurant_id", vector, limit, restaurant_id, min_score)

    def _nearest(self, table, columns, vector, limit, restaurant_id, min_score) -> list[dict]:
        clauses, params = [], {"vector": as_vector(vector), "limit": limit}
        if min_score is not None:
            # `1 - score` because `<=>` is cosine *distance*.
            clauses.append("embedding <=> %(vector)s::vector <= %(max_distance)s")
            params["max_distance"] = 1.0 - min_score
        if restaurant_id is not None:
            clauses.append("restaurant_id = %(restaurant_id)s")
            params["restaurant_id"] = str(restaurant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {columns}, 1 - (embedding <=> %(vector)s::vector) AS score
              FROM {table}
              {where}
             ORDER BY embedding <=> %(vector)s::vector
             LIMIT %(limit)s
        """
        with self._db.cursor() as cur:
            for statement in _settings(limit):
                cur.execute(statement)
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
