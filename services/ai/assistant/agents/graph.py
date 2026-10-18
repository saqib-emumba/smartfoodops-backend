"""The assistant's LangGraph (Week 5, D63): router -> retrieval -> generation -> guardrail.

    router --+-- out_of_scope ------------------------------> refuse ------------> END
             +-- order_tracking ----> retrieve_order --+
             +-- food / restaurant -> retrieve_food ---+-- nothing found --------> END
                                                       +-- found --> generate --> guardrail --> END

Three of the spec's agents are nodes here, plus the check that makes "zero hallucination" a property
of the code rather than of the prompt:

  router     classifies intent and pulls out the budget, dietary tags and order id
  retrieve   gathers grounded context — the Week 4 RAG assembler for food, the order explainer for
             orders. Retrieval always runs before generation because the edges say so; the model
             cannot decide to skip it, which a tool-calling agent could.
  generate   the model answers from that context only, streaming tokens as it writes
  guardrail  holds the finished answer against the context; an answer naming a dish or price the
             context lacks is replaced by one rendered from the context itself

When nothing clears the similarity floor the model is never called: there is nothing to ground an
answer in, so the reply is a fixed "nothing matches" rather than something the model might embellish.

Runs that end without the model (`refuse`, or retrieval finding nothing) do not stream
tokens; the endpoint streams their fixed text itself. `generate` writes each fragment through
LangGraph's stream writer, which is how the endpoint sees tokens before the guardrail has run.
"""

import asyncio
import json
from logging import Logger
from typing import AsyncIterator, TypedDict
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from common.auth import CurrentUser
from ai import config
from ai.assistant.agents import prompts
from ai.assistant.agents.context import (
    NO_ORDERS,
    NO_RESULTS,
    REFUSAL,
    SEARCH_UNAVAILABLE,
    FoodContext,
    build_food_context,
    render_food_fallback,
)
from ai.assistant.agents.router import parse_router
from ai.clients.order import OrderServiceClient
from ai.llm.guardrails import mentioned, verify_food_answer
from ai.llm.provider import call_model, stream_model
from ai.metrics import GUARDRAIL_VIOLATIONS_TOTAL
from ai.assistant.memory import ConversationStore
from ai.assistant.order_explainer import OrderExplainer, check_explanation
from ai.retrieval.rag_context import RagContextAssembler
from ai.schemas.rag import RagContextRequest

_tracer = trace.get_tracer("ai.assistant")

# How many earlier messages the router and the generator see. Enough for "what about under $10?"
# to make sense, few enough that history never dominates the prompt.
_HISTORY_FOR_ROUTER = 4
_HISTORY_FOR_GENERATION = 6


class AssistantState(TypedDict, total=False):
    # inputs
    question: str
    user: CurrentUser
    latitude: float | None
    longitude: float | None
    history: list[dict]
    # router
    intent: str
    max_price: float | None
    dietary_filters: list[str]
    order_id: str | None
    # retrieval
    food: FoodContext
    facts: dict
    # generation / guardrail
    draft: str
    answer: str
    outcome: str  # answered | fallback | no_results | refused
    replaced: bool
    recommended_item_ids: list[str]
    recommended_restaurant_ids: list[str]


def _traced(name: str, node):
    """Wrap a node in a span, so a trace shows where a slow answer spent its time."""

    async def wrapper(state: AssistantState):
        with _tracer.start_as_current_span(f"ai.assistant.{name}"):
            return await node(state)

    return wrapper


def _history_messages(history: list[dict], limit: int) -> list:
    return [
        HumanMessage(content=m["content"]) if m["role"] == "user" else AIMessage(content=m["content"])
        for m in history[-limit:]
    ]


class Assistant:
    def __init__(
        self,
        model: BaseChatModel,
        rag: RagContextAssembler,
        explainer: OrderExplainer,
        orders: OrderServiceClient,
        store: ConversationStore,
        *,
        logger: Logger,
    ):
        self._model = model
        self._rag = rag
        self._explainer = explainer
        self._orders = orders
        self._store = store
        self._logger = logger
        self._graph = self._build()

    # --- the graph ---------------------------------------------------------------------------

    def _build(self):
        graph = StateGraph(AssistantState)
        for name, node in (
            ("router", self._router),
            ("refuse", self._refuse),
            ("retrieve_food", self._retrieve_food),
            ("retrieve_order", self._retrieve_order),
            ("generate", self._generate),
            ("guardrail", self._guardrail),
        ):
            graph.add_node(name, _traced(name, node))

        graph.add_edge(START, "router")
        graph.add_conditional_edges(
            "router",
            lambda s: {"out_of_scope": "refuse", "order_tracking": "retrieve_order"}.get(
                s["intent"], "retrieve_food"
            ),
            ["refuse", "retrieve_order", "retrieve_food"],
        )
        # Retrieval that found nothing has already set the answer, so it ends the run: the model
        # is not called with nothing to ground it.
        found = lambda s: END if s.get("outcome") == "no_results" else "generate"  # noqa: E731
        graph.add_conditional_edges("retrieve_food", found, [END, "generate"])
        graph.add_conditional_edges("retrieve_order", found, [END, "generate"])
        graph.add_edge("generate", "guardrail")
        for terminal in ("refuse", "guardrail"):
            graph.add_edge(terminal, END)
        return graph.compile()

    # --- nodes -------------------------------------------------------------------------------

    async def _router(self, state: AssistantState):
        recent = "\n".join(f"{m['role']}: {m['content']}" for m in state["history"][-_HISTORY_FOR_ROUTER:])
        human = (f"Recent conversation:\n{recent}\n\n" if recent else "") + f"New message: {state['question']}"
        text = await call_model(
            self._model, [SystemMessage(content=prompts.ROUTER), HumanMessage(content=human)], node="router"
        )
        decision = parse_router(text)
        return {
            "intent": decision.intent,
            "max_price": decision.max_price,
            "dietary_filters": decision.dietary_filters,
            "order_id": decision.order_id,
        }

    async def _refuse(self, state: AssistantState):
        return {"answer": REFUSAL, "outcome": "refused", "recommended_item_ids": [], "recommended_restaurant_ids": []}

    async def _retrieve_food(self, state: AssistantState):
        question = state["question"]
        previous = [m["content"] for m in state["history"] if m["role"] == "user"]
        # A short follow-up ("and under $10?") has no meaning alone; retrieve on it with the
        # question before it.
        query = f"{previous[-1]}. {question}" if previous and len(question.split()) < 6 else question
        request = RagContextRequest(
            customer_id=state["user"].user_id,
            prompt_query=query[: config.ASSISTANT_MAX_QUESTION_CHARS],
            latitude=state.get("latitude"),
            longitude=state.get("longitude"),
            top_k=config.ASSISTANT_CANDIDATES,
            max_price=state.get("max_price"),
            dietary_filters=state.get("dietary_filters") or [],
            min_score=config.ASSISTANT_MIN_SCORE,
        )
        body = await self._rag.assemble(request)

        ratings: dict[str, dict] = {}
        if state["intent"] == "restaurant_support":
            ratings = await self._ratings([str(r.restaurant_id) for r in body.restaurants[:3]])

        food = build_food_context(body, ratings, state.get("max_price"))
        update: dict = {"food": food}
        nothing = not food.prompt["items"] and not (
            state["intent"] == "restaurant_support" and food.prompt["restaurants"]
        )
        if nothing:
            text = SEARCH_UNAVAILABLE if "vector" in body.sources_failed else NO_RESULTS
            update.update(answer=text, outcome="no_results", recommended_item_ids=[], recommended_restaurant_ids=[])
        return update

    async def _ratings(self, restaurant_ids: list[str]) -> dict[str, dict]:
        """Average rating and count per restaurant. Aggregates only — the text of a review is for
        the restaurant's own summary tool, not for a customer-facing answer."""

        async def one(rid: str):
            try:
                result = await asyncio.to_thread(self._orders.restaurant_reviews, rid, 1)
                return rid, {"average_rating": result["average_rating"], "review_count": result["review_count"]}
            except Exception as exc:  # noqa: BLE001 - a rating is decoration
                self._logger.warning("Could not read reviews for %s: %s", rid, exc)
                return rid, None

        return {rid: stats for rid, stats in await asyncio.gather(*(one(r) for r in restaurant_ids)) if stats}

    async def _retrieve_order(self, state: AssistantState):
        user = state["user"]
        order_id = state.get("order_id") or await self._explainer.latest_order_id(user)
        if order_id is None:
            return {"answer": NO_ORDERS, "outcome": "no_results", "recommended_item_ids": [], "recommended_restaurant_ids": []}
        facts = await self._explainer.facts(order_id, user)
        return {"facts": facts}

    async def _generate(self, state: AssistantState):
        writer = get_stream_writer()
        if state["intent"] == "order_tracking":
            system = prompts.EXPLAIN
            block = f"FACTS: {json.dumps(state['facts'], default=str)}"
        else:
            system = prompts.ASSISTANT
            block = f"CONTEXT: {json.dumps(state['food'].prompt, default=str)}"
        messages = [
            SystemMessage(content=system),
            *_history_messages(state["history"], _HISTORY_FOR_GENERATION),
            HumanMessage(content=f"{block}\n\nQUESTION: {state['question']}"),
        ]
        parts: list[str] = []
        async for fragment in stream_model(self._model, messages, node="generate"):
            parts.append(fragment)
            writer({"token": fragment})
        return {"draft": "".join(parts).strip()}

    async def _guardrail(self, state: AssistantState):
        draft = state["draft"]
        if state["intent"] == "order_tracking":
            verdict = check_explanation(draft, state["facts"])
            fallback = state["facts"]["plain_summary"]
            catalogue = None
        else:
            food = state["food"]
            verdict = verify_food_answer(
                draft, names=food.names, prices=food.prices, offers=food.offers, budget=food.budget
            )
            fallback = render_food_fallback(food)
            catalogue = food

        for kind, detail in verdict.violations:
            GUARDRAIL_VIOLATIONS_TOTAL.labels(kind=kind).inc()
            self._logger.warning("Guardrail rejected an answer (%s): %s", kind, detail)

        # An empty draft is not grounded either — a stream that produced nothing is a failure to answer.
        ok = verdict.ok and bool(draft)
        answer = draft if ok else fallback
        update = {"answer": answer, "outcome": "answered" if ok else "fallback", "replaced": not ok}
        update["recommended_item_ids"] = mentioned(answer, catalogue.item_ids) if catalogue else []
        update["recommended_restaurant_ids"] = mentioned(answer, catalogue.restaurant_ids) if catalogue else []
        return update

    # --- running it --------------------------------------------------------------------------

    async def stream(
        self,
        *,
        question: str,
        user: CurrentUser,
        conversation_id: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> AsyncIterator[dict]:
        """Yield `{"token": str}` as the model writes, then one `{"final": AssistantState}`.

        History is loaded before the graph runs and the exchange is saved after it ends, so a
        failed turn leaves the conversation as it was.
        """
        history = await asyncio.to_thread(self._store.load, str(user.user_id), conversation_id)
        initial: AssistantState = {
            "question": question,
            "user": user,
            "latitude": latitude,
            "longitude": longitude,
            "history": history,
        }
        final: AssistantState = {}
        async for mode, payload in self._graph.astream(initial, stream_mode=["custom", "values"]):
            if mode == "custom":
                yield {"token": payload["token"]}
            else:
                final = payload
        await asyncio.to_thread(
            self._store.append, str(user.user_id), conversation_id, question, final.get("answer", "")
        )
        yield {"final": final}


def new_conversation_id() -> str:
    return uuid4().hex
