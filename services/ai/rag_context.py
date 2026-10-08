"""The RAG context assembler (Week 4, D62): one call that gathers what a language model needs to
answer a customer's food question, grounded in three decoupled stores.

  vector     the dishes and restaurants whose meaning best matches the prompt, constrained by
             price and tags, from sfo_vector_core
  analytics  what this customer has ordered before, from the Analytics Service (D44)
  courier    how many riders could take an order from each candidate restaurant right now,
             from the Rider Service's Redis geo index (D49)

Nothing here generates text. It returns *facts* — rows the platform recorded — so the Week 5
assistant has something to be constrained to. That is the zero-hallucination contract's
supply side: a dish, price or restaurant the model can cite is one that appears in this response.

**Partial failure degrades; it does not fail.** Each source has its own time budget
(`config.RAG_*_TIMEOUT_SECONDS`). One that errors or runs out of time is named in
`sources_failed` and contributes nothing, and the rest of the response is still returned. A
source silently missing would be worse than an error: the model would read "no riders listed" as
"no riders available" and say so to a customer.

Vector and analytics run concurrently. Courier availability depends on the vector result (it needs
the candidates' coordinates), so it runs after, with one lookup per restaurant in parallel.
"""

import asyncio
import time
from logging import Logger
from typing import Awaitable

from ai import config
from ai.clients.analytics import AnalyticsServiceClient
from ai.clients.rider import RiderServiceClient
from ai.embeddings import Embedder
from ai.geo import haversine_km
from ai.repositories.vectors import VectorRepository
from ai.schemas.rag import (
    CourierAvailability,
    CustomerProfile,
    FavouriteVendor,
    RagContextRequest,
    RagContextResponse,
    RagRestaurant,
)
from ai.schemas.search import ItemMatch, RestaurantMatch


class RagContextAssembler:
    def __init__(
        self,
        vectors: VectorRepository,
        embedder: Embedder,
        analytics: AnalyticsServiceClient,
        riders: RiderServiceClient,
        *,
        logger: Logger,
    ):
        self._vectors = vectors
        self._embedder = embedder
        self._analytics = analytics
        self._riders = riders
        self._logger = logger

    async def assemble(self, request: RagContextRequest) -> RagContextResponse:
        (found, vector_failed), (profile, analytics_failed) = await asyncio.gather(
            self._source("vector", self._search(request), config.RAG_VECTOR_TIMEOUT_SECONDS),
            self._source(
                "analytics", self._profile(str(request.customer_id)), config.RAG_ANALYTICS_TIMEOUT_SECONDS
            ),
        )
        failed: list[str] = []
        if vector_failed:
            failed.append("vector")
        if analytics_failed:
            failed.append("analytics")

        item_rows, restaurant_rows = found if found is not None else ([], [])
        items = [ItemMatch.from_row(row) for row in item_rows]
        restaurants = self._restaurants(restaurant_rows, request)

        courier: list[CourierAvailability] = []
        if vector_failed:
            # Nothing to anchor a courier lookup on. Reported as failed rather than left empty:
            # an empty list would read as "no riders", which is a claim this response cannot make.
            failed.append("courier")
        else:
            courier, courier_failed = await self._courier(item_rows, restaurant_rows)
            if courier_failed:
                failed.append("courier")

        return RagContextResponse(
            query=request.prompt_query,
            items=items,
            restaurants=restaurants,
            customer_profile=profile,
            courier_availability=courier,
            sources_failed=failed,
        )

    async def _source(self, name: str, work: Awaitable, timeout: float):
        """Run one source under its budget. Returns `(value, failed)`; never raises."""
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(work, timeout=timeout), False
        except asyncio.TimeoutError:
            self._logger.warning("RAG source %s timed out after %.1fs", name, timeout)
        except Exception as exc:  # noqa: BLE001 - a source failing must degrade, not fail, the call
            self._logger.warning(
                "RAG source %s failed after %.2fs: %s", name, time.perf_counter() - started, exc
            )
        return None, True

    # --- vector ----------------------------------------------------------------------------------

    async def _search(self, request: RagContextRequest):
        return await asyncio.to_thread(self._search_sync, request)

    def _search_sync(self, request: RagContextRequest):
        """Embed the prompt and run the hybrid search — CPU-bound and blocking, so off the loop."""
        vector = self._embedder.embed([request.prompt_query])[0]
        return self._vectors.search(
            vector,
            top_k=request.top_k,
            max_price=request.max_price,
            tags=request.dietary_filters,
        )

    def _restaurants(self, rows: list[dict], request: RagContextRequest) -> list[RagRestaurant]:
        """Restaurants, nearest first when the caller gave a location.

        Ranked by distance only among the semantic top-K — relevance chose the candidates, and
        proximity orders them. Re-ranking the whole index by distance would put a nearby burger
        joint above a far-away sushi bar for a sushi query.
        """
        located = request.latitude is not None
        built = []
        for row in rows:
            distance = None
            if located and row.get("latitude") is not None:
                distance = round(
                    haversine_km(
                        request.latitude, request.longitude,
                        float(row["latitude"]), float(row["longitude"]),
                    ),
                    2,
                )
            built.append(RagRestaurant(**RestaurantMatch.row_fields(row), distance_km=distance))
        if located:
            built.sort(key=lambda r: (r.distance_km is None, r.distance_km or 0.0))
        return built

    # --- analytics -------------------------------------------------------------------------------

    async def _profile(self, customer_id: str) -> CustomerProfile:
        summary = await asyncio.to_thread(self._analytics.customer_summary, customer_id)
        favourites = summary.get("favourite_restaurants", [])
        names: dict[str, dict] = {}
        if favourites:
            try:
                names = await asyncio.to_thread(
                    self._vectors.restaurant_names, [str(f["restaurant_id"]) for f in favourites]
                )
            except Exception as exc:  # noqa: BLE001 - names are decoration; the profile still stands
                self._logger.warning("Could not resolve favourite restaurant names: %s", exc)
        return CustomerProfile(
            total_orders=summary["total_orders"],
            delivered_orders=summary["delivered_orders"],
            cancelled_orders=summary["cancelled_orders"],
            last_order_at=summary.get("last_order_at"),
            favourite_vendors=[
                FavouriteVendor(
                    restaurant_id=f["restaurant_id"],
                    name=names.get(str(f["restaurant_id"]), {}).get("name"),
                    is_active=names.get(str(f["restaurant_id"]), {}).get("is_active"),
                    delivered_orders=f["delivered_orders"],
                    last_ordered_at=f.get("last_ordered_at"),
                )
                for f in favourites
            ],
        )

    # --- courier ---------------------------------------------------------------------------------

    async def _courier(self, item_rows: list[dict], restaurant_rows: list[dict]):
        """Availability around each candidate restaurant: `(entries, any_lookup_failed)`.

        Anchored on the *restaurant*, not the customer: what decides whether an order can be
        delivered is whether a rider can reach the kitchen. Candidates are the restaurants of the
        matched dishes and the matched restaurants, in rank order, capped at
        `RAG_MAX_COURIER_LOOKUPS`. Entries that succeeded are returned even if another failed.
        """
        candidates: dict[str, tuple[float, float]] = {}
        for row in [*restaurant_rows, *item_rows]:
            rid = str(row["restaurant_id"])
            if rid in candidates or row.get("latitude") is None:
                continue
            candidates[rid] = (float(row["latitude"]), float(row["longitude"]))
            if len(candidates) == config.RAG_MAX_COURIER_LOOKUPS:
                break

        async def lookup(rid: str, lat: float, lon: float):
            result, failed = await self._source(
                "courier",
                asyncio.to_thread(self._riders.nearby, lat, lon),
                config.RAG_RIDER_TIMEOUT_SECONDS,
            )
            return rid, result, failed

        outcomes = await asyncio.gather(*(lookup(rid, *coords) for rid, coords in candidates.items()))
        entries = [
            CourierAvailability(
                restaurant_id=rid,
                available_riders=result["available_riders"],
                nearest_available_km=result.get("nearest_available_km"),
                radius_km=result["radius_km"],
            )
            for rid, result, failed in outcomes
            if not failed
        ]
        return entries, any(failed for _, _, failed in outcomes)
