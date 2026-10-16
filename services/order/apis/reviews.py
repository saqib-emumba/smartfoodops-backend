"""Reviews: a customer writes one, the AI Service reads a restaurant's (Week 5, D64).

Two credentials on one router, so the guard is per route (the same shape as apis/tracking.py):
the write is a bearer token checked against the order's owner, the read is internal-key only.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from common.auth import CurrentUser, require_internal, require_permission
from common.errors import conflict, forbidden, not_found
from common.responses import Envelope, ok
from order import deps
from order.schemas.reviews import OrderReviewRequest, OrderReviewResponse, RestaurantReviews

router = APIRouter(prefix="/api/v1/orders")


@router.post(
    "/{order_id}/review",
    response_model=Envelope[OrderReviewResponse],
    status_code=status.HTTP_201_CREATED,
)
def review_order(
    order_id: UUID,
    payload: OrderReviewRequest,
    current_user: CurrentUser = Depends(require_permission("order:review")),
) -> Envelope[OrderReviewResponse]:
    """Rate a delivered order, once.

    Only the customer who placed the order may review it — not an admin on their behalf, because a
    review is the customer's statement and an operator writing it would make the summaries the AI
    Service produces from it untrue. Only a `delivered` order can be reviewed: a cancelled or
    still-moving one has nothing to rate yet.
    """
    order = deps.orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")
    if str(order["customer_id"]) != str(current_user.user_id):
        raise forbidden("You may only review your own orders")
    if order["status"] != "delivered":
        raise conflict(f"Order {order_id} is '{order['status']}'; only a delivered order can be reviewed")

    row = deps.reviews.create(order=order, rating=payload.rating, comment=payload.comment)
    return ok(OrderReviewResponse(**row), message="Review recorded", status=201)


@router.get(
    "/internal/restaurants/{restaurant_id}/reviews",
    response_model=Envelope[RestaurantReviews],
    dependencies=[Depends(require_internal)],
)
def restaurant_reviews(
    restaurant_id: UUID, limit: int = Query(30, ge=1, le=100)
) -> Envelope[RestaurantReviews]:
    """A restaurant's review count, average rating and most recent comments, for the AI Service.

    Internal-key only, and unmapped in `route_permissions`, so the gateway refuses it even to an
    admin's bearer (D57). Carries no customer id: the consumer is an LLM prompt.
    """
    return ok(RestaurantReviews(**deps.reviews.for_restaurant(restaurant_id, limit)), message="Reviews")
