# SmartFoodOps — Payments Service Separation & Migration Blueprint

This document details the migration plan to split payment processing out of the **Order Service** and establish a dedicated, secure, and physically isolated **Payment Service** with its own transactional PostgreSQL database.

---

## 🗺️ 1. Architectural Rationale: Why Split Payments?

In our early iterations, the Order Service managed both orders and payments [v4]. However, to meet production-grade scalability, security, and reliability targets, we must isolate them:

1. **PCI-DSS Compliance Isolation**: Direct card handling requires strict, high-cost security auditing. Splitting payments into its own lightweight service isolates the compliance boundary. Only the Payment Service database and container need to be locked down in highly secure, restricted VPC subnets.
2. **Fault Blast Radius Containment**: Payment processing relies on external third-party gateways (e.g., Stripe, Adyen). If these external APIs experience high latencies, throttling, or outages, thread pools in a combined service would starve. Splitting payments ensures that gateway outages never prevent users from placing, reading, or tracking their active orders.
3. **Temporal Saga Orchestration (Week 2 Preparation)**: With separate services, the **Temporal Engine** can choreograph clean, decoupled transaction activities:
   - `OrderService.create_order_record()` (Relational PostgreSQL write)
   - `PaymentService.authorize_payment()` (External API gateway handshake)
   - If authorization fails, Temporal can automatically invoke compensation workflows (reverting the order record on the Order Service) without database deadlocks.

---

## 💾 2. Relational Database Isolation (Option B)

In a true Database-per-Service (Option B) layout, **physical foreign key constraints between databases are impossible**. Therefore, the Payment database will store a **logical logical reference** to the `order_id` as a simple UUID column without direct SQL references [v4].

### **Service & Port Mappings**

| Microservice | Database Container | Database Name | Internal Port | External Port | Default User |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Order Service** | `sfo-order-db` | `sfo_order_core` | `5432` | `5434` | `sfo_order_admin` |
| **Payment Service** | `sfo-payment-db` | `sfo_payment_core` | `5432` | `5435` | `sfo_payment_admin` |

### **The Payments Database SQL Schema (`db-payment-postgres`)**

Create the separate database schema inside your new payments engine. Note the removal of the direct SQL reference to the `orders` table to preserve database decoupling:

```sql
-- Create Enum Type for Payments Status
CREATE TYPE payment_status AS ENUM ('pending', 'authorized', 'captured', 'refunded');

-- Payments Table (Completely Isolated Storage)
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Logical Reference: UUID stored directly without physical "REFERENCES orders(id)" constraint
    order_id UUID UNIQUE NOT NULL, 
    
    -- Idempotency Guard: Protects transactions against double-charging under network retry conditions
    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
    
    amount DECIMAL(10, 2) NOT NULL,
    status payment_status NOT NULL DEFAULT 'pending',
    transaction_reference VARCHAR(255), -- References external gateway IDs (e.g. Stripe charge_id)
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Optimize queries searching payments by order ID
CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
-- Optimize idempotency checks
CREATE INDEX IF NOT EXISTS idx_payments_idempotency ON payments(idempotency_key);
```

---

## 🐳 3. Docker Orchestration Updates (`docker-compose.yml`)

Add your new dedicated payment database and service container to your local Docker Compose networks.

```yaml
version: '3.8'

networks:
  smartfoodops-network:
    driver: bridge

volumes:
  order_postgres_data:
  payment_postgres_data: # New volume for Payments DB

services:
  # --- Existing Order Database ---
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

  # --- NEW: Dedicated Payment Database ---
  db-payment-postgres:
    image: postgres:15-alpine
    container_name: sfo-payment-db
    restart: always
    environment:
      POSTGRES_DB: sfo_payment_core
      POSTGRES_USER: sfo_payment_admin
      POSTGRES_PASSWORD: sfo_payment_password_123
    ports:
      - "5435:5432" # Host 5435 maps to Container 5432
    volumes:
      - payment_postgres_data:/var/lib/postgresql/data
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sfo_payment_admin -d sfo_payment_core"]
      interval: 5s
      timeout: 5s
      retries: 5

  # --- NEW: Dedicated Payment Service Container ---
  payment-service:
    build:
      context: ./services/payment
      dockerfile: Dockerfile
    container_name: sfo-payment-service
    restart: always
    environment:
      - DATABASE_URL=postgresql://sfo_payment_admin:sfo_payment_password_123@db-payment-postgres:5432/sfo_payment_core
      - ORDER_SERVICE_URL=http://order-service:8004
    depends_on:
      db-payment-postgres:
        condition: service_healthy
    networks:
      - smartfoodops-network
```

---

## 🔀 4. API Gateway Routing Updates (`api-gateway/nginx.conf`)

Add a dedicated reverse-proxy rule inside your Nginx configurations to route payment endpoints securely to the new service container:

```nginx
# 💳 Route Payment service requests (Port 8005)
location /api/v1/payments {
    proxy_pass http://payment-service:8005;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

---

## 🐍 5. FastAPI Service Contract Boilerplate (`services/payment/main.py`)

This outlines the separate FastAPI implementation with strict idempotency protections to ensure customers are never double-charged:

```python
from fastapi import FastAPI, Header, HTTPException, status, Depends
from pydantic import BaseModel, Field
from typing import Optional
from uuid import UUID
import uvicorn

app = FastAPI(title="SFO Payment Service", version="1.0.0")

# Request validation schemas
class PaymentCreateRequest(BaseModel):
    order_id: UUID = Field(..., description="Logical reference to order ID")
    amount: float = Field(..., gt=0.0, description="Payment amount")
    idempotency_key: str = Field(..., description="Mandatory unique transaction key")

class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    amount: float
    status: str
    transaction_reference: Optional[str] = None

# POST endpoint with Idempotency protections
@app.post("/api/v1/payments", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
async def process_payment(
    payload: PaymentCreateRequest,
    x_idempotency_key: Optional[str] = Header(None)
):
    # 1. Enforce Idempotency header existence
    if not x_idempotency_key or x_idempotency_key != payload.idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mandatory X-Idempotency-Key header is missing or mismatched with payload"
        )
    
    # 2. Check Database for existing payment transaction
    # db_transaction = await db.fetch_one("SELECT * FROM payments WHERE idempotency_key = :key", {"key": x_idempotency_key})
    db_transaction = None # Placeholder for DB check
    
    if db_transaction:
        # Bypasses calculations & integrations, safely returning the original transaction
        return PaymentResponse(
            id=db_transaction["id"],
            order_id=db_transaction["order_id"],
            amount=db_transaction["amount"],
            status=db_transaction["status"],
            transaction_reference=db_transaction["transaction_reference"]
        )
        
    # 3. Simulate Payment authorization with gateway (e.g. Stripe)
    # response = stripe.Charge.create(...)
    gateway_reference = "ch_mock_stripe_abc123"
    
    # 4. Insert records into isolated payment DB
    # await db.execute("INSERT INTO payments...")
    
    return PaymentResponse(
        id="f3b9c8d5-1234-5678-abcd-ef1234567890",
        order_id=payload.order_id,
        amount=payload.amount,
        status="authorized",
        transaction_reference=gateway_reference
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8005)
```

---

## 🛠️ 6. Service Integration Flow

When a user places an order, the transaction coordinates across service boundaries:

```
[Customer] --(POST /api/v1/orders)--> [OrderSvc]
                                        |
                            (Verifies Items & Prices)
                                        |
                                        v
                                    [MenuSvc]
                                        |
                          (Creates Order in Postgres DB)
                                        |
                                        v
    [Temporal Workflow Engine / Sagas (Coordinates Phase 2)]
         |                                           |
         v                                           v
 [OrderSvc (Sets status='pending')]        [PaymentSvc (POST /api/v1/payments)]
                                                     |
                                            (Charges Card & Logs DB)
```

By keeping services decoupled, you ensure maximum high-volume scalability and flawless state tracking without locking databases!
