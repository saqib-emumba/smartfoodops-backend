# SmartFoodOps — PostgreSQL Migration Guide for Menus & Order Tracking Logs (v2)
**Architectural Transition Record — August 18, 2026**

This document provides the complete blueprints, SQL DDL schemas, Docker Compose services, and FastAPI code changes required to migrate the **Menu** and **Order Tracking Logs** from NoSQL (MongoDB) into **PostgreSQL databases** (complying with the database-per-service Option B layout).

In this **v2 blueprint**, following a design review on August 18, the **Order Tracking Logs** table has been consolidated directly into the existing **Order Database (`sfo_order_core` running inside `db-order-postgres`)** instead of a separate database instance. This guarantees strict database-level referential integrity, aligns with Domain-Driven Design (DDD) boundaries, and reduces local development infrastructure overhead while utilizing a Redis Cache-Aside Layer for the Menu Service.

---

## 🗺️ 1. Revamped Service-to-Database Mapping (PostgreSQL Decoupled)

To maintain absolute domain boundaries and credential isolation, we replace the single NoSQL MongoDB container with PostgreSQL storage. The Order Tracking Logs are housed inside the Order Service's database, establishing a cohesive Order Domain:

| Microservice | Primary Storage | Container Name | Host Port | Container Port | Database Name |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **User Service** | PostgreSQL | `sfo-user-db` | `5432` | `5432` | `sfo_user_core` |
| **Restaurant Service** | PostgreSQL | `sfo-restaurant-db` | `5433` | `5432` | `sfo_restaurant_core` |
| **Order Service** *(includes Tracking Logs)* | PostgreSQL | `sfo-order-db` | `5434` | `5432` | `sfo_order_core` |
| **Payment Service** | PostgreSQL | `sfo-payment-db` | `5435` | `5432` | `sfo_payment_core` |
| **Menu Service** | **PostgreSQL** + Redis | `sfo-menu-db` + `sfo-redis` | `5436` + `6379` | `5432` + `6379` | `sfo_menu_core` |

---

## 🐘 2. PostgreSQL DDL Schemas

These SQL tables are designed to replicate the exact structure of the old MongoDB collections using `JSONB` for menus and an append-only transactional design for tracking logs.

### **A. Menu Database Schema (`menus` table in `sfo_menu_core`)**
This table maps a single `restaurant_id` to its entire nested category, item, and option tree in a single row. This enables high-performance single-row fetches.

```sql
-- Run inside sfo-menu-db / sfo_menu_core
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS menus (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id UUID UNIQUE NOT NULL, -- Logical reference to Restaurant Service
    categories JSONB NOT NULL DEFAULT '[]'::jsonb, -- Stores nested categories, items, and option modifiers
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for scanning menus by restaurant
CREATE INDEX IF NOT EXISTS idx_menus_restaurant_id ON menus(restaurant_id);
-- GIN Index for fast nested searching inside menu items
CREATE INDEX IF NOT EXISTS idx_menus_categories_jsonb ON menus USING gin(categories);
```

### **B. Consolidated Tracking Logs Schema (`order_tracking_logs` table in `sfo_order_core`)**
To track order state transitions, we write individual **append-only relational rows** inside the Order Database. This establishes a **strict database-level foreign key constraint** linking logs to orders, ensuring perfect data integrity and automated cascade cleanup.

```sql
-- Run inside sfo-order-db / sfo_order_core (Appended to Week 1 migrations)
CREATE TABLE IF NOT EXISTS order_tracking_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID NOT NULL CONSTRAINT fk_tracking_order REFERENCES orders(id) ON DELETE CASCADE, -- Strict relational integrity
    old_status order_status, -- Optional: logs previous state transition (uses existing enum)
    new_status order_status NOT NULL, -- Current state transition
    updated_by VARCHAR(100) NOT NULL, -- Origin microservice (e.g., 'order-service', 'rider-service', 'system')
    notes TEXT, -- Human-readable explanations / error messages
    metadata JSONB DEFAULT '{}'::jsonb, -- Dynamic event payloads (ETA, GPS coordinates, error stack traces)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index to support rapid loading of chronological tracking timelines for any specific order
CREATE INDEX IF NOT EXISTS idx_tracking_order_timeline ON order_tracking_logs(order_id, created_at DESC);
```

---

## 🐳 3. Updated Docker Compose Infrastructure (`docker-compose.yml`)

This configuration removes the separate `db-tracking-postgres` container and its associated volume, consolidating the database footprint while keeping Redis for menu caching.

```yaml
version: '3.8'

networks:
  smartfoodops-network:
    driver: bridge

volumes:
  user_postgres_data:
  restaurant_postgres_data:
  order_postgres_data:
  payment_postgres_data:
  menu_postgres_data:
  redis_data:

services:
  # ==========================================
  # RELATIONAL STORAGE ENGINES (POSTGRES)
  # ==========================================

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

  db-restaurant-postgres:
    image: postgres:15-alpine
    container_name: sfo-restaurant-db
    restart: always
    environment:
      POSTGRES_DB: sfo_restaurant_core
      POSTGRES_USER: sfo_restaurant_admin
      POSTGRES_PASSWORD: sfo_restaurant_password_123
    ports:
      - "5433:5432"
    volumes:
      - restaurant_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_restaurant_admin -d sfo_restaurant_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  # Houses both the `orders` and `order_tracking_logs` tables
  db-order-postgres:
    image: postgres:15-alpine
    container_name: sfo-order-db
    restart: always
    environment:
      POSTGRES_DB: sfo_order_core
      POSTGRES_USER: sfo_order_admin
      POSTGRES_PASSWORD: sfo_order_password_123
    ports:
      - "5434:5432"
    volumes:
      - order_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_order_admin -d sfo_order_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  db-payment-postgres:
    image: postgres:15-alpine
    container_name: sfo-payment-db
    restart: always
    environment:
      POSTGRES_DB: sfo_payment_core
      POSTGRES_USER: sfo_payment_admin
      POSTGRES_PASSWORD: sfo_payment_password_123
    ports:
      - "5435:5432"
    volumes:
      - payment_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_payment_admin -d sfo_payment_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  # Dedicated Postgres Instance for Menu Service Catalog
  db-menu-postgres:
    image: postgres:15-alpine
    container_name: sfo-menu-db
    restart: always
    environment:
      POSTGRES_DB: sfo_menu_core
      POSTGRES_USER: sfo_menu_admin
      POSTGRES_PASSWORD: sfo_menu_password_123
    ports:
      - "5436:5432"
    volumes:
      - menu_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_menu_admin -d sfo_menu_core"]
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

  # ==========================================
  # CONTAINERIZED MICROSERVICES (DECOUPLED)
  # ==========================================

  menu-service:
    build:
      context: ./services/menu
      dockerfile: Dockerfile
    container_name: sfo-menu-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_menu_admin:sfo_menu_password_123@db-menu-postgres:5432/sfo_menu_core
      - REDIS_URL=redis://cache-redis:6379/0
    depends_on:
      db-menu-postgres:
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

## 🔌 4. Revised `.env` Configurations

We update the local `.env` blueprint to consolidate the database strings. Notice that the tracking logs write directly to the `ORDER_DATABASE_URL` routing block:

```ini
# --- PostgreSQL Connection Parameters (Option B Decoupled Layout) ---
USER_DATABASE_URL=postgresql://sfo_user_admin:sfo_user_password_123@db-user-postgres:5432/sfo_user_core
RESTAURANT_DATABASE_URL=postgresql://sfo_restaurant_admin:sfo_restaurant_password_123@db-restaurant-postgres:5432/sfo_restaurant_core
ORDER_DATABASE_URL=postgresql://sfo_order_admin:sfo_order_password_123@db-order-postgres:5432/sfo_order_core
PAYMENT_DATABASE_URL=postgresql://sfo_payment_admin:sfo_payment_password_123@db-payment-postgres:5432/sfo_payment_core

# Menu Connection String
MENU_DATABASE_URL=postgresql://sfo_menu_admin:sfo_menu_password_123@db-menu-postgres:5432/sfo_menu_core

# --- Caching Layer ---
MENU_REDIS_URL=redis://cache-redis:6379/0
```

---

## 🐍 5. FastAPI Application Refactoring (Zero API Degradation)

Our API validation payloads remain untouched. We only adjust the database client configurations so that logging records commit natively to the Order Service's engine.

### **A. Menu Service Caching & Write Pipeline (`services/menu/main.py`)**
*(Retained from v1, unmodified to preserve current menu caching mechanics)*

```python
import json
import uuid
from typing import List
from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
import datetime

# --- EXISTING PYDANTIC SCHEMAS (UNDISTURBED) ---
class MenuItem(BaseModel):
    item_id: str
    name: str
    description: str
    base_price: float
    is_available: bool = True

class MenuCategory(BaseModel):
    category_id: str
    category_name: str
    display_order: int
    items: List[MenuItem]

class MenuPayload(BaseModel):
    restaurant_id: uuid.UUID
    categories: List[MenuCategory]

# --- SQLALCHEMY MODELS ---
Base = declarative_base()

class SQLMenu(Base):
    __tablename__ = "menus"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id = Column(UUID(as_uuid=True), unique=True, nullable=False, index=True)
    categories = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=datetime.datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

# Database Engine Initialization
DATABASE_URL = "postgresql+asyncpg://sfo_menu_admin:sfo_menu_password_123@db-menu-postgres:5432/sfo_menu_core"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Redis Connection Setup
REDIS_URL = "redis://cache-redis:6379/0"
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="SFO Menu Service (Postgres Backed)")

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- API ENDPOINTS (PRESERVING EXACT API CONTRACTS) ---

@app.get("/api/v1/menus/{restaurant_id}", response_model=MenuPayload)
async def get_menu(restaurant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    cache_key = f"menu:{restaurant_id}"
    
    # Step 1: Query Redis Cache
    try:
        cached_menu = await redis_client.get(cache_key)
        if cached_menu:
            return MenuPayload.parse_raw(cached_menu)
    except Exception as e:
         print(f"Redis lookup failed (fallback to Postgres): {e}")

    # Step 2: Query PostgreSQL JSONB
    from sqlalchemy.future import select
    query = select(SQLMenu).where(SQLMenu.restaurant_id == restaurant_id)
    result = await db.execute(query)
    sql_menu = result.scalars().first()

    if not sql_menu:
        raise HTTPException(status_code=404, detail="Menu not found for this restaurant.")

    # Convert SQLAlchemy model structure into the validated Pydantic contract
    menu_data = MenuPayload(
        restaurant_id=sql_menu.restaurant_id,
        categories=sql_menu.categories
    )

    # Step 3: Populate Redis asynchronously with TTL of 1 hour
    try:
        await redis_client.setex(cache_key, 3600, menu_data.json())
    except Exception as e:
        print(f"Failed to populate Redis: {e}")

    return menu_data


@app.post("/api/v1/menus", status_code=status.HTTP_201_CREATED)
async def save_or_update_menu(payload: MenuPayload, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.future import select
    
    query = select(SQLMenu).where(SQLMenu.restaurant_id == payload.restaurant_id)
    result = await db.execute(query)
    sql_menu = result.scalars().first()

    categories_json = [cat.dict() for cat in payload.categories]

    if sql_menu:
        sql_menu.categories = categories_json
        sql_menu.updated_at = datetime.datetime.utcnow()
    else:
        new_menu = SQLMenu(
            restaurant_id=payload.restaurant_id,
            categories=categories_json
        )
        db.add(new_menu)

    await db.commit()

    # Invalidate Cache
    cache_key = f"menu:{payload.restaurant_id}"
    await redis_client.delete(cache_key)

    return {"status": "success", "message": "Menu saved successfully and cache invalidated."}
```

### **B. Refactored Order Tracking Logs Handshake (`services/order/logging_worker.py`)**
This logging code now targets the **Order Database engine**. Note that since logs are written asynchronously or as independent transactions, they write to `sfo_order_core` under the `order_tracking_logs` table.

```python
from pydantic import BaseModel
import uuid
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
import datetime

# --- PYDANTIC LOG SCHEMA (UNDISTURBED) ---
class TrackingLogPayload(BaseModel):
    order_id: uuid.UUID
    old_status: Optional[str] = None
    new_status: str
    updated_by: str
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = {}

# --- SQLALCHEMY MODEL ---
Base = declarative_base()

class SQLTrackingLog(Base):
    __tablename__ = "order_tracking_logs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    old_status = Column(String(50), nullable=True)
    new_status = Column(String(50), nullable=False)
    updated_by = Column(String(100), nullable=False)
    notes = Column(Text, nullable=True)
    metadata = Column(JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.datetime.utcnow)

# Targets our existing Order PostgreSQL instance
ORDER_DATABASE_URL = "postgresql+asyncpg://sfo_order_admin:sfo_order_password_123@db-order-postgres:5432/sfo_order_core"
engine = create_async_engine(ORDER_DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def write_tracking_log(log_payload: TrackingLogPayload):
    """
    Appends a new state tracking entry into the core sfo_order_core database.
    Because the table is append-only, it prevents MVCC lock contention on the primary 'orders' table rows.
    """
    async with AsyncSessionLocal() as session:
        new_log = SQLTrackingLog(
            order_id=log_payload.order_id,
            old_status=log_payload.old_status,
            new_status=log_payload.new_status,
            updated_by=log_payload.updated_by,
            notes=log_payload.notes,
            metadata=log_payload.metadata
        )
        session.add(new_log)
        await session.commit()
```

---

## 🏆 6. Architectural Benefits & Mentor Arguments

When presenting this **v2 design** in your **Mentor Review**, point out these structural benefits:

1. **Strict Database-Level Cascade Integrity**: By housing `order_tracking_logs` in the same database instance as the `orders` table, we establish a **strict SQL foreign key (`ON DELETE CASCADE`)**. This prevents orphaned logs and guarantees absolute referential integrity—which is impossible with cross-instance SQL or MongoDB.
2. **Reduced Local Compute Footprint**: We removed a dedicated PostgreSQL container and volume, freeing up system resources for local development without violating independent microservice boundaries (since the logs naturally fall under the Order Domain).
3. **No Service Code Redesign**: Your existing Pydantic validation models, FastAPI endpoints, and routing mechanisms are **completely undisturbed**.
4. **Append-Only Performance Shield**: We do not update a nested array on the main `orders` table (which would trigger catastrophic Postgres MVCC row duplication and disk write-amplification). By keeping the `order_tracking_logs` as an **append-only relational table**, writes remain incredibly lightweight and fast.
