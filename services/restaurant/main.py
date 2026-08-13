"""SmartFoodOps Restaurant Service — onboarding and lookups (Port 8002).

Owns the PostgreSQL `restaurants` table. Owner identity/authorisation is resolved over HTTP
against the User Service so that this service never touches the `users` table directly.
"""

from uuid import UUID

from fastapi import FastAPI, status

from clients import UserServiceClient
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
def onboard_restaurant(payload: RestaurantOnboardRequest) -> RestaurantResponse:
    """Onboard a restaurant once its owner is verified through the User Service."""
    user_service.verify_owner(payload.owner_id)
    return RestaurantResponse(**restaurants.onboard(payload))


@app.get("/api/v1/restaurants/{restaurant_id}", response_model=RestaurantResponse)
def get_restaurant(restaurant_id: UUID) -> RestaurantResponse:
    """Expose restaurant state (including is_active) for other services to verify."""
    row = restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return RestaurantResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
