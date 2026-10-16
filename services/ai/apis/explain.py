"""`POST /api/v1/ai/explain-order` — a plain-language account of where an order stands (D64).

The facts come from the Order Service (the order, its transition trail, the running saga's own
stage), the Restaurant Service (a name and coordinates) and the Rider Service (how far the assigned
courier is) — see `ai/order_explainer.py`. The model only phrases them; the figures it may quote are
checked against them afterwards.
"""

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_permission
from common.responses import Envelope, ok
from ai import assistant_deps
from ai.metrics import EXPLAIN_REQUESTS_TOTAL
from ai.order_explainer import explain
from ai.schemas.explain import ExplainOrderRequest, ExplainOrderResponse

router = APIRouter(prefix="/api/v1/ai")


@router.post("/explain-order", response_model=Envelope[ExplainOrderResponse])
async def explain_order(
    payload: ExplainOrderRequest,
    current_user: CurrentUser = Depends(require_permission("ai:explain_order")),
) -> Envelope[ExplainOrderResponse]:
    """Explain an order's status, delay or cancellation.

    Only the customer who placed the order (or an admin) may ask: the facts include what happened
    to their money. An unknown order is a `404`. The explanation never carries a cause the saga did
    not record, an arrival time, or any error text — a missing cause reads as "no recorded reason".
    """
    try:
        facts = await assistant_deps.explainer.facts(str(payload.order_id), current_user)
        explanation, grounded = await explain(assistant_deps.chat_model(), facts)
    except Exception:
        EXPLAIN_REQUESTS_TOTAL.labels(outcome="error").inc()
        raise
    EXPLAIN_REQUESTS_TOTAL.labels(outcome="answered" if grounded else "fallback").inc()
    return ok(
        ExplainOrderResponse(order_id=payload.order_id, explanation=explanation, grounded=grounded),
        message="Order explanation generated successfully",
    )
