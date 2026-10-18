"""Singletons for the Week 5 surface (D63, D64), kept apart from `deps.py`.

`deps.py` is imported by the ingestion worker too, and the worker has no use for LangGraph, a chat
model or Redis. Building those here, lazily, means the worker's image stays as light as it was and
a missing `ANTHROPIC_API_KEY` stops the assistant endpoints (with a clear 503) without stopping
search, RAG context or ingestion.
"""

from functools import lru_cache

from common.errors import service_unavailable
from ai import config, deps
from ai.assistant.agents.graph import Assistant
from ai.clients.order import OrderServiceClient
from ai.clients.restaurant import RestaurantServiceClient
from ai.clients.rider import RiderServiceClient
from ai.llm.provider import build_chat_model
from ai.assistant.memory import ConversationStore
from ai.assistant.order_explainer import OrderExplainer
from ai.assistant.partner import PartnerTools
from ai.repositories.interactions import InteractionRepository

orders = OrderServiceClient(deps.logger)
restaurants = RestaurantServiceClient(deps.logger)
interactions = InteractionRepository(deps.db, logger=deps.logger)
conversations = ConversationStore(config.AI_REDIS_URL, logger=deps.logger)
explainer = OrderExplainer(orders, restaurants, RiderServiceClient(deps.logger), logger=deps.logger)


@lru_cache(maxsize=1)
def chat_model():
    """The configured chat model, built on first use. A provider that cannot be built is a 503,
    not a 500: the caller did nothing wrong and the operator needs to hear it is a setup problem."""
    try:
        return build_chat_model()
    except RuntimeError as exc:
        deps.logger.error("Chat model unavailable: %s", exc)
        raise service_unavailable("The assistant is not configured on this deployment") from exc


@lru_cache(maxsize=1)
def assistant() -> Assistant:
    return Assistant(chat_model(), deps.rag, explainer, orders, conversations, logger=deps.logger)


@lru_cache(maxsize=1)
def partner_tools() -> PartnerTools:
    return PartnerTools(chat_model(), orders, logger=deps.logger)
