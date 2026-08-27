"""The one relay request a sibling service sends into the order's workflow."""

from typing import Literal

from pydantic import BaseModel


class WorkflowSignalRequest(BaseModel):
    """One event reported by a sibling service, on its way into the order's workflow.

    `signal` is constrained to the ones the workflow actually handles, so a typo is a 422
    here rather than a signal Temporal accepts and nothing ever reads — an unhandled signal
    name is silently dropped by the SDK, which would be an event that vanishes.

    `restaurant_decision` is deliberately absent. Since D32 a kitchen decision arrives
    through `POST /api/v1/orders/{id}/accept|reject`, which is authenticated as the owning
    admin and writes the decision before signalling. Leaving it reachable here as well would
    be a second, unauthenticated way to do the same thing — exactly the drift D16 warns
    about. This relay now carries rider events only.
    """

    signal: Literal["rider_pickup", "rider_delivery"]
    payload: dict = {}
