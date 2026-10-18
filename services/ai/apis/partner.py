"""Authoring tools for restaurant admins (Week 5, D64): dish descriptions, promotions, review summaries.

Gated twice: the gateway demands `ai:partner` (D57), and each handler confirms the caller owns the
restaurant named in the body, asking the Restaurant Service as the caller (D16). A restaurant admin
therefore cannot generate copy, or read review themes, for anyone else's restaurant.
"""

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_permission
from common.responses import Envelope, ok
from ai.assistant import deps as assistant_deps
from ai.metrics import PARTNER_REQUESTS_TOTAL
from ai.schemas.partner import (
    DescribeDishRequest,
    DescribeDishResponse,
    PromoRequest,
    PromoResponse,
    ReviewSummaryRequest,
    ReviewSummaryResponse,
)

router = APIRouter(prefix="/api/v1/ai/partner")


async def _owned_restaurant(restaurant_id, current_user: CurrentUser) -> dict:
    import asyncio

    return await asyncio.to_thread(assistant_deps.restaurants.verify_owner, str(restaurant_id), current_user)


@router.post("/describe-dish", response_model=Envelope[DescribeDishResponse])
async def describe_dish(
    payload: DescribeDishRequest,
    current_user: CurrentUser = Depends(require_permission("ai:partner")),
) -> Envelope[DescribeDishResponse]:
    """Draft a menu description from a dish name and its ingredients — and nothing else."""
    restaurant = await _owned_restaurant(payload.restaurant_id, current_user)
    try:
        result = await assistant_deps.partner_tools().describe_dish(payload, restaurant["name"])
    except Exception:
        PARTNER_REQUESTS_TOTAL.labels(tool="describe_dish", outcome="error").inc()
        raise
    PARTNER_REQUESTS_TOTAL.labels(tool="describe_dish", outcome="ok").inc()
    return ok(result, message="Dish description drafted")


@router.post("/promo", response_model=Envelope[PromoResponse])
async def promo(
    payload: PromoRequest,
    current_user: CurrentUser = Depends(require_permission("ai:partner")),
) -> Envelope[PromoResponse]:
    """Draft promotional copy for an offer the owner states. The tool never adds a discount."""
    restaurant = await _owned_restaurant(payload.restaurant_id, current_user)
    try:
        result = await assistant_deps.partner_tools().promo(payload, restaurant["name"])
    except Exception:
        PARTNER_REQUESTS_TOTAL.labels(tool="promo", outcome="error").inc()
        raise
    PARTNER_REQUESTS_TOTAL.labels(tool="promo", outcome="ok").inc()
    return ok(result, message="Promotion drafted")


@router.post("/review-summary", response_model=Envelope[ReviewSummaryResponse])
async def review_summary(
    payload: ReviewSummaryRequest,
    current_user: CurrentUser = Depends(require_permission("ai:partner")),
) -> Envelope[ReviewSummaryResponse]:
    """Summarise the themes in a restaurant's written reviews.

    The review count and average come from the database; only the prose is the model's. With no
    written reviews there is nothing to summarise, so `summary` is null and the model is not called.
    """
    await _owned_restaurant(payload.restaurant_id, current_user)
    try:
        result = await assistant_deps.partner_tools().review_summary(payload)
    except Exception:
        PARTNER_REQUESTS_TOTAL.labels(tool="review_summary", outcome="error").inc()
        raise
    PARTNER_REQUESTS_TOTAL.labels(tool="review_summary", outcome="ok" if result.summary else "empty").inc()
    return ok(result, message="Review summary generated" if result.summary else "No written reviews to summarise")
