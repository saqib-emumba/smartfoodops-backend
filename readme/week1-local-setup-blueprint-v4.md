# SmartFoodOps — Week 1 Local Environment Setup Blueprint (v4 - Fully Decoupled Layout)

This blueprint provides the complete, copy-pasteable configuration files, service directory layouts, and step-by-step terminal commands to establish your local containerized multi-service development environment for **Week 1**.

---

## 📂 1. Directory Structure

Before spinning up any services, establish a clean, separated service structure. Run the following command in your terminal to generate this structure:

```bash
mkdir -p smartfoodops-backend/{api-gateway,services/{user,restaurant,menu,order}}
```

Your project directory should look exactly like this:

```text
smartfoodops-backend/
├── api-gateway/
│   └── nginx.conf            # Gateway routing config
├── services/
│   ├── user/
│   │   ├── main.py           # User FastAPI app
│   │   └── Dockerfile
│   ├── restaurant/
│   │   ├── main.py           # Restaurant FastAPI app
│   │   └── Dockerfile
│   ├── menu/
│   │   ├── main.py           # Menu FastAPI app
│   │   └── Dockerfile
│   └── order/
│       ├── main.py           # Orders FastAPI app
│       └── Dockerfile
├── .env                      # Local environment variables
└── docker-compose.yml        # Docker orchestration file
```

---

## 🐳 2. Docker Compose Configuration (`docker-compose.yml`)

Create a `docker-compose.yml` file in your root `smartfoodops-backend/` directory. This coordinates your local databases, Redis cache, the API gateway, and placeholders for your four core services.

```yaml
version: '3.8'

networks:
  smartfoodops-network:
    driver: bridge

volumes:
  postgres_data:
  mongodb_data:
  redis_data:

services:
  # --- 1. DATABASES & CACHING ---
  db-postgres:
    image: postgres:15-alpine
    container_name: sfo-postgres
    restart: always
    environment:
      POSTGRES_DB: smartfoodops_core
      POSTGRES_USER: sfo_admin
      POSTGRES_PASSWORD: sfo_password_123
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_admin -d smartfoodops_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  db-nosql:
    image: mongo:6.0
    container_name: sfo-mongodb
    restart: always
    ports:
      - "27017:27017"
    volumes:
      - mongodb_data:/data/db
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 5s
      timeout: 5s
      retries: 5

  cache-redis:
    image: redis:7.0-alpine
    container_name: sfo-redis
    restart: always
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  # --- 2. API GATEWAY (NGINX REVERSE PROXY) ---
  api-gateway:
    image: nginx:alpine
    container_name: sfo-api-gateway
    restart: always
    ports:
      - "80:80"
    volumes:
      - ./api-gateway/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - user-service
      - restaurant-service
      - menu-service
      - order-service
    networks:
      - smartfoodops-network

  # --- 3. CORE SERVICE CONTAINER SERVICES ---
  user-service:
    build:
      context: ./services/user
      dockerfile: Dockerfile
    container_name: sfo-user-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core
    depends_on:
      db-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  restaurant-service:
    build:
      context: ./services/restaurant
      dockerfile: Dockerfile
    container_name: sfo-restaurant-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core
    depends_on:
      db-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network

  menu-service:
    build:
      context: ./services/menu
      dockerfile: Dockerfile
    container_name: sfo-menu-service
    restart: always
    environment:
      - MONGO_URI=mongodb://db-nosql:27017/smartfoodops_menus
      - REDIS_URL=redis://cache-redis:6379/0
    depends_on:
      db-nosql:
        condition: service_healthy
      cache-redis:
        condition: service_healthy
    networks:
      - smartfoodops-network

  order-service:
    build:
      context: ./services/order
      dockerfile: Dockerfile
    container_name: sfo-order-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core
      - USER_SERVICE_URL=http://user-service:8001
      - RESTAURANT_SERVICE_URL=http://restaurant-service:8002
      - MENU_SERVICE_URL=http://menu-service:8003
    depends_on:
      db-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network
```

---

## 🔒 3. API Gateway Router Configuration (`api-gateway/nginx.conf`)

Nginx will intercept all requests arriving at `http://localhost/` and route them seamlessly based on URL path rules, satisfying Week 1 modular routing requirements.

Create `api-gateway/nginx.conf` and paste the following:

```nginx
events {
    worker_connections 1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;
    sendfile        on;
    keepalive_timeout  65;

    # Standard Main Access log format (correlation headers deferred to Week 3)
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$upstream_addr"';
    access_log /var/log/nginx/access.log main;

    server {
        listen 80;
        server_name localhost;

        # 👤 Route User service requests
        location /api/v1/users {
            proxy_pass http://user-service:8001;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 🏪 Route Restaurant service requests
        location /api/v1/restaurants {
            proxy_pass http://restaurant-service:8002;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 📜 Route Menu service requests
        location /api/v1/menus {
            proxy_pass http://menu-service:8003;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # 🛍️ Route Order service requests
        location /api/v1/orders {
            proxy_pass http://order-service:8004;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # Global health check endpoint
        location /health {
            return 200 '{"status": "gateway_is_healthy"}';
            add_header Content-Type application/json;
        }
    }
}
```

---

## 🐍 4. Standard Python Service Blueprint (`Dockerfile` & Boilerplate)

Each microservice folder under `services/` must contain its own custom `Dockerfile` and initial startup file `main.py` so that Docker Compose can build them correctly.

### **Dockerfile (Same for all services)**
Place this exact file inside **`services/user/Dockerfile`**, **`services/restaurant/Dockerfile`**, **`services/menu/Dockerfile`**, and **`services/order/Dockerfile`**:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install standard dependencies
RUN pip install --no-cache-dir fastapi uvicorn pydantic psycopg2-binary motor redis

# Copy application source code
COPY . /app

EXPOSE 8000

# Start command overrides port via environment configuration
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### **Microservice App Templates (`main.py`)**

Below are the simplified base implementations for each microservice. They initialize database hook connections and expose ports corresponding to the Nginx mapping configuration.

#### **A. User Service (`services/user/main.py`) — Port 8001**
```python
from fastapi import FastAPI
import os

app = FastAPI(title="SmartFoodOps User Service")

@app.get("/api/v1/users/health")
def health():
    return {"status": "User Service is up and connected", "db_url": os.getenv("DATABASE_URL") is not None}

@app.post("/api/v1/users/register")
def register_user(payload: dict):
    # TODO: Week 1 user validation and insert logic
    return {"message": "User registered successfully", "user": payload}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
```

#### **B. Restaurant Service (`services/restaurant/main.py`) — Port 8002**
```python
from fastapi import FastAPI
import os

app = FastAPI(title="SmartFoodOps Restaurant Service")

@app.get("/api/v1/restaurants/health")
def health():
    return {"status": "Restaurant Service is operational"}

@app.post("/api/v1/restaurants/onboard")
def onboard_restaurant(payload: dict):
    # TODO: Insert restaurant details & capacities in Postgres
    return {"message": "Restaurant onboarded", "restaurant": payload}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
```

#### **C. Menu Service (`services/menu/main.py`) — Port 8003**
```python
from fastapi import FastAPI, HTTPException
import os

app = FastAPI(title="SmartFoodOps Menu Service")

@app.get("/api/v1/menus/health")
def health():
    return {"status": "Menu Service running with NoSQL + Redis connection"}

@app.post("/api/v1/menus")
def create_menu(payload: dict):
    # TODO: Upsert hierarchical menu to MongoDB collection
    return {"message": "Menu upserted to Document DB", "menu": payload}

@app.post("/api/v1/menus/logs")
def log_order_status(payload: dict):
    # TODO: Append log to MongoDB order_tracking_logs collection
    return {"message": "Audit log captured", "log": payload}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
```

#### **D. Orders Service (`services/order/main.py`) — Port 8004**
```python
from fastapi import FastAPI, Header, HTTPException
import os

app = FastAPI(title="SmartFoodOps Order Service")

@app.get("/api/v1/orders/health")
def health():
    return {"status": "Orders Service operational"}

@app.post("/api/v1/orders")
def create_order(payload: dict, x_idempotency_key: str = Header(None)):
    if not x_idempotency_key:
         raise HTTPException(status_code=400, detail="X-Idempotency-Key header is required")
    
    # TODO: Verify header in PG database to prevent duplicate orders
    # TODO: Call Menu & Restaurant services for validations
    return {
        "message": "Order created successfully",
        "idempotency_key_received": x_idempotency_key,
        "order": payload
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004)
```

---

## 🚀 5. Spinning Up and Verifying Your Environment

Once all directories and files are written:

1.  **Start your infrastructure and services:**
    Open your terminal in the root `smartfoodops-backend/` directory and run:
    ```bash
    docker-compose up --build -d
    ```

2.  **Verify the Containers are Running:**
    Check the running state using:
    ```bash
    docker compose ps
    ```
    All containers (`sfo-postgres`, `sfo-mongodb`, `sfo-redis`, `sfo-api-gateway`, and the service containers) should show as `running`.

3.  **Test Routing via the Gateway:**
    Use `curl` or an API client (like Postman) to send requests directly to localhost (port 80). Nginx will automatically route them to the correct services!
    
    *   **Test Nginx Gateway Direct health check:**
        ```bash
        curl http://localhost/health
        ```
    *   **Test User Service through the gateway:**
        ```bash
        curl http://localhost/api/v1/users/health
        ```
    *   **Test Menu Service through the gateway:**
        ```bash
        curl http://localhost/api/v1/menus/health
        ```
    *   **Test Order creation with the mandatory Idempotency Header:**
        ```bash
        curl -X POST http://localhost/api/v1/orders \
          -H "Content-Type: application/json" \
          -H "X-Idempotency-Key: sample-uuid-1111" \
          -d '{"restaurant_id": "1", "items": [{"id": "item1", "qty": 2}]}'
        ```

Congratulations! You now have a complete, fully operational local development environment structured perfectly for your Week 1 deliverables.
