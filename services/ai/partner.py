"""The restaurant-admin authoring tools (Week 5, D64): dish copy, promotions, review summaries.

Plain chains rather than graph nodes — one prompt, one model call, one check — because there is
no retrieval to force and no branching to route. What they share with the assistant is the
discipline: the model is given only what the owner supplied (or what the platform recorded), and
its output is checked against that before it is returned.

Checks here are narrower than the assistant's, matched to what each tool could get wrong. Copy
that states a price, a discount or a quantity the owner never gave is the realistic harm, so any
number in the output must appear in the input; otherwise the tool returns a plain rendering of the
input instead. Review counts and averages never pass through the model at all.
"""

import asyncio
import json
import re
from logging import Logger

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ai import config
from ai.agents import prompts
from ai.clients.order import OrderServiceClient
from ai.llm import call_model
from ai.metrics import GUARDRAIL_VIOLATIONS_TOTAL
from ai.schemas.partner import (
    DescribeDishRequest,
    DescribeDishResponse,
    PromoRequest,
    PromoResponse,
    ReviewSummaryRequest,
    ReviewSummaryResponse,
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _invents_numbers(output: str, source: str) -> bool:
    """True if the output contains a number the source does not."""
    allowed = set(_NUMBER.findall(source))
    return any(n not in allowed for n in _NUMBER.findall(output))


class PartnerTools:
    def __init__(self, model: BaseChatModel, orders: OrderServiceClient, *, logger: Logger):
        self._model = model
        self._orders = orders
        self._logger = logger

    async def describe_dish(self, request: DescribeDishRequest, restaurant_name: str) -> DescribeDishResponse:
        details = {
            "dish_name": request.dish_name,
            "ingredients": request.ingredients,
            "tone": request.tone,
            "restaurant_name": restaurant_name,
        }
        text = await call_model(
            self._model,
            [SystemMessage(content=prompts.DESCRIBE_DISH), HumanMessage(content=f"DETAILS: {json.dumps(details)}")],
            node="partner_describe_dish",
        )
        text = text.strip()
        source = f"{request.dish_name} {' '.join(request.ingredients)}"
        if not text or "$" in text or _invents_numbers(text, source):
            GUARDRAIL_VIOLATIONS_TOTAL.labels(kind="unknown_quantity").inc()
            fallback = f"{request.dish_name}, made with {', '.join(request.ingredients)}."
            return DescribeDishResponse(dish_name=request.dish_name, description=fallback, grounded=False)
        return DescribeDishResponse(dish_name=request.dish_name, description=text, grounded=True)

    async def promo(self, request: PromoRequest, restaurant_name: str) -> PromoResponse:
        details = {"restaurant_name": restaurant_name, "offer": request.offer, "channel": request.channel}
        text = await call_model(
            self._model,
            [SystemMessage(content=prompts.PROMO), HumanMessage(content=f"DETAILS: {json.dumps(details)}")],
            node="partner_promo",
        )
        text = text.strip()
        if not text or _invents_numbers(text, request.offer) or ("$" in text and "$" not in request.offer):
            GUARDRAIL_VIOLATIONS_TOTAL.labels(kind="unknown_quantity").inc()
            return PromoResponse(promo_text=f"{restaurant_name}: {request.offer}", grounded=False)
        return PromoResponse(promo_text=text, grounded=True)

    async def review_summary(self, request: ReviewSummaryRequest) -> ReviewSummaryResponse:
        data = await asyncio.to_thread(
            self._orders.restaurant_reviews, str(request.restaurant_id), config.REVIEW_SUMMARY_MAX_REVIEWS
        )
        reviews = data["reviews"]
        response = ReviewSummaryResponse(
            restaurant_id=request.restaurant_id,
            review_count=data["review_count"],
            average_rating=data["average_rating"],
            reviews_analysed=len(reviews),
        )
        if not reviews:
            return response  # nothing written to summarise; counts still stand
        payload = {
            "review_count": data["review_count"],
            "average_rating": data["average_rating"],
            "reviews": [{"rating": r["rating"], "comment": r["comment"]} for r in reviews],
        }
        text = await call_model(
            self._model,
            [SystemMessage(content=prompts.REVIEW_SUMMARY), HumanMessage(content=f"REVIEWS: {json.dumps(payload)}")],
            node="partner_review_summary",
        )
        response.summary = text.strip() or None
        return response
