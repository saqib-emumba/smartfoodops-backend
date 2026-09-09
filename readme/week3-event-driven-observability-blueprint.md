# SmartFoodOps — Week 3 Implementation Blueprint
**Decoupled Event-Driven Core & Observability Suite**

This document serves as an exhaustive, copy-pasteable implementation prompt designed for **Claude / Cursor / ChatGPT** to set up the entire **Week 3** architecture of **SmartFoodOps** in one go.

By completing this blueprint, the platform moves from a synchronous request-driven system to an **asynchronous, event-driven choreographic pipeline** with complete **production-grade observability (Distributed Tracing, System Metrics, and Live Analytics)**.

---

## 🗺️ 1. Directory Structure Update
Ensure your repository matches or is updated to support the following folders. All new configurations and workers live within these clean boundaries:

```text
/workspace/
├── docker-compose.yml              # Updated to include Kafka, RabbitMQ, Celery, Prometheus, Grafana, Jaeger
├── .env                            # Decoupled credential variables
├── prometheus.yml                  # Scrape config for microservice metrics
├── services/
│   ├── common/                     # Shared chassis imported by all microservices
│   │   ├── common/
│   │   │   ├── kafka_client.py     # NEW: Shared Async Kafka Producer and Consumer boilerplate
│   │   │   ├── telemetry.py        # NEW: OpenTelemetry & Prometheus middleware / bootstrap
│   │   │   ├── logging_config.py   # UPDATED: Injects correlation_id/trace_id into standard logs
│   │   │   └── config.py
│   ├── order/                      # Order microservice
│   │   └── main.py                 # Emits 'Order Placed' and 'Order Cancelled' events to Kafka
│   ├── payment/                    # Payment microservice
│   │   └── main.py                 # Emits 'Payment Authorized' and 'Payment Cancelled' events
│   ├── orchestration/              # Isolated Temporal Workflow engine
│   │   └── workflows.py            # Emits workflow status event triggers to Kafka
│   ├── notification/               # NEW: Celery background worker consuming from RabbitMQ
│   │   ├── Dockerfile
│   │   ├── tasks.py                # Simulated SMS, Email, and Push notifications
│   │   └── worker.py               # Celery app bootstrapper
│   ├── analytics/                  # NEW: Independent Kafka Consumer compiling business KPIs
│   │   ├── Dockerfile
│   │   ├── main.py                 # Exposes current KPIs on /metrics for Prometheus
│   │   └── consumer.py             # Continuous event loop processing events from Kafka
```

---

## 🐳 2. Infrastructure Setup (`docker-compose.yml`)
Add the following blocks for **Kafka (KRaft mode)**, **RabbitMQ**, **Celery**, **Jaeger**, **Prometheus**, and **Grafana** into your main `docker-compose.yml`.

Ensure that you merge this with your existing PostgreSQL databases, FastAPI microservices, Redis, and Temporal.

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
  kafka_data:
  prometheus_data:
  grafana_data:

services:
  # =========================================================================
  # ASYNC MESSAGING & TELEMETRY INFRASTRUCTURE
  # =========================================================================

  # Kafka Message Broker (KRaft Mode - No Zookeeper Needed)
  kafka:
    image: confluentinc/cp-kafka:7.4.0
    container_name: sfo-kafka
    restart: always
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: 'CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT'
      KAFKA_ADVERTISED_LISTENERS: 'PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092'
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_PROCESS_ROLES: 'broker,controller'
      KAFKA_CONTROLLER_QUORUM_VOTERS: '1@kafka:29093'
      KAFKA_LISTENERS: 'PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:29093,PLAINTEXT_HOST://0.0.0.0:9092'
      KAFKA_INTER_BROKER_LISTENER_NAME: 'PLAINTEXT'
      KAFKA_CONTROLLER_LISTENER_NAMES: 'CONTROLLER'
      KAFKA_LOG_DIRS: '/tmp/kraft-combined-logs'
      CLUSTER_ID: 'MkU3OEVBNTcwNTJENDM2Qk'
    ports:
      - "9092:9092"
    volumes:
      - kafka_data:/var/lib/kafka/data
    networks:
      - smartfoodops-network

  # Confluent Schema Registry (Schema Contract Enforcement)
  schema-registry:
    image: confluentinc/cp-schema-registry:7.4.0
    container_name: sfo-schema-registry
    restart: always
    depends_on:
      - kafka
    ports:
      - "8081:8081"
    environment:
      SCHEMA_REGISTRY_HOST_NAME: schema-registry
      SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS: 'kafka:29092'
      SCHEMA_REGISTRY_LISTENERS: http://0.0.0.0:8081
    networks:
      - smartfoodops-network

  # RabbitMQ Message Broker (Celery Task Inbox)
  rabbitmq:
    image: rabbitmq:3-management-alpine
    container_name: sfo-rabbitmq
    restart: always
    ports:
      - "5672:5672"     # RabbitMQ connection port
      - "15672:15672"   # Management Web Dashboard
    environment:
      RABBITMQ_DEFAULT_USER: sfo_rabbit_admin
      RABBITMQ_DEFAULT_PASS: sfo_rabbit_password_123
    networks:
      - smartfoodops-network
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3

  # Jaeger (Distributed Tracing UI)
  jaeger:
    image: jaegertracing/all-in-one:1.47
    container_name: sfo-jaeger
    restart: always
    ports:
      - "16686:16686"   # Web UI for developers
      - "4317:4317"     # OTLP gRPC collector
      - "4318:4318"     # OTLP HTTP collector
    networks:
      - smartfoodops-network

  # Prometheus (Time-Series Metric Scraping Server)
  prometheus:
    image: prom/prometheus:v2.45.0
    container_name: sfo-prometheus
    restart: always
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    networks:
      - smartfoodops-network

  # Grafana (Metrics Dashboards Visualization)
  grafana:
    image: grafana/grafana:10.0.0
    container_name: sfo-grafana
    restart: always
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
    networks:
      - smartfoodops-network

  # =========================================================================
  # ASYNC COMPUTATION SERVICES
  # =========================================================================

  # Notification Celery Worker Service
  notification-worker:
    build:
      context: .
      dockerfile: services/notification/Dockerfile
    container_name: sfo-notification-worker
    restart: always
    environment:
      - CELERY_BROKER_URL=amqp://sfo_rabbit_admin:sfo_rabbit_password_123@rabbitmq:5672//
      - CELERY_RESULT_BACKEND=redis://cache-redis:6379/2
      - KAFKA_BOOTSTRAP_SERVERS=kafka:29092
      - JAEGER_ENDPOINT=http://jaeger:4318/v1/traces
    depends_on:
      rabbitmq:
        condition: service_healthy
    networks:
      - smartfoodops-network

  # Live Analytics Engine Service
  analytics-service:
    build:
      context: .
      dockerfile: services/analytics/Dockerfile
    container_name: sfo-analytics-service
    restart: always
    environment:
      - KAFKA_BOOTSTRAP_SERVERS=kafka:29092
      - JAEGER_ENDPOINT=http://jaeger:4318/v1/traces
      - DATABASE_URL=postgresql://sfo_order_admin:sfo_order_password_123@db-order-postgres:5432/sfo_order_core
    ports:
      - "8008:8008"   # Exposes /metrics endpoints for Prometheus
    depends_on:
      - kafka
    networks:
      - smartfoodops-network
```

---

## 🔒 3. Updated Local Environment File (`.env`)
Append the following variables to your existing `.env` file:

```ini
# --- Message Brokers & Event Infrastructure ---
KAFKA_BOOTSTRAP_SERVERS=kafka:29092
SCHEMA_REGISTRY_URL=http://schema-registry:8081
RABBITMQ_URL=amqp://sfo_rabbit_admin:sfo_rabbit_password_123@rabbitmq:5672//

# --- Observability Infrastructure Ends ---
JAEGER_ENDPOINT=http://jaeger:4318/v1/traces
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
```

---

## 📈 4. Prometheus Configuration (`prometheus.yml`)
Create `prometheus.yml` in your root folder to instruct Prometheus on where to scrape application telemetry from:

```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'user-service'
    static_configs:
      - targets: ['user-service:8001']

  - job_name: 'restaurant-service'
    static_configs:
      - targets: ['restaurant-service:8002']

  - job_name: 'menu-service'
    static_configs:
      - targets: ['menu-service:8003']

  - job_name: 'order-service'
    static_configs:
      - targets: ['order-service:8004']

  - job_name: 'payment-service'
    static_configs:
      - targets: ['payment-service:8005']

  - job_name: 'analytics-service'
    static_configs:
      - targets: ['analytics-service:8008']
```

---

## 🛠️ 5. Phase-by-Phase Integration Plan

To implement the entire pipeline flawlessly, guide Claude to complete the changes in these **five sequential phases**:

### 📦 Phase A: Update Shared Chassis (`services/common/`)
Update your shared common chassis library first. Every service inherits these libraries automatically, so writing telemetry and Kafka code here ensures universal contract enforcement.

#### **A.1 Shared Kafka Client (`services/common/common/kafka_client.py`)**
Create an async-safe Kafka wrapper using the `aiokafka` package. This standardizes how events are published and consumed with schema safety.

```python
import json
import logging
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from typing import Dict, Any

logger = logging.getLogger("sfo_kafka_client")

class AsyncKafkaProducer:
    def __init__(self, bootstrap_servers: str):
        self.bootstrap_servers = bootstrap_servers
        self.producer = None

    async def start(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        await self.producer.start()
        logger.info("Async Kafka Producer started.")

    async def send_event(self, topic: str, key: str, payload: Dict[str, Any]):
        if not self.producer:
            await self.start()
        try:
            # Enforce unified event structural contract before publishing
            event_envelope = {
                "event_type": topic,
                "key": key,
                "payload": payload,
                "timestamp": payload.get("timestamp") or str(payload) # Use current time if blank
            }
            await self.producer.send_and_wait(topic, value=event_envelope, key=key.encode('utf-8'))
            logger.info(f"Successfully published event {topic} for key {key}")
        except Exception as e:
            logger.error(f"Failed to publish event {topic} to Kafka: {e}")
            raise e

    async def stop(self):
        if self.producer:
            await self.producer.stop()
            logger.info("Async Kafka Producer stopped.")
```

#### **A.2 Telemetry & Distributed Tracing Bootstrapper (`services/common/common/telemetry.py`)**
Configure the global instrumentation pipeline. It wires OpenTelemetry tracing to export to Jaeger and configures Prometheus metric hooks.

```python
import time
from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

# Define Unified Application Metrics
REQUEST_COUNT = Counter(
    "sfo_api_requests_total", "Total count of HTTP API requests", ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "sfo_api_request_latency_seconds", "HTTP request latency in seconds", ["method", "endpoint"]
)

def setup_telemetry(app: FastAPI, service_name: str, jaeger_endpoint: str):
    \"\"\"
    Instruments FastAPI with OpenTelemetry Distributed Tracing (Jaeger) 
    and exposes Prometheus Metrics.
    \"\"\"
    # Setup OpenTelemetry
    provider = TracerProvider()
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=jaeger_endpoint))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # Instrument FastAPI natively with OTel middleware
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    # Middleware to log API latency and count metrics for Prometheus
    @app.middleware("http")
    async def prometheus_metrics_middleware(request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time
        
        # Track metrics
        REQUEST_COUNT.labels(
            method=request.method, 
            endpoint=request.url.path, 
            status=response.status_code
        ).inc()
        REQUEST_LATENCY.labels(
            method=request.method, 
            endpoint=request.url.path
        ).observe(duration)
        
        return response

    # Standard metrics endpoint for Prometheus scraping
    @app.get("/metrics")
    def metrics_endpoint():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

#### **A.3 Correlated Logging Engine (`services/common/common/logging_config.py`)**
Configure python loggers to automatically capture the active OpenTelemetry span trace IDs, printing them alongside regular terminal logs so you can match system processes.

```python
import logging
from opentelemetry import trace

class CorrelatedLogFilter(logging.Filter):
    def filter(self, record):
        span = trace.get_current_span()
        if span and span.get_span_context().is_valid:
            record.trace_id = format(span.get_span_context().trace_id, "032x")
            record.span_id = format(span.get_span_context().span_id, "16x")
        else:
            record.trace_id = "N/A"
            record.span_id = "N/A"
        return True

def configure_correlated_logging():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '[SFO] %(asctime)s - %(name)s - %(levelname)s - [TraceID: %(trace_id)s SpanID: %(span_id)s] - %(message)s'
    )
    handler.setFormatter(formatter)
    handler.addFilter(CorrelatedLogFilter())
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
```

---

### 📡 Phase B: Instrument Core Microservices (Order & Payment)
Integrate the Kafka Client and Telemetry setups inside the entrypoints of your core services.

#### **B.1 Order Service Integration (`services/order/main.py`)**
Expose the async producer and wire up events during order creations/state modifications.

```python
import os
import uuid
from fastapi import FastAPI, Depends, status
from common.kafka_client import AsyncKafkaProducer
from common.telemetry import setup_telemetry
from common.logging_config import configure_correlated_logging
import logging

configure_correlated_logging()
logger = logging.getLogger("sfo_order_service")

app = FastAPI(title="SFO Order Service")

# Initialize and integrate the Shared Telemetry Config
setup_telemetry(
    app=app, 
    service_name="order-service", 
    jaeger_endpoint=os.getenv("JAEGER_ENDPOINT", "http://jaeger:4318/v1/traces")
)

# Boot up the Kafka Producer on app startup
kafka_producer = AsyncKafkaProducer(bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"))

@app.on_event("startup")
async def startup_event():
    await kafka_producer.start()

@app.on_event("shutdown")
async def shutdown_event():
    await kafka_producer.stop()

# --- Example of publishing events asynchronously during checkout ---
@app.post("/api/v1/orders", status_code=status.HTTP_201_CREATED)
async def create_order(order_payload: dict):
    # 1. Commit basic record in sfo_order_core Postgres...
    order_id = str(uuid.uuid4())
    logger.info(f"Committed Order record to postgres: {order_id}")

    # 2. Asynchronously broadcast the order intent to the Kafka cluster
    await kafka_producer.send_event(
        topic="order_placed",
        key=order_id,
        payload={
            "order_id": order_id,
            "customer_id": order_payload.get("customer_id"),
            "restaurant_id": order_payload.get("restaurant_id"),
            "total_amount": order_payload.get("total_amount"),
            "status": "created"
        }
    )
    return {"status": "success", "order_id": order_id}
```

---

### ✉️ Phase C: Build Background Workers (Celery & Notification)
Implement asynchronous out-of-process notification routines using Celery.

#### **C.1 Notification Dockerfile (`services/notification/Dockerfile`)**
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy shared library chassis
COPY services/common /services/common
RUN pip install --no-cache-dir -e /services/common

# Copy microservice requirements
COPY services/notification/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/notification/ .

CMD ["celery", "-A", "worker.celery_app", "worker", "--loglevel=info"]
```

#### **C.2 Notification Tasks (`services/notification/tasks.py`)**
Write the tasks triggered by events. In a production system, these handle HTTP connections with SMS APIs or email gateways.

```python
import logging
import time

logger = logging.getLogger("sfo_notification_worker")

def send_customer_sms(order_id: str, status: str, phone: str):
    logger.info(f"[SMS DISPATCH START] Order {order_id} has moved to status: {status}")
    # Simulate API connection latency
    time.sleep(1.5)
    logger.info(f"[SMS DISPATCH SUCCESS] Confirmed message sent to customer phone: {phone}")

def send_customer_email(order_id: str, status: str, email: str):
    logger.info(f"[EMAIL DISPATCH START] Creating transaction invoice PDF for Order {order_id}")
    time.sleep(2.0)
    logger.info(f"[EMAIL DISPATCH SUCCESS] Invoice dispatched cleanly to {email}")
```

#### **C.3 Celery App & Queue Listener (`services/notification/worker.py`)**
This module initializes the Celery app, listens directly to the Kafka bus, and offloads notifications dynamically.

```python
import os
import asyncio
from celery import Celery
from aiokafka import AIOKafkaConsumer
import json
import threading
from tasks import send_customer_sms, send_customer_email

# Configure Celery
celery_app = Celery(
    "notification_tasks",
    broker=os.getenv("CELERY_BROKER_URL", "amqp://sfo_rabbit_admin:sfo_rabbit_password_123@rabbitmq:5672//"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://cache-redis:6379/2")
)

# Background Kafka-to-Celery Bridge Thread
def start_kafka_consumer_bridge():
    async def listen():
        consumer = AIOKafkaConsumer(
            "order_confirmed", "order_cancelled",
            bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
            group_id="notification-bridge-group",
            value_deserializer=lambda m: json.loads(m.decode('utf-8'))
        )
        await consumer.start()
        try:
            async for msg in consumer:
                event_type = msg.value.get("event_type")
                payload = msg.value.get("payload", {})
                order_id = payload.get("order_id")
                
                # Offload heavy tasks asynchronously to out-of-process Celery Workers
                if event_type == "order_confirmed":
                    celery_app.send_task("worker.dispatch_sms", args=[order_id, "confirmed", "+923001234567"])
                    celery_app.send_task("worker.dispatch_email", args=[order_id, "confirmed", "customer@gmail.com"])
                elif event_type == "order_cancelled":
                    celery_app.send_task("worker.dispatch_sms", args=[order_id, "cancelled", "+923001234567"])
        finally:
            await consumer.stop()

    loop = asyncio.new_event_loop()
    loop.run_until_complete(listen())

# Boot consumer bridge in a background daemon thread
threading.Thread(target=start_kafka_consumer_bridge, daemon=True).start()

# Registered Celery Tasks
@celery_app.task(name="worker.dispatch_sms")
def dispatch_sms(order_id: str, status: str, phone: str):
    send_customer_sms(order_id, status, phone)

@celery_app.task(name="worker.dispatch_email")
def dispatch_email(order_id: str, status: str, email: str):
    send_customer_email(order_id, status, email)
```

---

### 📊 Phase D: Create Live Analytics Service
Build an independent, high-throughput consumer to harvest business metrics without stressing SQL connections.

#### **D.1 Analytics Main Service (`services/analytics/main.py`)**
This FastAPI service uses `prometheus_client` to expose operational stats calculated by the async consumer thread.

```python
import os
from fastapi import FastAPI, Response
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
import uvicorn
import threading
from consumer import start_analytics_kafka_consumer

app = FastAPI(title="SFO Real-Time Analytics Dashboard")

# Define High-Value Business Gauges (To be scraped by Prometheus)
TOTAL_ORDERS = Gauge("sfo_business_total_orders", "Total checkout orders placed on SFO")
ORDER_CANCELLATIONS = Gauge("sfo_business_order_cancellations", "Total cancelled orders")
AVG_DELIVERY_TIME_SEC = Gauge("sfo_business_average_delivery_time_seconds", "Average delivery duration in seconds")

@app.get("/metrics")
def get_operational_metrics():
    # Fetch live counts updated by the background Kafka thread
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.on_event("startup")
def start_background_consumer():
    # Run the high-performance event harvester in a separate thread
    threading.Thread(target=start_analytics_kafka_consumer, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8008)
```

#### **D.2 Analytics Event Harvester (`services/analytics/consumer.py`)**
```python
import os
import asyncio
import json
from aiokafka import AIOKafkaConsumer
from prometheus_client import Gauge

# Local metrics counters (Exposed on /metrics)
orders_count = 0
cancellations_count = 0
durations_sum = 0.0
completed_orders_count = 0

def start_analytics_kafka_consumer():
    async def listen():
        global orders_count, cancellations_count, durations_sum, completed_orders_count
        
        consumer = AIOKafkaConsumer(
            "order_placed", "order_cancelled", "order_delivered",
            bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
            group_id="analytics-harvester-group",
            value_deserializer=lambda m: json.loads(m.decode('utf-8'))
        )
        await consumer.start()
        
        # In-memory tracking cache for calculating rolling average delivery time SLA
        order_creation_timestamps = {}

        try:
            async for msg in consumer:
                event_type = msg.value.get("event_type")
                payload = msg.value.get("payload", {})
                order_id = payload.get("order_id")
                
                if event_type == "order_placed":
                    orders_count += 1
                    # Update Prometheus metrics
                    Gauge("sfo_business_total_orders", "Total checkout orders").set(orders_count)
                    # Cache creation time (Unix timestamp)
                    order_creation_timestamps[order_id] = msg.timestamp / 1000.0 # Convert ms to sec
                    
                elif event_type == "order_cancelled":
                    cancellations_count += 1
                    Gauge("sfo_business_order_cancellations", "Total cancellations").set(cancellations_count)
                    
                elif event_type == "order_delivered":
                    completed_orders_count += 1
                    placed_time = order_creation_timestamps.get(order_id)
                    if placed_time:
                        delivery_time = (msg.timestamp / 1000.0) - placed_time
                        durations_sum += delivery_time
                        avg_time = durations_sum / completed_orders_count
                        Gauge("sfo_business_average_delivery_time_seconds", "Avg delivery time").set(avg_time)
        finally:
            await consumer.stop()

    loop = asyncio.new_event_loop()
    loop.run_until_complete(listen())
```

---

### 📈 Phase E: Validation & Dashboards Setup

#### **E.1 Verifying Trace Spans inside Jaeger**
1. Trigger a full order placement.
2. Open your web browser and navigate to the Jaeger UI: `http://localhost:16686`.
3. Select `order-service` from the Service dropdown and click **Find Traces**.
4. You will see a complete visual breakdown of your HTTP request propagation, the corresponding Kafka publish event, and database latency spans.

#### **E.2 Setting Up Your Grafana Live Dashboard**
1. Access the Grafana Dashboard by navigating to `http://localhost:3000` (Default credentials: user `admin`, password `admin`).
2. Navigate to **Connections -> Data Sources** and select **Add Data Source**. Select **Prometheus**.
3. Set the Prometheus HTTP URL to: `http://prometheus:9090` and click **Save & Test**.
4. Go to **Dashboards -> New Dashboard**, select **Add a new panel**, and enter your business or technical metric queries in the PromQL search block:
   * **Total Orders Metric:** `sfo_business_total_orders`
   * **SLA Speed Check:** `sfo_business_average_delivery_time_seconds`
   * **Database Connection Pool Saturation:** `go_memstats_alloc_bytes` or customized pool metrics.
