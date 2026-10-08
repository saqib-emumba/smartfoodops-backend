"""`POST /api/v1/ai/search` — hybrid semantic search over dishes and restaurants (Week 4, D59).

Embeds the query with the same model the documents were embedded with (`deps.embedder` — one
instance, so the two can never drift apart), then asks the database for the nearest documents
that also satisfy the structured filters.

A plain `def`, not `async def`: embedding is CPU-bound and the database call blocks, so FastAPI's
threadpool is where both belong — an `async def` doing either would stall the event loop for
every other request.
"""

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_permission
from common.responses import Envelope, ok
from ai import deps
from ai.schemas.search import ItemMatch, RestaurantMatch, SearchRequest, SearchResponse

router = APIRouter(prefix="/api/v1/ai")


@router.post("/search", response_model=Envelope[SearchResponse])
def search(
    payload: SearchRequest,
    _: CurrentUser = Depends(require_permission("ai:search")),
) -> Envelope[SearchResponse]:
    """Find dishes by meaning, constrained by price, tags and availability.

    Results are only ever rows the Menu Service published — available items of active
    restaurants — so nothing here can suggest a dish that does not exist or is sold out. That
    guarantee is what the Week 5 assistant's zero-hallucination rule builds on.

    An empty result is a `200` with empty lists, not a `404`: "nothing matches those filters" is
    an answer the caller can act on (relax the price), not a missing resource.
    """
    vector = deps.embedder.embed([payload.query])[0]
    items, restaurants = deps.vectors.search(
        vector,
        top_k=payload.top_k,
        max_price=payload.max_price,
        tags=payload.dietary_filters,
        restaurant_id=payload.restaurant_id,
    )
    body = SearchResponse(
        matches=[
            ItemMatch(
                item_id=row["item_key"],
                item_name=row["item_name"],
                restaurant_id=row["restaurant_id"],
                restaurant_name=row["restaurant_name"],
                category_name=row["category_name"],
                price=float(row["base_price"]),
                dietary_tags=list(row["dietary_tags"]),
                similarity_score=round(float(row["score"]), 3),
            )
            for row in items
        ],
        restaurants=[
            RestaurantMatch(
                restaurant_id=row["restaurant_id"],
                name=row["name"],
                address=row["address"],
                similarity_score=round(float(row["score"]), 3),
            )
            for row in restaurants
        ],
    )
    count = len(body.matches)
    return ok(body, message=f"Retrieved {count} matching item{'' if count == 1 else 's'}")
