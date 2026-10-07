# SmartFoodOps — Requirements Specification [Part B: Weeks 4 & 5]
**Intelligent Food Ordering & Delivery Orchestration Platform**
*Document Version:* 2.0  
*Date:* October 7, 2026  
*Scope:* Part B — GenAI & Intelligent Food Experience Layer (Weeks 4 & 5)

---

## 📋 1. Executive Summary & Part B Vision

QuickServe is evolving the **SmartFoodOps** platform into an intelligent food ordering and delivery ecosystem powered by Generative AI. While Part A (Weeks 1–3) established a resilient, high-concurrency event-driven microservices backend (with PostgreSQL database isolation, Temporal workflow orchestration, Kafka event choreography, and OpenTelemetry observability), Part B introduces semantic intelligence, Retrieval-Augmented Generation (RAG), conversational AI, and streaming user experiences.

### Primary Problem Statement (Part B)
- **Static Food Discovery:** Customers struggle to decide what to eat; traditional keyword search fails on vague or intent-based queries (e.g., *"something light for a warm afternoon"*).
- **Manual Restaurant Content Management:** Restaurant partners manually write dish descriptions, promotional campaigns, and customer feedback summaries.
- **Opaque Delivery Issues:** Delivery delays, driver reassignments, and kitchen bottlenecks are not explainable to customers in human-readable terms.
- **Data Latency & LLM Blocking:** Standard HTTP request-response cycles block core ordering APIs when waiting for long-running LLM inferences.

### Business Goals & Vision
1. **Intelligent Customer Discovery:** Enable natural language Q&A, dietary filtering, budget-constrained choices, and personalized meal recommendations grounded strictly in active menus.
2. **Automated Operational Explanations:** Provide real-time, context-aware reasoning for order delays, ETAs, and cancellations by combining transactional database state with AI generation.
3. **Restaurant Partner Productivity:** Automate menu description crafting, promotional offer generation, and feedback summarization for restaurant admins.
4. **Zero-Hallucination Guardrails & Real-Time Streaming:** Enforce strict RAG context retrieval over operational databases, delivering token-by-token streaming responses via Server-Sent Events (SSE).

---

## 🟣 2. Week 4 Requirements: Data Preparation & Retrieval Layer

### 2.1 Operational Goal
Build the data parsing, chunking, embedding, vector storage, and multi-source retrieval pipeline that connects structured operational databases (PostgreSQL and Redis) with unstructured semantic search engines.

---

### 2.2 Functional Requirements & Engineering Scope

#### A. Menu, Restaurant & Operational Data Ingestion & Chunking
- **Multi-Source Data Parsing:** Ingest dynamic restaurant profiles (`RESTAURANTS`), normalized 4-tier menu hierarchies (`MENUS` $\rightarrow$ `MENU_CATEGORIES` $\rightarrow$ `MENU_ITEMS` $\rightarrow$ `MENU_ITEM_CUSTOMIZATION_GROUPS` $\rightarrow$ `MENU_ITEM_CUSTOMIZATION_OPTIONS` per Decision **D50**), and customer order history projections (`ORDER_PROJECTIONS` per Decision **D44**).
- **Semantic Text Chunking:** Construct rich natural-language document blocks from normalized relational tables.
  - *Example Chunk Format:* `"Dish: Spicy Dragon Roll at Zen Sushi ($14.99) | Category: Sushi Rolls | Description: Fresh tuna, avocado, jalapeño, and chili oil. | Dietary: Gluten-Free, Spicy | Availability: In Stock."`
- **Metadata Enrichment:** Attach structured filter keys directly to each text vector:
  - `restaurant_id` (UUID)
  - `category_id` (UUID)
  - `base_price` (Decimal/Float)
  - `is_available` (Boolean)
  - `dietary_tags` (List of Strings)

#### B. Automated Embedding Pipeline
- **Embedding Model Integration:** Generate dense, high-dimensional vector embeddings using standard embedding models (e.g., OpenAI `text-embedding-3-small` 1536-dim or HuggingFace `all-MiniLM-L6-v2` 384-dim).
- **Asynchronous Pipeline Worker:** Build a background worker (`services/ai/ingestion.py`) that executes non-blocking ingestion whenever a menu publish or item update occurs in `sfo_menu_core`.

#### C. Vector Database Storage & Indexing
- **Vector Database Engine:** Containerize and configure a vector database (PGVector extension on PostgreSQL `db-vector-postgres` or Qdrant/ChromaDB).
- **High-Performance Vector Indexing:** Enforce **HNSW (Hierarchical Navigable Small World)** vector indexing (`vector_cosine_ops`) for Approximate Nearest Neighbor (ANN) cosine similarity matching under $O(\log N)$ latency.

#### D. Semantic Search Engine API
- **Intent & Meaning-Based Discovery:** Replace exact string-matching with vector similarity search over:
  - Food items & customization options
  - Restaurants & cloud kitchen attributes
  - Cuisine categories & dietary traits
  - Past customer order preferences
- **Hybrid Vector Filtering:** Support combined queries combining vector cosine similarity with SQL metadata constraints (e.g., `base_price <= 15.00 AND is_available = true`).

#### E. Multi-Engine RAG Context Retrieval Engine
- **Multi-Source Context Assembly:** Construct a context aggregator (`services/ai/rag_context.py`) that fetches and joins context from three decoupled storage engines:
  1. **Vector Database:** Top-$K$ semantically relevant menu items and cuisines.
  2. **Analytics Projections (`sfo_analytics_core`):** Customer order history and favorite vendors (Decision **D44**).
  3. **Redis Spatial Index (`riders:geo`):** Real-time courier proximity and driver availability (Decision **D49**).

---

### 2.3 Architectural Constraints & Standards Alignment

1. **Universal Envelope Standard (Decision D35):** All API responses returned by `/api/v1/ai/*` endpoints must conform to:
   ```json
   {
     "status": 200,
     "body": { ... },
     "message": "Semantic search executed successfully",
     "errors": null
   }
   ```
2. **Database-Backed Multi-Role RBAC (Decision D57):** Routes are secured at the API Gateway via `ROUTE_PERMISSIONS`, `ROLE_PERMISSIONS`, and `PERMISSIONS` tables in `sfo_user_core`.
3. **Database Isolation (Decision D01):** Vector data is isolated in `sfo_vector_core` / PGVector container; cross-service operational reads execute over HTTP or read-only analytical replicas.

---

### 2.4 Week 4 API Specifications

#### `POST /api/v1/ai/search`
- **Description:** Performs hybrid semantic vector search over food items, cuisines, and restaurants.
- **Request Body:**
  ```json
  {
    "query": "spicy keto dishes under $15",
    "top_k": 5,
    "max_price": 15.00,
    "dietary_filters": ["keto", "spicy"]
  }
  ```
- **Success Response (Envelope):**
  ```json
  {
    "status": 200,
    "body": {
      "matches": [
        {
          "item_id": "8f3b2a11-5a21-4d92-9f12-00123456789a",
          "item_name": "Spicy Dragon Roll",
          "restaurant_name": "Zen Sushi",
          "price": 14.99,
          "similarity_score": 0.892
        }
      ]
    },
    "message": "Retrieved 1 matching item",
    "errors": null
  }
  ```

#### `POST /api/v1/ai/rag-context`
- **Description:** Assembles multi-engine RAG context payload for downstream LLMs.
- **Request Body:**
  ```json
  {
    "customer_id": "a1122334-b556-6778-8990-112233445566",
    "prompt_query": "Suggest lunch options available near me"
  }
  ```

---

### 2.5 Week 4 Deliverables Checklist
- [ ] **Vector Engine Infrastructure:** Containerized PGVector / Qdrant instance active in `docker-compose.yml`.
- [ ] **Ingestion Pipeline:** Operational worker converting normalized menu rows into HNSW-indexed vector embeddings.
- [ ] **Semantic Search API:** `/api/v1/ai/search` endpoint returning intent-matched dishes and restaurants.
- [ ] **RAG Context Assembler:** Multi-engine retrieval API combining vector matches, `ORDER_PROJECTIONS`, and Redis `riders:geo`.

---

## 🔵 3. Week 5 Requirements: AI Assistant & Streaming Engine

### 3.1 Operational Goal
Deliver a real-time, context-aware, low-latency Conversational Food Assistant and Order Explanation Engine powered by LangGraph/LangChain multi-agent workflows, zero-hallucination prompt guardrails, and Server-Sent Events (SSE) streaming.

---

### 3.2 Functional Requirements & Engineering Scope

#### A. Conversational AI Food Assistant
- **Contextual Food Q&A:** Handle complex, multi-turn user queries such as:
  - *"What should I eat tonight under $10?"*
  - *"Recommend spicy healthy food near me."*
  - *"What are the popular combo items from Zen Sushi?"*
- **Personalized Recommendation Engine:** Generate personalized suggestions based on:
  - User's historical order preferences (`ORDER_PROJECTIONS`)
  - Active restaurant availability (`RESTAURANTS.is_active = true`)
  - Live rider availability in Redis (`riders:geo`)
  - Strict budget, dietary, and distance constraints

#### B. Delivery & Order Explanation Engine
- **Automated Delay & ETA Reasoning:** Analyze database logs (`order_tracking_logs`), Temporal workflow states, and rider location pings to answer:
  - *"Why is my order delayed?"*
  - *"Explain the current delivery status of Order #102."*
- **Transparent Failure Explanations:** Provide human-readable explanations for kitchen rejections, driver reassignment timeouts, or refund compensations without exposing raw technical error stack traces.

#### C. Automated Restaurant Partner Productivity Tools
- **Generative Menu & Promo Authoring:** Provide tools for restaurant admins (`restaurant_admin` role) to:
  - Auto-generate dish descriptions from raw ingredients.
  - Create promotional marketing copy and special offers.
  - Summarize customer review feedback trends.

#### D. Real-Time Streaming Response Engine (SSE)
- **Non-Blocking Streaming (Server-Sent Events):** Implement streaming endpoints (`GET /api/v1/ai/assistant/stream`) using FastAPI `EventSourceResponse`.
- **Low-Latency UX:** Stream token-by-token LLM outputs to the client in real time, preventing HTTP connection timeouts and eliminating UI lag.

#### E. Multi-Agent Framework, Prompt Engineering & Guardrails
- **Multi-Agent Architecture (LangChain / LangGraph):**
  - **Router Agent:** Classifies user intent (Food Discovery vs. Order Tracking vs. Restaurant Support).
  - **Retrieval Agent:** Fetches grounded RAG context from Week 4 retrieval engine.
  - **Generation Agent:** Drafts polite, formatted responses strictly constrained to retrieved context.
- **Zero-Hallucination System Prompts:** Enforce system prompt boundaries preventing the AI from inventing non-existent dishes, false prices, or out-of-stock items.

---

### 3.3 Week 5 API Specifications

#### `GET /api/v1/ai/assistant/stream?q=Recommend+spicy+sushi+under+$15`
- **Description:** Server-Sent Events (SSE) streaming endpoint for conversational AI responses.
- **Headers:** `Accept: text/event-stream`
- **Stream Response Chunk Format:**
  ```text
  data: {"token": "Based ", "done": false}
  data: {"token": "on ", "done": false}
  data: {"token": "your ", "done": false}
  data: {"token": "preference, ", "done": false}
  data: {"token": "I recommend ", "done": false}
  data: {"token": "the Spicy Dragon Roll ($14.99).", "done": true}
  ```

#### `POST /api/v1/ai/explain-order`
- **Description:** Explains order status, delays, or refund compensations using tracking logs.
- **Request Body:**
  ```json
  {
    "order_id": "c9a1b2c3-d4e5-6f7a-8b9c-0123456789ab"
  }
  ```
- **Success Response (Envelope):**
  ```json
  {
    "status": 200,
    "body": {
      "order_id": "c9a1b2c3-d4e5-6f7a-8b9c-0123456789ab",
      "explanation": "Your order was delayed by 6 minutes due to high dinner traffic at Zen Sushi. A courier has been assigned and is currently 1.2 km away."
    },
    "message": "Order explanation generated successfully",
    "errors": null
  }
  ```

---

### 3.4 Week 5 Deliverables Checklist
- [ ] **AI Food Assistant:** Fully functional conversational agent handling dietary, price, and cuisine queries.
- [ ] **Order Explanation Engine:** Contextual reasoning engine analyzing `order_tracking_logs` and Temporal workflow states.
- [ ] **Streaming Engine:** Operational SSE endpoint streaming tokens in real time.
- [ ] **Prompt Guardrails & Multi-Agent Framework:** Zero-hallucination agent system built with LangGraph/LangChain.

---

## 📊 4. Telemetry, Analytics & Observability (Weeks 4 & 5)

To maintain platform observability and satisfy evaluation criteria, Part B introduces specific GenAI telemetry metrics tracked via OpenTelemetry, Jaeger, Prometheus, and Grafana:

| Category | Telemetry Metric | System Source | Operational Importance |
| :--- | :--- | :--- | :--- |
| **AI Engagement** | AI Assistant Usage | Assistant Analytics Logs | Measures total user interactions with the AI layer. |
| **AI Accuracy** | Questions Asked / Answered | Conversational Logs | Monitors query resolution and completion rate. |
| **AI Latency** | Average AI Response Time | OpenTelemetry / Jaeger | Traces LLM inference and RAG retrieval latency. |
| **AI Quality** | Recommendation Acceptance Rate | Conversion Pipeline | Measures if users add recommended items to their carts. |
| **Commercial ROI** | Order Conversion After AI Interaction | Analytics DB (`sfo_analytics_core`) | Evaluates revenue generated directly via AI interactions. |
| **System Health** | RAG Retrieval & LLM Failures | Prometheus / Dead-Letter Log | Tracks vector store timeouts and provider rate-limit errors. |

---

## 🔗 5. Feature Traceability & Acceptance Matrix (Weeks 4 & 5)

| Milestone | Feature | Component / Service | Core Dependency | Acceptance Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **Week 4** | Relational Menu Ingestion | `services/ai/ingestion.py` | `sfo_menu_core` (D50) | Ingests normalized categories, items, and option groups into text chunks. |
| **Week 4** | HNSW Vector Indexing | `db-vector-postgres` | PGVector | Stores 1536-dim embeddings with HNSW cosine similarity index. |
| **Week 4** | Hybrid Semantic Search | `POST /api/v1/ai/search` | FastAPI + D35 Envelope | Returns top-$K$ dishes matching semantic meaning and SQL price filters. |
| **Week 4** | Multi-Engine RAG | `services/ai/rag_context.py` | Analytics (D44) + Redis GEO (D49) | Joins vector top-$K$, past order history, and driver proximity. |
| **Week 5** | Conversational Assistant | `GET /api/v1/ai/assistant/stream` | LangGraph / LangChain | Streams token-by-token responses answering user meal queries. |
| **Week 5** | Order Delay Reasoning | `POST /api/v1/ai/explain-order` | `order_tracking_logs` | Provides human-readable delay explanations grounded in tracking logs. |
| **Week 5** | Zero-Hallucination Guardrails | AI Assistant Prompts | System Prompts | Restricts suggestions strictly to retrieved active menus and restaurants. |
