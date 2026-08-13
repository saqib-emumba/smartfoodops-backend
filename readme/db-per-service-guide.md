# SmartFoodOps — Database-per-Service (Option B) Integration Guide

This guide details the physical separation of databases for our core services to achieve production-grade scalability, performance isolation, and a clean zero-shared-state architecture.

---

## 🗺️ 1. Service to Database Mapping

In a production-grade rollout, each service owns its storage engine. Under Option B, we spin up independent physical database containers with separate network ports and data volumes:

| Microservice | Database Type | Container Name | Internal Port | External Port | Default Database Name |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **User Service** | PostgreSQL | `sfo-user-db` | `5432` | `5432` | `sfo_user_core` |
| **Restaurant Service** | PostgreSQL | `sfo-restaurant-db` | `5432` | `5433` | `sfo_restaurant_core` |
| **Order Service** | PostgreSQL | `sfo-order-db` | `5432` | `5434` | `sfo_order_core` |
| **Menu Service** | MongoDB + Redis | `sfo-mongodb` + `sfo-redis` | `27017` + `6379` | `27017` + `6379` | `smartfoodops_menus` |

---

## 🐳 2. Updated Docker Compose Configuration

Replace the database and service block in your main `docker-compose.yml` with the following configuration:

```yaml
version: '3.8'

networks:
  smartfoodops-network:
    driver: bridge

volumes:
  user_postgres_data:
  restaurant_postgres_data:
  order_postgres_data:
  mongodb_data:
  redis_data:

services:
  # ==========================================
  # 1. PHYSICAL DATABASE TIER (OPTION B)
  # ==========================================
  
  # Dedicated User Database
  db-user-postgres:
    image: postgres:15-alpine
    container_name: sfo-user-db
    restart: always
    environment:
      POSTGRES_DB: sfo_user_core
      POSTGRES_USER: sfo_user_admin
      POSTGRES_PASSWORD: sfo_user_password_123
    ports:
      - "5432:5432"
    volumes:
      - user_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_user_admin -d sfo_user_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  # Dedicated Restaurant Database
  db-restaurant-postgres:
    image: postgres:15-alpine
    container_name: sfo-restaurant-db
    restart: always
    environment:
      POSTGRES_DB: sfo_restaurant_core
      POSTGRES_USER: sfo_restaurant_admin
      POSTGRES_PASSWORD: sfo_restaurant_password_123
    ports:
      - "5433:5432" # Maps host 5433 to container 5432
    volumes:
      - restaurant_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_restaurant_admin -d sfo_restaurant_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  # Dedicated Order Database
  db-order-postgres:
    image: postgres:15-alpine
    container_name: sfo-order-db
    restart: always
    environment:
      POSTGRES_DB: sfo_order_core
      POSTGRES_USER: sfo_order_admin
      POSTGRES_PASSWORD: sfo_order_password_123
    ports:
      - "5434:5432" # Maps host 5434 to container 5432
    volumes:
      - order_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_order_admin -d sfo_order_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  # Document DB (Owned by Menu Service & Tracking Logs)
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

  # Cache Engine (Owned by Menu Service)
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

  # ==========================================
  # 2. API GATEWAY ROUTER (NGINX)
  # ==========================================
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

  # ==========================================
  # 3. CONTAINERIZED MICROSERVICES
  # ==========================================
  user-service:
    build:
      context: ./services/user
      dockerfile: Dockerfile
    container_name: sfo-user-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_user_admin:sfo_user_password_123@db-user-postgres:5432/sfo_user_core
    depends_on:
      db-user-postgres:
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
      - DATABASE_URL=postgresql://sfo_restaurant_admin:sfo_restaurant_password_123@db-restaurant-postgres:5432/sfo_restaurant_core
      - USER_SERVICE_URL=http://user-service:8001
    depends_on:
      db-restaurant-postgres:
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
      - DATABASE_URL=postgresql://sfo_order_admin:sfo_order_password_123@db-order-postgres:5432/sfo_order_core
      - USER_SERVICE_URL=http://user-service:8001
      - RESTAURANT_SERVICE_URL=http://restaurant-service:8002
      - MENU_SERVICE_URL=http://menu-service:8003
    depends_on:
      db-order-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network
```

---

## 🔒 3. Updated Local Environment File (`.env`)

Update your local development environment variable parameters to decouple credentials:

```ini
# --- Database Connection URLs (Internal Docker Network Routes) ---
USER_DATABASE_URL=postgresql://sfo_user_admin:sfo_user_password_123@db-user-postgres:5432/sfo_user_core
RESTAURANT_DATABASE_URL=postgresql://sfo_restaurant_admin:sfo_restaurant_password_123@db-restaurant-postgres:5432/sfo_restaurant_core
ORDER_DATABASE_URL=postgresql://sfo_order_admin:sfo_order_password_123@db-order-postgres:5432/sfo_order_core

# --- NoSQL & Caching Tier (Owned by Menu/Logging) ---
MENU_MONGO_URI=mongodb://db-nosql:27017/smartfoodops_menus
MENU_REDIS_URL=redis://cache-redis:6379/0
```

---

## 🧪 4. Local Testing with Multi-Port PostgreSQL

Because each PostgreSQL service maps to a distinct port on your local host system, you can connect your local GUI SQL client (like DBeaver or pgAdmin) directly to your sandbox containers:

*   **User DB Client Connect:** `localhost:5432` (user: `sfo_user_admin`, database: `sfo_user_core`)
*   **Restaurant DB Client Connect:** `localhost:5433` (user: `sfo_restaurant_admin`, database: `sfo_restaurant_core`)
*   **Order DB Client Connect:** `localhost:5434` (user: `sfo_order_admin`, database: `sfo_order_core`)
