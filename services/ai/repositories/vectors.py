"""Hybrid vector + SQL search over the document tables (Week 4, D59).

One statement per result kind, each combining a cosine ordering (`<=>`, served by the HNSW index)
with ordinary WHERE clauses on the structured metadata. Doing both in the database — rather than
fetching nearest neighbours and filtering them in Python — is what keeps `top_k` honest: the
limit applies to rows that already satisfy the constraints.

**Filtered HNSW.** An HNSW scan visits a fixed number of candidates (`ef_search`) and *then*
the WHERE clause removes some. With a selective filter that can leave fewer than `top_k` rows
even though more matches exist. pgvector 0.8's iterative scan fixes this: with
`hnsw.iterative_scan` on, the scan keeps walking the graph until the LIMIT is satisfied or
`hnsw.max_scan_tuples` is reached. Both settings are `SET LOCAL`, so they apply to this
transaction only and never leak into another request sharing the pooled connection.
"""

from common.repository import Repository
from ai import config
from ai.embeddings import as_vector

_SETTINGS = (
    "SET LOCAL hnsw.iterative_scan = relaxed_order",
    # An int from our own config, never user input — safe to format into the statement.
    f"SET LOCAL hnsw.ef_search = {int(config.HNSW_EF_SEARCH)}",
)


def _item_predicates(alias: str, *, max_price, tags) -> tuple[str, dict]:
    """The structured constraints, as SQL against `alias` plus their parameters."""
    clauses = [f"{alias}.is_available"]
    params: dict = {}
    if max_price is not None:
        clauses.append(f"{alias}.base_price <= %(max_price)s")
        params["max_price"] = max_price
    if tags:
        clauses.append(f"{alias}.dietary_tags @> %(tags)s::text[]")
        params["tags"] = list(tags)
    return " AND ".join(clauses), params


class VectorRepository(Repository):
    def search(
        self,
        vector: list[float],
        *,
        top_k: int,
        max_price: float | None = None,
        tags: list[str] | None = None,
        restaurant_id: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """`(item_matches, restaurant_matches)` nearest to `vector`, best first.

        Items must be available, within `max_price`, carry every tag in `tags`, and belong to an
        active restaurant. Restaurants must be active and have at least one item that passes the
        same constraints — a restaurant with nothing the customer could actually order under
        their filters is not a useful result. `score` is cosine similarity (1 - distance).
        """
        item_where, params = _item_predicates("d", max_price=max_price, tags=tags)
        params.update({"vector": as_vector(vector), "top_k": top_k})
        item_scope = ""
        restaurant_scope = ""
        if restaurant_id is not None:
            item_scope = "AND d.restaurant_id = %(restaurant_id)s"
            restaurant_scope = "AND r.restaurant_id = %(restaurant_id)s"
            params["restaurant_id"] = str(restaurant_id)

        items_sql = f"""
            SELECT d.item_key, d.item_name, d.restaurant_id, d.restaurant_name,
                   d.category_name, d.base_price, d.dietary_tags,
                   r.latitude, r.longitude,
                   1 - (d.embedding <=> %(vector)s::vector) AS score
              FROM menu_item_documents d
              JOIN restaurant_documents r
                ON r.restaurant_id = d.restaurant_id AND r.is_active
             WHERE {item_where} {item_scope}
             ORDER BY d.embedding <=> %(vector)s::vector
             LIMIT %(top_k)s
        """
        match_where, _ = _item_predicates("m", max_price=max_price, tags=tags)
        restaurants_sql = f"""
            SELECT r.restaurant_id, r.name, r.address, r.latitude, r.longitude,
                   1 - (r.embedding <=> %(vector)s::vector) AS score
              FROM restaurant_documents r
             WHERE r.is_active {restaurant_scope}
               AND EXISTS (SELECT 1 FROM menu_item_documents m
                            WHERE m.restaurant_id = r.restaurant_id AND {match_where})
             ORDER BY r.embedding <=> %(vector)s::vector
             LIMIT %(top_k)s
        """
        with self._db.cursor() as cur:
            for statement in _SETTINGS:
                cur.execute(statement)
            cur.execute(items_sql, params)
            items = [dict(row) for row in cur.fetchall()]
            cur.execute(restaurants_sql, params)
            restaurants = [dict(row) for row in cur.fetchall()]
        return items, restaurants

    def restaurant_names(self, restaurant_ids: list[str]) -> dict[str, dict]:
        """`{restaurant_id: {name, is_active}}` for whichever of these are indexed.

        Used to put names on the analytics service's favourite-restaurant ids: the projection
        keeps only ids, and the vector store is where this service already holds names.
        """
        if not restaurant_ids:
            return {}
        rows = self.all(
            "SELECT restaurant_id, name, is_active FROM restaurant_documents"
            " WHERE restaurant_id = ANY(%(ids)s::uuid[])",
            {"ids": restaurant_ids},
        )
        return {str(row["restaurant_id"]): {"name": row["name"], "is_active": row["is_active"]} for row in rows}
