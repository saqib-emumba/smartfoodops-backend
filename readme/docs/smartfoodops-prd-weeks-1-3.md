# SmartFoodOps — Product Requirements Document (Weeks 1–3)
**Document Version:** 1.0  
**Target System:** Core Platform & Distributed Infrastructure (Part A)  
**Author:** Gemini Notebook / Lead Systems Architect  

---

## 1. Executive Summary & Problem Statement

### 1.1 Executive Summary
**SmartFoodOps** is QuickServe’s next-generation, large-scale food ordering and delivery orchestration platform. Designed to support multi-city operations across restaurants, cloud kitchens, enterprise dining programs, and independent delivery partners, the platform serves as the technical backbone for high-concurrency order placement, real-time logistics, and fault-tolerant financial transactions.

This Product Requirements Document (PRD) establishes the functional, architectural, and non-functional specifications for **Part A (Weeks 1–3)** of the platform build, transitioning SmartFoodOps from a monolithic prototype into a production-grade, event-driven distributed system.

### 1.2 Problem Statement
QuickServe's legacy platform suffered from critical operational bottlenecks during peak lunch and dinner hours:
1. **Unreliable Order Checkouts:** High latency and blocking synchronous HTTP calls caused frequent checkout timeouts and dropped orders.
2. **Data Inconsistency:** Menu availability, pricing, and stock items were out of sync across ordering endpoints and restaurant portals.
3. **Inefficient Dispatching:** Delivery rider allocation relied on manual or non-scalable polling, leading to delayed pickups and poor rider utilization.
4. **Database Resource Starvation:** Single monolithic databases experienced connection pool exhaustion under concurrent read/write traffic spikes.
5. **Lack of Failure Recovery:** Upstream payment or network failures left orders in ambiguous states without automated refunds or state rollbacks.
6. **Zero Operational Visibility:** Distributed service failures could not be traced or debugged due to fragmented, un-correlated logs.

---

## 2. Business Vision & System Roles

### 2.1 Business Goals
* **Sub-Second Customer UX:** Deliver instant menu loading (<1ms via caching) and responsive checkout initialization (<200ms).
* **Absolute Financial & State Consistency:** Guarantee zero double-charging via cryptographic idempotency keys and automated Saga compensations.
* **Scalable Logistics Engine:** Support real-time proximity dispatching for thousands of active delivery riders.
* **High-Throughput Event Architecture:** Decouple non-critical background jobs (notifications, analytics) from core transactional paths using Kafka and Celery.
* **Production-Grade Observability:** Map 100% of multi-service requests via OpenTelemetry distributed tracing and real-time Grafana dashboards.

### 2.2 System Roles & User Personas
* **Customer:** Browses menus, places orders, completes payments, tracks real-time delivery timelines, and receives automated status notifications.
* **Restaurant Admin:** Onboards kitchen metadata, updates dynamic menus and pricing, manages item availability (`is_available`), and accepts/rejects incoming kitchen tickets.
* **Delivery Rider:** Registers active availability, streams physical GPS coordinates, receives automated proximity dispatch requests, and updates order fulfillment stages (`picked_up`, `delivered`).
* **System Admin / Ops Manager:** Monitors global platform health, tracks operational KPIs (e.g., average delivery time, cancellation rates), and inspects distributed tracing logs.

---

## 3. Core Architecture & Architectural Principles

SmartFoodOps enforces a **Database-per-Service (Option B)** zero-shared-state architecture. Each microservice completely owns its private storage engine and communicates exclusively via validated REST APIs, Temporal state workflows, or Kafka event streams.

```
                          ┌───────────────────────┐
     http://localhost:80  │   Nginx API Gateway   │
     ────────────────────▶│  (path-based routing) │
                          └───────────┬───────────┘
   ┌──────────┬──────────┬────────────┴───┬──────────┬──────────┐
   ▼          ▼          ▼                ▼          ▼          ▼
┌────────┐┌────────┐┌────────┐      ┌────────┐┌────────┐┌────────┐
│  User  ││Restaur.││  Menu  │      │ Order  ││Payment ││ Rider  │
│ :8001  ││ :8002  ││ :8003  │      │ :8004  ││ :8005  ││ :8006  │
└───┬────┘└───┬────┘└───┬────┘      └───┬────┘└───┬────┘└───┬────┘
    │         │      ┌──┴──┐            │         │         │
    ▼         ▼      ▼     ▼            ▼         ▼         ▼
 Postgres  Postgres  PG  Redis       Postgres  Postgres  Postgres
  :5432     :5433  :5436 :6379        :5434     :5435     :5437
                  (menus)(cache)   (+ tracking)         (fleet)
                                        │                   │
                                        │ gRPC: start saga, │ gRPC: signal
                                        │ signal kitchen    │ pickup /
                                        │ decision (D47)    │ delivery (D47)
                                        └─────────┬─────────┘
                                                  ▼
                                 ┌────────────────────────────────────┐
                                 │        Temporal dev server         │
                                 │ :7233 gRPC :8233 UI :9233 /metrics │
                                 └───────┬─────────────────┬──────────┘
                                         │ health only     │ polls "order-tasks"
                                         ▼                 ▼
                  ┌───────────────────────┐     ┌─────────────────────────┐
                  │  orchestrator-service │     │   orchestrator-worker   │
                  │   :8007, no database  │     │   runs the saga, no db  │
                  └───────────────────────┘     └────────────┬────────────┘
                                                              │ HTTP, X-Internal-Key:
                                                              │  order transitions/read
                                                              │  payment authorize/refund
                                                              │  rider dispatch/release
```

### 3.1 Guiding Architectural Principles
1. **Strict Storage Isolation:** No service may directly execute SQL joins or read queries against another service's physical database [D01].
2. **Polyglot Persistence:** Match storage technology to workload requirements:
   * **PostgreSQL:** ACID transactional compliance for Users, Restaurants, Orders, Payments, and Menus [39, v4].
   * **Redis:** High-speed in-memory Cache-Aside layer for menu reads [39, 4, v4].
   * **Kafka:** Distributed commit-log for asynchronous event choreography [39, v4].
   * **RabbitMQ + Celery:** Out-of-process task queue for heavy, non-blocking jobs [39, v4].
3. **Orchestration vs. Choreography Split:**
   * **Temporal (Orchestration):** Used for strict, step-by-step distributed state transitions requiring timeouts and Saga compensations (Order Checkout $\rightarrow$ Payment Charge $\rightarrow$ Rider Dispatch) [39, v2].
   * **Kafka (Choreography):** Used for broadcast events (`Order Confirmed`, `Rider Assigned`) where independent downstream consumers react asynchronously [39, v4].

---

## 4. Detailed Functional Requirements & Weekly Milestones

---

### 🟢 WEEK 1 — Foundation & Core Services

#### 4.1 Scope & Operational Goals
Establish the physical containerized infrastructure, modular directory layouts, relational schemas, database-per-service isolation, API gateway routing, and core CRUD domain endpoints [40].

#### 4.2 System Requirements & Features
1. **Database-per-Service Infrastructure:** Spin up isolated PostgreSQL databases mapped to unique external ports [40, v4]:
   * User DB (`sfo_user_core`): Port `5432`
   * Restaurant DB (`sfo_restaurant_core`): Port `5433`
   * Order DB (`sfo_order_core`): Port `5434`
   * Menu DB (`sfo_menu_core`): Port `5436`
2. **Menu Service with PostgreSQL JSONB & Redis Caching:**
   * Model dynamic, deeply nested menu categories, items, and option groups inside a single `JSONB` column on the `menus` table [v4].
   * Implement the **Cache-Aside Pattern** with Redis (`sfo-redis:6379`). Menu reads check Redis first (<1ms); on a cache miss, data is read from Postgres JSONB and populated into Redis with a 1-hour TTL [4, v4].
   * On menu updates, instantly invalidate the Redis key (`menu:{restaurant_id}`) [v4].
3. **API Contracts:**
   * `POST /api/v1/users/register`: Registers customers, restaurant admins, riders, and system admins [40].
   * `POST /api/v1/restaurants/onboard`: Onboards kitchen metadata, operational capacities, and location coordinates [40].
   * `POST /api/v1/menus`: Inserts or updates dynamic JSONB menus and invalidates Redis [40, v4].
   * `POST /api/v1/orders`: Initializes an order record in PostgreSQL [40].

#### 4.3 Week 1 Key Deliverables
* [x] Modular service directory structure with separate container contexts [40].
* [x] Finalized PostgreSQL DDL scripts with GIN indexes on JSONB menu categories [40, v4].
* [x] Fully functional FastAPI endpoints matching standardized Pydantic request/response wrappers [40, v2].
* [x] Docker Compose blueprint orchestrating multi-port PostgreSQL containers and Redis [40, v4].

---

### 🟡 WEEK 2 — Order Lifecycle & Delivery Workflow

#### 4.4 Scope & Operational Goals
Implement an end-to-end distributed order state machine, simulated payment authorizations with idempotency protection, proximity rider allocation, Temporal workflow orchestration, and Saga compensations [41].

#### 4.5 System Requirements & Features
1. **Distributed Order State Machine:** Enforce strict state transitions:
   $$\text{created} \longrightarrow \text{confirmed} \longrightarrow \text{assigned} \longrightarrow \text{picked\_up} \longrightarrow \text{delivered} \quad (\text{or } \text{cancelled})$$
2. **Standalone Orchestration Service (Temporal):**
   * Extract Temporal workflows and workers into a dedicated `/services/orchestration` microservice [v2].
   * Run a stateful `OrderWorkflow` coordinating execution across Payment, Restaurant, and Rider HTTP endpoints [v2].
3. **Payment Service & Cryptographic Idempotency:**
   * Deduplicate charges using a unique `idempotency_key` (SHA-256 hash of `order_id` + `amount`) stored in a dedicated `payments` table (`sfo_payment_core`, Port `5435`) [v2].
4. **Proximity-Based Rider Dispatch Algorithm:**
   * Query available riders (`is_available = TRUE`) and calculate physical distance to the restaurant using the mathematical **Haversine Formula**:
     $$d = 2r \arcsin\left(\sqrt{\sin^2\left(\frac{\Delta \phi}{2}\right) + \cos(\phi_1)\cos(\phi_2)\sin^2\left(\frac{\Delta \lambda}{2}\right)}\right)$$
   * Reserve the closest rider using atomic database row locks (`SELECT ... FOR UPDATE`) to prevent double-allocation race conditions under high concurrency [v2].
5. **Saga Pattern & Compensating Rollbacks:**
   * If Payment fails $\rightarrow$ Mark order `failed`, log reason, notify user [41].
   * If Restaurant rejects or Rider dispatch times out (2-minute SLA) $\rightarrow$ Execute compensating activity to refund/void payment transaction, update order state to `cancelled`, and release held resources [41, v2].
6. **Consolidated Append-Only Order Tracking Logs:**
   * Store order tracking logs inside the Order DB (`sfo_order_core`) using an append-only relational table `order_tracking_logs` [v4].
   * Enforce strict database-level referential integrity: `FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE` [v4].

#### 4.6 Week 2 Key Deliverables
* [x] End-to-end operational Order state machine driven by Temporal Workflows [41].
* [x] Isolated Payment Service with idempotency key enforcement [v2].
* [x] Automated proximity dispatch engine using Haversine calculation and atomic reservation locks [v2].
* [x] Saga rollback handlers for automated payment refunds and order cancellations [41, v2].
* [x] Consolidated append-only tracking log schema in `sfo_order_core` [v4].

---

### 🔴 WEEK 3 — Event-Driven System & Observability

#### 4.7 Scope & Operational Goals
Transition the platform to asynchronous event choreography, offload background processing to Celery/RabbitMQ, harvest operational telemetry, and deploy a full OpenTelemetry/Prometheus/Grafana/Jaeger observability stack [42].

#### 4.8 System Requirements & Features
1. **Kafka Event Streaming & Schema Registry:**
   * Deploy KRaft-mode Kafka (`sfo-kafka:9092`) and Confluent Schema Registry (`sfo-schema-registry:8081`) [42, v2].
   * Publish and consume key lifecycle events: `Order Placed`, `Order Confirmed`, `Payment Authorized`, `Rider Assigned`, `Order Picked Up`, `Order Delivered`, and `Order Cancelled` [42].
2. **Asynchronous Background Task Workers (Celery + RabbitMQ):**
   * Offload slow, non-blocking tasks (SMS/Email notifications, PDF receipt generation) out-of-process using RabbitMQ (`sfo-rabbitmq:5672`) and Celery Workers [42, v2].
3. **Real-Time Operational Analytics Pipeline:**
   * Deploy a background analytics consumer that streams Kafka lifecycle events to compute real-time operational KPIs without hitting transactional databases [42, v2].
4. **Complete Observability & Telemetry Suite:**
   * **Distributed Tracing (OpenTelemetry + Jaeger):** Automatically inject a `correlation_id` / trace context at the Nginx API Gateway. Propagate trace headers across FastAPI, Temporal, and Kafka, visualizing full request lifecycles inside Jaeger (`sfo-jaeger:16686`) [42, v2].
   * **Metrics (Prometheus + Grafana):** Expose `/metrics` endpoints on all microservices. Prometheus (`sfo-prometheus:9090`) scrapes connection pool sizes, HTTP latencies, and error rates, displaying them on Grafana dashboards (`sfo-grafana:3000`) [42, v2].

#### 4.9 Week 3 Key Deliverables
* [x] Asynchronous Kafka messaging spine with Schema Registry contract enforcement [42, v2].
* [x] Out-of-process Celery worker pipeline for background customer notifications [42, v2].
* [x] Real-time event harvester calculating operational analytics KPIs [42, v2].
* [x] Active Prometheus metric scraping and visual Grafana dashboards [42, v2].
* [x] End-to-end distributed tracing across microservice boundaries in Jaeger [42, v2].

---

## 5. Non-Functional Requirements (NFRs)

| Dimension | Target Metric / Constraint | Architectural Implementation |
| :--- | :--- | :--- |
| **Read Latency** | Menu Reads < 1ms | Redis Cache-Aside layer [4, v4] |
| **Write Latency** | Order Creation < 200ms | Async FastAPI endpoints + non-blocking Temporal workflow trigger [v2] |
| **Concurrency** | 10,000+ concurrent menu browsers | Redis caching + PostgreSQL connection pool isolation [4, v4] |
| **Idempotency** | 100% duplicate charge prevention | Cryptographic SHA-256 idempotency key check in Payment DB [v2] |
| **Fault Tolerance** | Zero orphan orders on payment failure | Automated Saga compensation handlers via Temporal [41, v2] |
| **Isolation** | Zero shared database state | Strict Database-per-Service (Option B) [D01] |
| **Observability** | 100% request trace propagation | OpenTelemetry trace context injection + Jaeger [42, v2] |

---

## 6. Key Performance Indicators (KPIs) & Operational Metrics

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        TELEMETRY & OPERATIONAL ANALYTICS METRICS                       │
├──────────────────────────┬──────────────────────────────┬──────────────────────────────┤
│ Metric Name              │ Data Source                  │ Operational Purpose          │
├──────────────────────────┼──────────────────────────────┼──────────────────────────────┤
│ Total Orders             │ Postgres (`orders` table)    │ Commercial volume tracking   │
│ Orders per Restaurant    │ Postgres (`orders` table)    │ Vendor demand distribution   │
│ Peak Hour Order Load     │ Prometheus (`/metrics`)      │ Stress & latency analysis    │
│ Average Delivery Time    │ Kafka Analytics Pipeline     │ SLA fulfillment speed        │
│ Rider Utilization Rate   │ Rider DB / GPS Telemetry     │ Active vs. total fleet ratio │
│ Order Cancellation Rate  │ Order DB / Kafka Events      │ Failure / rejection tracking │
│ Restaurant Acceptance    │ Order DB / Kafka Events      │ Kitchen throughput efficiency│
│ Failed Workflow Issues   │ Temporal / Kafka DLQ         │ Bug & network failure audit  │
└──────────────────────────┴──────────────────────────────┴──────────────────────────────┘
```

---

## 7. Feature & Deliverables Traceability Matrix

| Requirement / Component | Targeted Milestone | Implementation Technology | Validation Criteria |
| :--- | :--- | :--- | :--- |
| **Database Isolation** | Week 1 | PostgreSQL Containers | Separate ports (`5432`–`5436`), isolated credentials [D01, v4] |
| **Menu Storage & Cache** | Week 1 | Postgres JSONB + Redis | Sub-millisecond reads; invalidation on write [4, v4] |
| **State Machine** | Week 2 | Temporal Workflows | Deterministic state transitions with timeouts [v2] |
| **Payment Idempotency** | Week 2 | Payment DB SHA-256 Key | Duplicate POST requests return cached 200 result [v2] |
| **Rider Proximity** | Week 2 | Haversine Formula + SQL Lock | Atomic driver allocation without double-booking [v2] |
| **Saga Compensations** | Week 2 | Temporal Activity Handlers | Payment auto-refunded if order cancelled [41, v2] |
| **Order Tracking Audit** | Week 2 | Append-Only Order DB Table | Foreign key cascaded history rows [v4] |
| **Event Choreography** | Week 3 | Kafka + Schema Registry | Asynchronous lifecycle event broadcasts [42, v2] |
| **Background Jobs** | Week 3 | Celery + RabbitMQ | Non-blocking notification dispatch [42, v2] |
| **Distributed Tracing** | Week 3 | OpenTelemetry + Jaeger | Unified `correlation_id` across all spans [42, v2] |
| **System Metrics** | Week 3 | Prometheus + Grafana | Real-time visualization of latency & connection pools [42, v2] |

---
*End of Product Requirements Document (SmartFoodOps Weeks 1–3)*
