"""SmartFoodOps Restaurant Service — onboarding and lookups (Port 8002).

Owns the `restaurants` table in its own PostgreSQL database. Owner identity/authorisation is
resolved over HTTP against the User Service, whose database this service cannot reach.

This service briefly owned the kitchen queue as well. D32 moved it: a kitchen's decision is
a fact about an *order*, and every other fact about an order's lifecycle already lived in the
Order Service's database, so `order_tickets` was a cross-database hop earning nothing. What
stays here is the thing that genuinely is restaurant-domain — `capacity`, which the Order
Service reads at checkout and hands to the saga.

Whether a caller owns a restaurant remains this service's fact, and the Order Service
resolves it here over HTTP before letting an admin decide an order (D16).
"""

from uuid import UUID

from fastapi import Depends, FastAPI, status

from clients import UserServiceClient
from common.auth import Principal, current_principal, require_role
from common.config import required
from common.errors import not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import RestaurantRepository
from schemas import RestaurantOnboardRequest, RestaurantResponse

SERVICE_NAME = "restaurant-service"
DATABASE_URL = required("DATABASE_URL")

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(DATABASE_URL, logger=logger)
restaurants = RestaurantRepository(db)
user_service = UserServiceClient(logger)

app = FastAPI(title="SmartFoodOps Restaurant Service", lifespan=db.lifespan)


@app.get("/api/v1/restaurants/health")
def health():
    return {
        "status": "Restaurant Service is operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        "user_service_url": user_service.base_url,
    }


@app.post(
    "/api/v1/restaurants/onboard",
    response_model=RestaurantResponse,
    status_code=status.HTTP_201_CREATED,
)
def onboard_restaurant(
    payload: RestaurantOnboardRequest,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> RestaurantResponse:
    """Onboard a restaurant once its owner is verified through the User Service.

    The owner is the token's subject, so a restaurant can only ever be onboarded under the
    account making the request.
    """
    user_service.verify_owner(principal.user_id, principal.token)
    return RestaurantResponse(**restaurants.onboard(payload, principal.user_id))


@app.get("/api/v1/restaurants/{restaurant_id}", response_model=RestaurantResponse)
def get_restaurant(
    restaurant_id: UUID,
    _: Principal = Depends(current_principal),
) -> RestaurantResponse:
    """Expose restaurant state (including is_active) for other services to verify.

    Any authenticated caller: customers browsing and the Order and Menu Services checking
    a restaurant all read the same non-sensitive record.
    """
    row = restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return RestaurantResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
