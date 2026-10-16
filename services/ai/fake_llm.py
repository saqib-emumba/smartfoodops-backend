"""A deterministic chat model for tests and for running the stack with no API key (D63).

Selected only by an explicit `LLM_PROVIDER=fake`. It is a test double, not a degraded mode: it
does not understand language, it follows the `[ROLE:...]` tag at the top of each system prompt
(see `agents/prompts.py`) and answers from the structured blocks the nodes put in the user
message. That is enough to drive every branch of the graph — routing, retrieval, streaming,
the guardrail's replacement path — repeatably, which a real model cannot be asked to do.

Its one deliberate misbehaviour: a question containing `[force-ungrounded]` makes the assistant
reply with a dish and a price that exist nowhere, so a test can prove the guardrail catches it.
"""

import json
import os
import re
import time
from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from common.dietary import DIETARY_TAGS

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_BUDGET = re.compile(r"(?:under|below|less than|within|upto|up to|max(?:imum)?|budget of)\s*\$?\s*(\d+(?:\.\d+)?)", re.I)
_ORDER_WORDS = ("order", "delivery", "deliver", "refund", "rider", "courier", "arrive", "late", "delay")
_RESTAURANT_WORDS = ("restaurant", "rated", "rating", "reviews", "where is", "address")
_FOOD_WORDS = ("eat", "food", "dish", "hungry", "recommend", "suggest", "lunch", "dinner", "breakfast",
               "snack", "drink", "dessert", "meal", "spicy", "sweet", "cheap", "vegan", "pizza", "burger",
               "sushi", "under", "options", "menu", "popular", "combo")


def _block(text: str, label: str) -> dict:
    """The JSON object that follows `label:` on its own line, or {}."""
    match = re.search(rf"^{label}:\s*(\{{.*\}})\s*$", text, re.M | re.S)
    return json.loads(match.group(1)) if match else {}


class DeterministicChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "smartfoodops-deterministic"

    # --- the one place the "model" decides what to say ---------------------------------------

    def _reply(self, messages: list[BaseMessage]) -> str:
        system = next((m.content for m in messages if m.type == "system"), "")
        human = next((m.content for m in reversed(messages) if m.type == "human"), "")
        system, human = str(system), str(human)
        role = re.match(r"\[ROLE:([a-z-]+)\]", system)
        role = role.group(1) if role else ""

        if role == "router":
            return self._route(human)
        if role == "assistant":
            return self._assist(human)
        if role == "explain":
            return self._explain(human)
        if role.startswith("partner-"):
            return self._partner(role, human)
        return "OK"

    def _route(self, human: str) -> str:
        question = human.split("New message:", 1)[-1].strip()
        lowered = question.lower()
        if _UUID.search(question) or any(w in lowered for w in _ORDER_WORDS):
            intent = "order_tracking"
        elif any(w in lowered for w in _RESTAURANT_WORDS):
            intent = "restaurant_support"
        elif any(w in lowered for w in _FOOD_WORDS):
            intent = "food_discovery"
        else:
            intent = "out_of_scope"
        budget = _BUDGET.search(question)
        tags = [t for t in DIETARY_TAGS if re.search(rf"\b{re.escape(t)}\b", lowered)]
        order = _UUID.search(question)
        return json.dumps(
            {
                "intent": intent,
                "max_price": float(budget.group(1)) if budget else None,
                "dietary_filters": tags,
                "order_id": order.group(0) if order else None,
            }
        )

    def _assist(self, human: str) -> str:
        if "[force-ungrounded]" in human:
            return "I recommend the **Imaginary Dragon Roll** for $1.00."
        context = _block(human, "CONTEXT")
        items = context.get("items", [])
        if items:
            top = items[0]
            return (
                f"Based on what is on the menu, I recommend **{top['name']}** at "
                f"**{top['restaurant']}** (${top['price']:.2f})."
            )
        restaurants = context.get("restaurants", [])
        if restaurants:
            return f"You could try **{restaurants[0]['name']}**."
        return "I could not find anything matching that on the current menus."

    def _explain(self, human: str) -> str:
        facts = _block(human, "FACTS")
        return facts.get("plain_summary", "Your order is being handled.")

    def _partner(self, role: str, human: str) -> str:
        details = _block(human, "DETAILS") or _block(human, "REVIEWS")
        if role == "partner-describe-dish":
            return f"{details.get('dish_name', 'This dish')}, made with {', '.join(details.get('ingredients', []))}."
        if role == "partner-promo":
            return f"{details.get('restaurant_name', 'We')}: {details.get('offer', '')}"
        return "Customers mostly praise the food; a few mention waiting."

    # --- BaseChatModel plumbing ---------------------------------------------------------------

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._reply(messages)))])

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        # Word by word, so the SSE path sees many chunks rather than one.
        words = self._reply(messages).split(" ")
        delay = float(os.getenv("FAKE_LLM_TOKEN_DELAY_SECONDS", "0"))
        for index, word in enumerate(words):
            if delay:
                time.sleep(delay)  # lets a test see that tokens arrive while the model is still writing
            token = word if index == 0 else f" {word}"
            yield ChatGenerationChunk(message=AIMessageChunk(content=token))
