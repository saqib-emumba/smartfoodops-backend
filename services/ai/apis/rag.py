"""`POST /api/v1/ai/rag-context` — the multi-source context a language model answers from (D62).

See `ai/rag_context.py` for what is gathered and how a failing source degrades. This module is the
HTTP edge: authorisation, then delegation.
"""

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_permission, require_self_or_admin
from common.responses import Envelope, ok
from ai import deps
from ai.schemas.rag import RagContextRequest, RagContextResponse

router = APIRouter(prefix="/api/v1/ai")


@router.post("/rag-context", response_model=Envelope[RagContextResponse])
async def rag_context(
    payload: RagContextRequest,
    current_user: CurrentUser = Depends(require_permission("ai:rag_context")),
) -> Envelope[RagContextResponse]:
    """Assemble dishes, restaurants, order history and courier availability for one customer.

    `customer_id` must be the caller's own — this response carries that customer's order
    history, so one customer asking for another's is a `403` — unless the caller is an admin.
    The id is in the body rather than inferred from the token because the spec's contract names it
    there, and checking it against the token is what keeps that from being an impersonation hole.

    Always a `200` when authorised: a source that could not be read is named in
    `sources_failed`, not turned into an error (see `ai/rag_context.py`).
    """
    require_self_or_admin(
        current_user,
        payload.customer_id,
        detail="You may only request context for your own account",
    )
    body = await deps.rag.assemble(payload)
    message = "RAG context assembled"
    if body.sources_failed:
        message += f"; unavailable: {', '.join(body.sources_failed)}"
    return ok(body, message=message)
