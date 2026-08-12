# SmartFoodOps — Week 1 API Contracts & Database Initializer (v6 - Decoupled & Robust)

This document provides the raw DDL schema, Pydantic validation specs, and environmental configurations needed to fully implement the core API operations for Week 1. This version utilizes a normalized **`roles` lookup table** instead of a PostgreSQL Enum to define user scopes, stores a JSONB item snapshot on checkout, and prevents duplicate orders. Telemetry and log correlation have been removed to keep Week 1 simple (deferred to Week 3).

Feed this directly to Claude Code to allow it to bootstrap your database and write fully functional, validation-compliant microservices.

---

## 🐘 1. PostgreSQL Database Migrations (`init.sql`)

Save this block as `init.sql`. You can place it in your project's root or mount it directly inside your `db-postgres` volume configuration (under `/docker-entrypoint-initdb.d/init.sql`) to run schema migrations automatically when the container starts.

```sql
-- Enable UUID extension for secure, non-sequential IDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Define custom ENUM types
CREATE TYPE order_status AS ENUM ('created', 'confirmed', 'assigned', 'picked_up', 'delivered', 'cancelled');
CREATE TYPE payment_status AS ENUM ('pending', 'authorized', 'captured', 'refunded');

-- 1a. Roles Lookup Table (Normalized Database Design)
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Seed static user roles on initialization
INSERT INTO roles (name, description) VALUES
('customer', 'App Customer / Order placer'),
('restaurant_admin', 'Restaurant Owner / Menu and Order manager'),
('rider', 'Delivery Partner / Logistics handler'),
('system_admin', 'SFO Platform Operations administrator')
ON CONFLICT (name) DO NOTHING;

-- 1b. Users Table (Core Profiles)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    phone VARCHAR(50) UNIQUE NOT NULL,
    role_id INT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Case-insensitive unique constraint index for emails (prevent duplicate registrations)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));

-- 2. Restaurants Table
CREATE TABLE IF NOT EXISTS restaurants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name VARCHAR(255) NOT NULL,
    address TEXT NOT NULL,
    latitude DECIMAL(9, 6) NOT NULL,
    longitude DECIMAL(9, 6) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    capacity INT NOT NULL DEFAULT 50,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for restaurants geo queries
CREATE INDEX IF NOT EXISTS idx_restaurants_geo ON restaurants(latitude, longitude);

-- 3. Riders Table
CREATE TABLE IF NOT EXISTS riders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_type VARCHAR(100) NOT NULL,
    vehicle_number VARCHAR(100) UNIQUE NOT NULL,
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    current_latitude DECIMAL(9, 6),
    current_longitude DECIMAL(9, 6),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_riders_availability ON riders(is_available);

-- 4. Orders Table (Primary Registry with JSONB Items and Idempotency Guard)
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE RESTRICT,
    rider_id UUID REFERENCES riders(id) ON DELETE SET NULL,
    items JSONB NOT NULL, -- Stores snapshot of ordered items, prices, and selected customization options at checkout
    total_amount DECIMAL(10, 2) NOT NULL,
    status order_status NOT NULL DEFAULT 'created',
    idempotency_key VARCHAR(255) UNIQUE, -- Protects order creation writes against API duplicate submissions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- 5. Payments Table (Built with Idempotency Protection)
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID UNIQUE NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status payment_status NOT NULL DEFAULT 'pending',
    transaction_reference VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

---

## 🍃 2. MongoDB Schema Blueprints

Save these structures in your MongoDB service guidelines. This defines how dynamic schemas (Menus) and state trackers (Order Logs) are modeled.

### **A. Menus Collection (`menus`)**
Stores the complete category, item, pricing, and customization trees for each restaurant.

```json
{
  "_id": "64d50f839a31f24d98a00223", 
  "restaurant_id": "90e0c83a-4424-4f27-bc09-9f5bca981315", 
  "categories": [
    {
      "category_id": "cat_entrees_100",
      "category_name": "Entrées",
      "display_order": 1,
      "items": [
        {
          "item_id": "item_burger_001",
          "name": "Intelligent SFO Burger",
          "description": "Double patty beef burger with signature sauce.",
          "base_price": 12.99,
          "is_available": true,
          "dietary_flags": ["non-veg", "contains-gluten"],
          "customization_groups": [
            {
              "group_id": "grp_add_ons",
              "group_name": "Select Add-Ons",
              "min_selection": 0,
              "max_selection": 3,
              "options": [
                { "name": "Extra Cheddar Cheese", "extra_price": 1.50 },
                { "name": "Smoked Bacon", "extra_price": 2.25 }
              ]
            }
          ]
        }
      ]
    }
  ],
  "created_at": "2026-08-11T08:00:00Z",
  "updated_at": "2026-08-11T08:00:00Z"
}
```

### **B. Order Tracking Logs Collection (`order_tracking_logs`)**
Consolidates the status history, the service trigger origin, and the exact raw logs generated during operational transitions.

```json
{
  "_id": "64d51abf9a31f24d98a00259",
  "order_id": "a4d33ebc-6624-4b47-ab09-9f2bca98131a", 
  "status_history": [
    {
      "status": "created",
      "timestamp": "2026-08-11T08:15:30Z",
      "service": "order-service", 
      "raw_log": "{\"event\": \"order_received\", \"total_amount\": 12.99, \"items_count\": 1}", 
      "updated_by": "customer_client",
      "metadata": {
        "device_ip": "192.168.1.104",
        "user_agent": "iOS/16.5 (SmartFoodOps Customer App)"
      }
    }
  ]
}
```

---

## 📝 3. FastAPI Data Models & Validation Schemas

Feed these Python classes directly to Claude Code. They provide the complete Pydantic v2 schemas required to validate payload entries across all microservices.

### **User Service validation (`services/user/schemas.py`)**
```python
from pydantic import BaseModel, EmailStr, Field
from enum import Enum
from uuid import UUID

class UserRole(str, Enum):
    customer = "customer"
    restaurant_admin = "restaurant_admin"
    rider = "rider"
    system_admin = "system_admin"

class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=2)
    phone: str = Field(..., min_length=8)
    role: UserRole = UserRole.customer

class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    phone: str
    role: str  # Resolves database roles table lookup via SQL Join query on role_id

    class Config:
        from_attributes = True
```

### **Restaurant Service Validation (`services/restaurant/schemas.py`)**
```python
from pydantic import BaseModel, Field
from uuid import UUID

class RestaurantOnboardRequest(BaseModel):
    owner_id: UUID
    name: str = Field(..., min_length=2)
    address: str = Field(..., min_length=5)
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    capacity: int = Field(50, gt=0)

class RestaurantResponse(BaseModel):
    id: UUID
    owner_id: UUID
    name: str
    address: str
    latitude: float
    longitude: float
    is_active: bool
    capacity: int

    class Config:
        from_attributes = True
```

### **Menu Service Validation (`services/menu/schemas.py`)**
```python
from pydantic import BaseModel, Field
from typing import List, Optional
from uuid import UUID

class CustomOption(BaseModel):
    name: str
    extra_price: float = Field(0.0, ge=0.0)

class CustomizationGroup(BaseModel):
    group_id: str
    group_name: str
    min_selection: int = Field(1, ge=0)
    max_selection: int = Field(1, ge=1)
    options: List[CustomOption]

class MenuItem(BaseModel):
    item_id: str
    name: str
    description: str
    base_price: float = Field(..., gt=0.0)
    is_available: bool = True
    dietary_flags: List[str] = []
    customization_groups: List[CustomizationGroup] = []

class MenuCategory(BaseModel):
    category_id: str
    category_name: str
    display_order: int = Field(1, ge=1)
    items: List[MenuItem]

class MenuUpsertRequest(BaseModel):
    restaurant_id: UUID
    categories: List[MenuCategory]

class MenuResponse(BaseModel):
    restaurant_id: UUID
    categories: List[MenuCategory]

class OrderTrackingLogCreateRequest(BaseModel):
    order_id: UUID
    status: str
    service: str
    raw_log: str
    updated_by: Optional[str] = "system"
    metadata: Optional[dict] = None
```

### **Order Service Validation (`services/order/schemas.py`)**
```python
from pydantic import BaseModel, Field
from typing import List, Optional
from uuid import UUID

class OrderItemSelection(BaseModel):
    item_id: str
    quantity: int = Field(..., gt=0)
    customizations: Optional[dict] = None  # To capture dynamic modifications

class OrderCreateRequest(BaseModel):
    customer_id: UUID
    restaurant_id: UUID
    items: List[OrderItemSelection]
    total_amount: float = Field(..., gt=0.0)
    idempotency_key: Optional[str] = Field(None, description="Client-provided unique transaction tracking ID")

class OrderResponse(BaseModel):
    id: UUID
    customer_id: UUID
    restaurant_id: UUID
    items: List[OrderItemSelection]
    total_amount: float
    status: str
    idempotency_key: Optional[str]

    class Config:
        from_attributes = True
```

---

## ⚙️ 4. Environment Variables Configuration (`.env`)

Save this as `.env` in the root of your project directory:

```env
# Database Credentials
POSTGRES_DB=smartfoodops_core
POSTGRES_USER=sfo_admin
POSTGRES_PASSWORD=sfo_password_123
POSTGRES_HOST=db-postgres
POSTGRES_PORT=5432

MONGO_URI=mongodb://db-nosql:27017/smartfoodops_menus
REDIS_URL=redis://cache-redis:6379/0

# Services Endpoints (Within Docker Network)
USER_SERVICE_URL=http://user-service:8001
RESTAURANT_SERVICE_URL=http://restaurant-service:8002
MENU_SERVICE_URL=http://menu-service:8003
ORDER_SERVICE_URL=http://order-service:8004
```

---