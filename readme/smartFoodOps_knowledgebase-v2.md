# SmartFoodOps — Integrated Platform Knowledgebase & Execution Blueprint

This document serves as the comprehensive repository of engineering guidelines, technical requirements, architectural blueprints, and execution roadmaps for the **SmartFoodOps** intelligent food ordering and delivery orchestration platform. It integrates the structural learning path of the core platform with the advanced Generative AI intelligence layer.

---

## SECTION 1: Strategic Vision & Core Learning Philosophy

### 1.1 Objective of the Learning Plan
The SmartFoodOps curriculum is not merely a feature-delivery checklist, but a structured learning journey designed to cultivate deep systems thinking. The plan emphasizes:
*   **Decoupled Distributed Architecture:** Designing resilient services at consumer-internet scale.
*   **Workflow Orchestration & Event-Driven Patterns:** Mastering asynchronous life-cycles and distributed state-tracking.
*   **High-Volume Reliability:** Developing absolute precision under peak-hour traffic spikes and failure scenarios.
*   **Generative AI Integration:** Learning how to blend unstructured customer-centric operations with deterministic relational systems in production.

The primary metric of success is understanding the **"why"** behind engineering trade-offs and architectural decisions.

### 1.2 The Mentor Review & Collaboration Loop
To maximize educational outcome, the execution of this platform follows a strict, incremental feedback loop:
*   **Module-Based Progression:** Try to complete each module within its assigned week. Concepts must be solidified and reviewed before building subsequent distributed state machines or event workflows.
*   **Weekly Milestone Cycle:** The operational cadence consists of: *Build Milestone → Submit for Mentor Review → Discuss & Trace Trade-offs → Refine Implementation → Advance*.
*   **Review Focus Areas:** Mentor evaluations are designed as dialogues focusing on:
    *   System design clarity and logical domain boundaries.
    *   Workflow correctness, particularly the end-to-end order $\rightarrow$ delivery $\rightarrow$ completion lifecycle.
    *   Mastery of event-driven choreography (Kafka) and background task patterns (Celery).
    *   Data consistency approaches, failure boundaries, and idempotency guarantees.

---

## SECTION 2: System-Wide Tech Stack & Architecture

SmartFoodOps utilizes a decoupled, modern backend and observability stack designed for multi-city, high-concurrency scaling.

### 2.1 Technology Stack Matrix
| Layer | Core Components / Technologies | Architectural Purpose |
| :--- | :--- | :--- |
| **Backend Framework** | Python, FastAPI | High-performance, async web services and API gateways. |
| **Transactional Database** | PostgreSQL | ACID-compliant, relational storage for strict schemas (Users, Payments, Orders). |
| **Flexible Storage** | PostgreSQL `JSONB` | Hierarchical storage for dynamic schemas (Menus). Originally a separate NoSQL engine; dropped in favour of `JSONB` because the document shape was never the reason to run a second engine — see D22. Audit logs moved to a relational table beside the orders they describe (D24). |
| **Caching Layer** | Redis | High-speed cache for menu-reads and user sessions to bypass database hits. |
| **State Orchestration** | Temporal | Fault-tolerant workflow engine for complex multi-service transactions. |
| **Asynchronous Workers** | Celery + RabbitMQ | Offloads heavy non-blocking background jobs (e.g., generating notifications, processing data). |
| **Message Streaming** | Kafka + Schema Registry | High-throughput, event-driven choreography for core system updates. |
| **GenAI & Orchestration** | LangGraph / LangChain | Multi-agent modeling, conversational memory, and retrieval pipelines. |
| **Vector DB** | Supported Vector Database | Indexing, semantic retrieval, and storage of high-dimensional embeddings. |
| **Observability Suite** | Prometheus, Grafana, OpenTelemetry, Jaeger | Distributed tracing, application-level metrics, logs, and dashboard monitoring. |
| **Local Environment** | Docker + Docker Compose | Containerized execution of the entire ecosystem for seamless local development. |

---

## SECTION 3: Part A — Core Platform Execution Plan (3 Weeks)
**Focus:** Core Platform + Distributed Systems. Addresses current bottlenecks like slow checkouts, inconsistent menu inventory, peak-load delays, inefficient rider dispatch, and unreliable status updates.

```
  [Customer/Admin] -> [API Gateway (Nginx)]
                            |
    +----------+------------+------------+-----------+
    |          |            |            |           |
 [User]  [Restaurant]   [Menu]      [Orders]   [Payment] [Rider]
    |          |            |            |           |       |
(Postgres) (Postgres)  (Postgres    (Postgres +  (Postgres)(Postgres)
                        JSONB           Temporal
                        + Redis)        Workflow)
                                             |
                                      +------+------+
                                      |             |
                                   (Kafka)      (Celery)
```

*Every service owns one physical database with its own credentials (D01). Redis serves the
Menu Service as a cache (db 0) and the User Service as a session store (db 1). Kafka and
Celery arrive in Week 3.*

### 3.1 Week 1 — Foundation & Core Services
*   **Operational Goal:** Set up the modular directory layout, relational/non-relational schemas, and establish the API gateway and basic services.
*   **Core Scope:**
    *   Initial high-level system design layout.
    *   Bootstrapping **User Service**, **Restaurant Service**, and **Menu Service**.
    *   Designing the relational (PostgreSQL) and flexible (NoSQL) database schemas.
    *   Configuring a fully containerized local developer workflow utilizing Docker Compose.
*   **API Requirements:**
    *   `POST /api/v1/users/register` - Registers users with roles: *customer, restaurant_admin, rider, system_admin*.
    *   `POST /api/v1/restaurants/onboard` - Establishes restaurant metadata, operational capacity limits, and physical coordinates.
    *   `POST /api/v1/menus` - Inserts or updates dynamic restaurant menu structures (handled in NoSQL).
    *   `POST /api/v1/orders` - Initializes a basic order creation record in PostgreSQL.
*   **Week 1 Deliverables:**
    *   [x] Clean, separated service boundary directory structure.
    *   [x] Finalized and indexed database schemas.
    *   [x] Fully functional CRUD and registration REST APIs.
    *   [x] Operational Docker Compose local setup.
    *   [x] Initial high-level system architecture diagram embedded in README.

### 3.2 Week 2 — Order Lifecycle & Delivery Workflow
*   **Operational Goal:** Implement robust multi-service state tracking, logistics management, and orchestrate complex workflows using Temporal.
*   **Core Scope:**
    *   Implementing an end-to-end distributed order state machine ($created \rightarrow confirmed \rightarrow assigned \rightarrow picked\_up \rightarrow delivered$).
    *   Simulating real payment authorization (integrated with idempotency protection to prevent double-charging).
    *   Modeling restaurant acceptance/rejection flows.
    *   Implementing a rider allocation and delivery assignment system based on proximity and status.
    *   Integrating **Temporal workflows** to handle timing constraints, coordinate state updates, and trigger compensation flows on failure.
*   **Failure Handling & Rollbacks:**
    *   If payment fails $\rightarrow$ Rollback order state and notify user.
    *   If restaurant rejects $\rightarrow$ Refund payment, cancel order.
    *   If rider assignment fails or times out $\rightarrow$ Reassign rider or execute compensation flow.
*   **Week 2 Deliverables:** *(in progress — see [week2-temporal-orchestration-blueprint.md](week2-temporal-orchestration-blueprint.md))*
    *   [ ] End-to-end functional Order workflow.
    *   [ ] Operational delivery assignment (rider dispatch) system.
    *   [ ] Enforced distributed order state machine.
    *   [ ] Integrated Temporal workflow engine.
    *   [ ] Basic failure handling (retries and rolling rollback/refund mechanisms).

### 3.3 Week 3 — Event-Driven System & Observability
*   **Operational Goal:** Transition the platform to use decoupled event streams, scale background tasks, and achieve telemetry-level system visibility.
*   **Core Scope:**
    *   Setting up **Kafka** messaging queues and schema registries.
    *   Decoupling workflows by producing and consuming events such as: *Order Placed, Order Confirmed, Payment Authorized, Rider Assigned, Order Picked Up, Order Delivered, and Order Cancelled*.
    *   Implementing **Celery workers** to handle non-blocking, asynchronous tasks like customer notifications or bulk data dumps.
    *   Building real-time SMS/Email notification dispatchers triggered via event handlers.
    *   Constructing a background analytics event-pipeline to continuously harvest telemetry data.
    *   Enabling **Distributed Tracing** (Jaeger + OpenTelemetry) and **Metrics** (Prometheus + Grafana dashboards).
*   **Week 3 Deliverables:** *(not started)*
    *   [ ] Fully decoupled, asynchronous event-driven core architecture.
    *   [ ] Operational Celery background processing worker pipeline.
    *   [ ] Functional analytics telemetry stream.
    *   [ ] Active Prometheus metric scraping and Grafana dashboard visualization.
    *   [ ] Deep distributed tracing showing end-to-end system flows.

---

## SECTION 4: Part B — GenAI Intelligence Layer (2 Weeks)
**Focus:** Enhancing Customer Experience, Restaurant Productivity, and Operational Insight using Generative AI.

```
  [User Assistant] -> [LangGraph Agent]
                            |
             +--------------+--------------+
             |                             |
      [Semantic Search]             [RAG Retrieval]
             |                             |
         (Vector DB)              (Postgres/NoSQL Logs)
```

### 4.1 Week 4 — Data Preparation & Retrieval
*   **Operational Goal:** Design the data parsing, chunking, and indexing pipeline to power semantic search over structured and unstructured food data.
*   **Core Scope:**
    *   Structuring ingestion pipelines to parse restaurant menus, metadata, and user order histories.
    *   Partitioning text and menu schemas into optimized chunks, generating vector representations using embeddings, and storing them in a **Vector Database**.
    *   Implementing **Semantic Search** engines allowing users to search across food items, specific cuisines, and relative dining attributes without relying on direct string-matching.
    *   Creating a multi-source retrieval pipeline that serves context directly to the LLM-powered recommendation engine.
*   **Week 4 Deliverables:** *(not started)*
    *   [ ] Robust, active menu embedding data pipeline.
    *   [ ] Populated and queryable Vector Database.
    *   [ ] Functional semantic search API over restaurants, cuisines, and menus.
    *   [ ] High-performance retrieval APIs feeding contextual system prompts.

### 4.2 Week 5 — AI Assistant & Streaming Response Engine
*   **Operational Goal:** Deliver a real-time, context-aware, low-latency Conversational Assistant to users.
*   **Core Scope:**
    *   Building the **AI Food Assistant** that resolves user requests like *"What should I eat tonight under $10?"* or *"Recommend spicy dishes near me"*.
    *   Creating the **Order Explanation Engine** which reads database logs and telemetry to dynamically explain delays, ETAs, cancellations, or refund logic to users (e.g., *"Why is my order delayed?"*).
    *   Designing generative **Restaurant Support Tools** to auto-generate menu descriptions, promotional offers, and customer engagement emails.
    *   Implementing **Streaming Response API** protocols (e.g., SSE) to handle conversational outputs smoothly and bypass long blocking latencies.
    *   Using advanced **Prompt Engineering** and Guardrails to mitigate hallucinations and restrict responses strictly to grounded menus.
*   **Week 5 Deliverables:** *(not started)*
    *   [ ] Fully functional conversational AI Food Assistant.
    *   [ ] Context-aware meal recommendation and support engines.
    *   [ ] Implemented streaming outputs for real-time customer UX.
    *   [ ] End-to-end integration combining Core Orchestration with LLM intelligence.

---

## SECTION 5: Operational Analytics & Telemetry Metrics

To maintain a healthy, load-tested system under high concurrency, engineers must build dashboards and metrics engines to track the following values:

### 5.1 Technical & Business Telemetry
| Category | Telemetry Metric | System Source | Operational Importance |
| :--- | :--- | :--- | :--- |
| **Volume & Scale** | Total Orders | PostgreSQL (Orders Table) | Measures overall commercial growth and transactional load. |
| **Throughput** | Orders per Restaurant | PostgreSQL (Orders Table) | Flags popular vendors and hotspots. |
| **Concurrency** | Peak Hour Order Load | Prometheus Telemetry | Analyzes system latency and database connections under stress. |
| **Performance** | Average Delivery Time | Kafka / Analytics Pipeline | Core SLA metric tracking speed from creation to delivery. |
| **Logistics** | Rider Utilization Rate | PostgreSQL / NoSQL Logs | Tracks active riders vs. total registered fleet. |
| **Error / Defect** | Order Cancellation Rate | Orders Database / Events | High rates indicate systematic supply issues or platform errors. |
| **Efficiency** | Restaurant Acceptance Rate | Orders Database / Events | Evaluates kitchen throughput and responsiveness. |
| **Reliability** | Delivery Success Rate | Orders Database / Events | Percentage of orders successfully completed without dispute. |
| **System Health** | Failed Events / Workflow Issues | Temporal / Kafka Dead-Letter Queues | Captures software, network, and communication bugs. |
| **AI Engagement** | AI Assistant Usage | Assistant Analytics Logs | Monitors how many users interact with the assistant. |
| **AI Accuracy** | Questions Asked / Answered | Conversational Analytics | Monitors conversation volume and completion status. |
| **AI Performance** | Average AI Response Time | OpenTelemetry / Jaeger | Measures latency and streaming response performance. |
| **AI UX Metric** | Recommendation Acceptance Rate | Conversion Logs | Tracks if a user orders items suggested by the AI. |
| **AI Conversion** | Order Conversion After AI Interaction| Conversion Logs | Directly evaluates the business ROI of the GenAI integration. |
| **AI Telemetry** | Customer Engagement Metrics | Session / Conversation Logs | Evaluates chat depth, user feedback, and satisfaction. |

---

## SECTION 6: Submission & Deliverables Checklist

Prior to final grading and project wrapping, the following conditions must be met:
- [ ] **End-to-End Workflow Stability:** The core order-to-delivery-to-completion flow must execute without state degradation or manual interference.
- [ ] **Event-Driven Choreography:** Kafka and Celery networks must be completely stable, handling heavy async spikes seamlessly.
- [ ] **Observability Dashboards:** Logging, OpenTelemetry tracing, and Prometheus metrics must be active and accessible.
- [ ] **GenAI Integration:** Retrieval pipelines must feed real-time assistants with zero-hallucination guardrails and operational streaming responses.
- [ ] **Failure Handling & Resiliency:** Demonstrate that the system survives failures (e.g. refunding cancellations, handling duplicate requests via idempotency).
- [ ] **Modular Codebase:** Clean service separation mirroring strict Domain-Driven Design boundaries.
- [ ] **Technical Documentation:** Completed README (detailing local setups and architecture diagrams) and separate design document covering state machines, tradeoffs, and schemas.

---

## SECTION 7: System Grading & Evaluation Criteria

Mentors evaluate candidates based on the following specific dimensions:
*   **Architecture & Domain Modeling:** Cleanliness of directory layouts, dependency directions, and separation of concerns.
*   **Workflow Integrity:** Faultless state routing through the entire delivery and fulfillment process.
*   **Event-Driven Design Quality:** Effective use of Kafka partitions, Celery workers, schema registry validation, and avoiding event loops.
*   **Resiliency & Exception Recovery:** Graceful degradations, Temporal workflow timeouts, retry backoffs, and reliable rollback schemas.
*   **Observability Depth:** Ease of diagnosing failures using correlation IDs across distributed microservices (Jaeger + Prometheus).
*   **Data Consistency & Idempotency:** Prevention of duplicate payments or double-dispatching via cryptographic idempotency keys.
*   **GenAI Synthesis Quality:** Precision of RAG retrieval, lack of hallucination, assistant conversational memory, and streaming capability.
*   **Engineering Humility & Trade-Off Articulation:** Ability of the engineer to explain *why* specific trade-offs were made during live architectural reviews.
