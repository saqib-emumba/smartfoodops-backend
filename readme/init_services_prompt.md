## 🤖 Claude Code Prompt

Once you have set up your local directories and created these blueprint files, copy and paste this command into your Claude Code terminal interface to execute the full code write-up automatically:

```text
Initialize all Week 1 service logic based on the existing configuration files on disk, 'readme/week1-local-setup-blueprint-v4.md', and the updated 'readme/week1-api-and-db-contracts-v6.md'.

Note: A bootstrap script has already generated 'docker-compose.yml', 'init.sql', '.env', and 'api-gateway/nginx.conf'. DO NOT overwrite or recreate these files. Read them to understand the database connections and ports, then execute the following tasks:

1. Create a Dockerfile and schemas.py inside services/user/, services/restaurant/, services/menu/, and services/order/ using the specifications in your blueprints.

2. In services/user/main.py:
   - Handle registration payloads using Pydantic. Map incoming string roles (e.g. "customer") to corresponding primary key "role_id" from the PostgreSQL "roles" table using a database lookup.
   - Use bcrypt to securely hash passwords before storing.
   - EXPLICITLY expose a "GET /api/v1/users/{user_id}" endpoint that queries the database (joining the roles lookup table) and returns a UserResponse schema.
   - EXPLICITLY handle edge cases: Throw a 400 Bad Request if an invalid role name is requested during registration. Throw a 409 Conflict if registration fails due to duplicate email or phone number database constraints. Return a 404 Not Found if a requested user_id does not exist.

3. In services/restaurant/main.py:
   - Validate latitude/longitude coordinates (latitude between -90 and 90, longitude between -180 and 180) and capacity > 0 via Pydantic.
   - EXPLICITLY handle edge cases: To respect microservice boundaries, DO NOT query the PostgreSQL "users" table directly. Instead, make an inter-service HTTP GET call to the User Service (using the USER_SERVICE_URL environment variable: http://user-service:8001/api/v1/users/{owner_id}) to verify the owner exists and has the "restaurant_admin" role. Throw a 404 Not Found if the owner doesn't exist, and a 403 Forbidden if the owner is not authorized to onboard restaurants.
   - EXPLICITLY expose a "GET /api/v1/restaurants/{restaurant_id}" endpoint returning RestaurantResponse to let other services verify restaurant active status.

4. In services/menu/main.py:
   - Implement dynamic, hierarchical menu upserts into MongoDB.
   - Validate custom menu selections (e.g. throw a 422 Unprocessable Entity if "min_selection" is greater than "max_selection" in any customization group).
   - EXPLICITLY handle edge cases: DO NOT query the PostgreSQL database directly. Instead, make an inter-service HTTP GET call to the Restaurant Service (using RESTAURANT_SERVICE_URL: http://restaurant-service:8002/api/v1/restaurants/{restaurant_id}) to verify that the restaurant exists and is active. Throw a 404 Not Found if it is missing or inactive. Gracefully catch MongoDB socket timeouts/failures and return a 503 Service Unavailable.
   - EXPLICITLY expose a "POST /api/v1/menus/logs" endpoint mapping to OrderTrackingLogCreateRequest that inserts or appends an order audit log to the MongoDB "order_tracking_logs" collection.

5. In services/order/main.py:
   - Secure order creation by verifying the client's checkout request against active menus in the Menu Service (via MENU_SERVICE_URL: http://menu-service:8003/api/v1/menus/{restaurant_id}). Recalculate total pricing and verify item availability.
   - EXPLICITLY handle edge cases:
     a. If "X-Idempotency-Key" header is missing, return 400 Bad Request.
     b. If the idempotency key already exists in the PostgreSQL orders table, bypass processing and safely return the matching existing order payload as a 200 OK.
     c. If any ordered item is marked as unavailable ("is_available" = false) or if the calculated total amount does not match the client's payload, abort the transaction and return 422 Unprocessable Entity.
     d. DO NOT write to MongoDB directly. Instead, make an inter-service HTTP POST call to the Menu Service's logging endpoint (POST http://menu-service:8003/api/v1/menus/logs) logging the "created" state. Map the active "service" name ("order-service") and serialize the payload as a "raw_log" string. If PostgreSQL connection pools are starved, return a 500 Internal Server Error.

6. Build and spin up the environment using 'docker-compose up --build -d'.

7. Run healthchecks on localhost:80/health, localhost/api/v1/users/health, localhost/api/v1/restaurants/health, localhost/api/v1/menus/health, and localhost/api/v1/orders/health to verify that the Nginx proxy and services route perfectly.
```
