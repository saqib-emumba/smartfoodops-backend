"""`GET /api/v1/ai/assistant/stream` — the conversational assistant over Server-Sent Events (D63).

One question in, tokens out as the model writes them. The stream is not wrapped in the D35
envelope — an event stream has no single body to envelop — so its shape is the spec's: `data:`
lines of `{"token": str, "done": bool}`, with a final chunk (`done: true`, empty token) that also
carries `conversation_id`, `intent` and `grounded`.

If the grounding check rejects the finished answer (see `ai/guardrails.py`), the tokens already
sent cannot be recalled, so the final chunk carries `replacement` — the plain, rule-written answer
— and the client is expected to show it in place of what streamed.

Failures before the first event (a bad question, someone else's order, the model unavailable) are
ordinary enveloped HTTP errors. A failure after streaming has begun can no longer change the status
line, so it arrives as a final `{"done": true, "error": ...}` chunk.
"""

import asyncio
import json
import re
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from opentelemetry import trace
from sse_starlette.sse import EventSourceResponse

from common.auth import CurrentUser, require_permission
from common.errors import unprocessable
from ai import assistant_deps, config
from ai.memory import CONVERSATION_ID_PATTERN
from ai.metrics import (
    RECOMMENDING_INTERACTIONS_TOTAL,
    ASSISTANT_FIRST_TOKEN_SECONDS,
    ASSISTANT_LATENCY_SECONDS,
    ASSISTANT_REQUESTS_TOTAL,
    RECOMMENDED_ITEMS_TOTAL,
)

router = APIRouter(prefix="/api/v1/ai")
_tracer = trace.get_tracer("ai.assistant")

_WORDS = re.compile(r"\S+\s*")


def _event(payload: dict) -> dict:
    return {"data": json.dumps(payload)}


async def _log_interaction(user: CurrentUser, conversation_id: str, intent: str, outcome: str, final: dict) -> None:
    """Record the answer for usage and conversion reporting. Best-effort: never costs the customer
    their answer. Identifiers and outcomes only — no question and no answer text."""
    restaurants = final.get("recommended_restaurant_ids", [])
    if restaurants:
        RECOMMENDING_INTERACTIONS_TOTAL.inc()
    try:
        await asyncio.to_thread(
            assistant_deps.interactions.record,
            customer_id=user.user_id,
            conversation_id=conversation_id,
            intent=intent,
            outcome=outcome,
            restaurant_ids=restaurants,
            item_ids=final.get("recommended_item_ids", []),
        )
    except Exception as exc:  # noqa: BLE001
        assistant_deps.deps.logger.warning("Could not log the assistant interaction: %s", exc)


@router.get("/assistant/stream")
async def assistant_stream(
    q: str = Query(..., description="The question, e.g. 'Recommend spicy sushi under $15'"),
    conversation_id: str | None = Query(None, description="Send back the id from a previous reply to continue it"),
    latitude: float | None = Query(None, ge=-90, le=90),
    longitude: float | None = Query(None, ge=-180, le=180),
    current_user: CurrentUser = Depends(require_permission("ai:assistant")),
) -> EventSourceResponse:
    question = q.strip()
    if not 2 <= len(question) <= config.ASSISTANT_MAX_QUESTION_CHARS:
        raise unprocessable(f"q must be between 2 and {config.ASSISTANT_MAX_QUESTION_CHARS} characters")
    if (latitude is None) != (longitude is None):
        raise unprocessable("latitude and longitude must be given together")
    if conversation_id is None:
        from ai.agents.graph import new_conversation_id

        conversation_id = new_conversation_id()
    elif not CONVERSATION_ID_PATTERN.match(conversation_id):
        raise unprocessable("conversation_id may contain only letters, digits, '-' and '_' (max 64)")

    started = time.perf_counter()
    events = assistant_deps.assistant().stream(
        question=question,
        user=current_user,
        conversation_id=conversation_id,
        latitude=latitude,
        longitude=longitude,
    )

    # Pull the first event before answering, so a failure that precedes any output is a real HTTP
    # status. The wait is bounded by routing plus retrieval, which is also how long the client
    # would wait for a first token anyway.
    try:
        first = await anext(events)
    except HTTPException:
        ASSISTANT_REQUESTS_TOTAL.labels(intent="unknown", outcome="error").inc()
        ASSISTANT_LATENCY_SECONDS.observe(time.perf_counter() - started)
        raise

    async def body() -> AsyncIterator[dict]:
        intent, outcome, first_token_seen = "unknown", "error", False
        streamed = False
        try:
            pending = [first]
            while True:
                event = pending.pop() if pending else await anext(events, None)
                if event is None:
                    break
                if "token" in event:
                    if not first_token_seen:
                        first_token_seen = True
                        ASSISTANT_FIRST_TOKEN_SECONDS.observe(time.perf_counter() - started)
                    streamed = True
                    yield _event({"token": event["token"], "done": False})
                    continue

                final = event["final"]
                intent, outcome = final.get("intent", "unknown"), final.get("outcome", "error")
                answer = final.get("answer", "")
                if not streamed:
                    # Fixed answers (out of scope, nothing found) never touch the model; stream them
                    # the same way so a client has one code path.
                    for piece in _WORDS.findall(answer):
                        if not first_token_seen:
                            first_token_seen = True
                            ASSISTANT_FIRST_TOKEN_SECONDS.observe(time.perf_counter() - started)
                        yield _event({"token": piece, "done": False})
                closing = {
                    "token": "",
                    "done": True,
                    "conversation_id": conversation_id,
                    "intent": intent,
                    "grounded": outcome != "fallback",
                }
                if final.get("replaced"):
                    closing["replacement"] = answer
                RECOMMENDED_ITEMS_TOTAL.inc(len(final.get("recommended_item_ids", [])))
                await _log_interaction(current_user, conversation_id, intent, outcome, final)
                yield _event(closing)
        except asyncio.CancelledError:
            outcome = "cancelled"  # the client went away mid-stream; not a fault of ours
            raise
        except HTTPException as exc:
            outcome = "error"
            yield _event({"token": "", "done": True, "conversation_id": conversation_id, "error": exc.detail})
        except Exception:  # noqa: BLE001 - the status line is gone; say so in-band and log it
            outcome = "error"
            assistant_deps.deps.logger.exception("Assistant stream failed")
            yield _event({"token": "", "done": True, "conversation_id": conversation_id, "error": "The assistant hit a problem; please try again"})
        finally:
            ASSISTANT_REQUESTS_TOTAL.labels(intent=intent, outcome=outcome).inc()
            ASSISTANT_LATENCY_SECONDS.observe(time.perf_counter() - started)

    return EventSourceResponse(
        body(),
        headers={"X-Conversation-Id": conversation_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
