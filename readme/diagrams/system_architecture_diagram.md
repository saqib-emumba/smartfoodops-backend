# SmartFoodOps — Complete System Architecture

Every container in `docker-compose.yml` as of Week 3, with real host/container ports and
the actual dependency graph — not an idealized version. See
[readme/key-decisions.md](../key-decisions.md) for the D-numbers behind the shape (D01
database-per-service, D25 orchestration over choreography, D36 the orchestrator split,
D38 Kafka carries facts / Temporal owns decisions, D39 the transactional outbox, D47
services hold their own Temporal client).

Nodes are color-coded by subsystem (application services, the Temporal saga,
Kafka/schema-registry eventing, the notification bridge, databases, observability) so a
line's color/region tells you what it belongs to even where paths cross.

```mermaid
%%{init: {"flowchart": {"curve": "linear", "nodeSpacing": 35, "rankSpacing": 55, "htmlLabels": true}}}%%
flowchart TB
    Client["Customer / Owner / Rider client"]
    Gateway["Nginx API Gateway<br/>host 80"]

    subgraph AppServices ["Application Services"]
        UserSvc["User Service<br/>8001"]
        RestaurantSvc["Restaurant Service<br/>8002"]
        MenuSvc["Menu Service<br/>8003"]
        OrderSvc["Order Service<br/>8004"]
        PaymentSvc["Payment Service<br/>8005"]
        RiderSvc["Rider Service<br/>8006"]
        OrchestratorAPI["Orchestrator Service<br/>8007 health only"]
        AnalyticsSvc["Analytics Service<br/>8008"]
    end

    subgraph Saga ["Orchestration (Temporal)"]
        TemporalServer["Temporal Server (dev, embedded SQLite)<br/>7233 / 8233 / 9233"]
        OrchestratorWorker["Orchestrator Worker<br/>metrics 9108"]
    end

    subgraph Eventing ["Eventing"]
        Kafka["Kafka<br/>9092"]
        SchemaRegistry["Schema Registry<br/>8081"]
    end

    subgraph Notify ["Notifications"]
        NotifConsumer["Notification Consumer<br/>metrics 9110"]
        RabbitMQ["RabbitMQ<br/>5672 / 15672 / 15692"]
        NotifWorker["Notification Worker (Celery)"]
    end

    subgraph Data ["Databases and Cache"]
        UserDB["sfo_user_core<br/>5432"]
        RestaurantDB["sfo_restaurant_core<br/>5433"]
        OrderDB["sfo_order_core<br/>5434"]
        PaymentDB["sfo_payment_core<br/>5435"]
        MenuDB["sfo_menu_core<br/>5436"]
        RiderDB["sfo_rider_core<br/>5437"]
        AnalyticsDB["sfo_analytics_core<br/>5438"]
        Redis["Redis<br/>6379"]
    end

    subgraph Observability ["Observability"]
        Jaeger["Jaeger<br/>16686"]
        Prometheus["Prometheus<br/>9090"]
        Grafana["Grafana<br/>3000"]
    end

    %% -- request entry --
    Client --> Gateway
    Gateway --> UserSvc & RestaurantSvc & MenuSvc & OrderSvc & PaymentSvc & RiderSvc
    Gateway -->|health only| OrchestratorAPI

    %% -- synchronous service-to-service calls --
    OrderSvc -->|verify customer| UserSvc
    OrderSvc -->|verify restaurant| RestaurantSvc
    OrderSvc -->|fetch menu| MenuSvc
    PaymentSvc -->|verify order total| OrderSvc
    RiderSvc -->|record pickup / delivery stage| OrderSvc

    %% -- saga: services hold their own Temporal client (D47) --
    OrderSvc -->|start saga, signal kitchen decision| TemporalServer
    RiderSvc -->|signal pickup / delivery| TemporalServer
    OrchestratorAPI -.->|health probe only| TemporalServer
    TemporalServer -->|dispatch workflow task| OrchestratorWorker
    OrchestratorWorker -->|authorize / refund| PaymentSvc
    OrchestratorWorker -->|transitions, internal reads| OrderSvc
    OrchestratorWorker -->|dispatch / release| RiderSvc

    %% -- each service owns one database --
    UserSvc --> UserDB
    RestaurantSvc --> RestaurantDB
    MenuSvc --> MenuDB
    OrderSvc --> OrderDB
    PaymentSvc --> PaymentDB
    RiderSvc --> RiderDB
    AnalyticsSvc --> AnalyticsDB
    MenuSvc -.->|menu cache, db 0| Redis
    UserSvc -.->|refresh tokens, db 1| Redis
    RiderSvc -.->|location GEO index, db 2 (D49)| Redis

    %% -- eventing: outbox relay, consumers, schema registry --
    OrderSvc -->|outbox relay| Kafka
    PaymentSvc -->|outbox relay| Kafka
    Kafka -->|consume| AnalyticsSvc
    Kafka -->|consume| NotifConsumer
    OrderSvc -.->|schema| SchemaRegistry
    PaymentSvc -.->|schema| SchemaRegistry
    AnalyticsSvc -.->|schema| SchemaRegistry
    NotifConsumer -.->|schema| SchemaRegistry

    %% -- notifications: Kafka to Celery bridge --
    NotifConsumer -->|enqueue task| RabbitMQ
    RabbitMQ --> NotifWorker
    NotifWorker -->|resolve contact| UserSvc

    %% -- observability: every service traces + exposes /metrics (target node implies which) --
    AppServices -.-> Jaeger
    OrchestratorWorker -.-> Jaeger
    NotifConsumer -.-> Jaeger
    AppServices -.-> Prometheus
    OrchestratorWorker -.-> Prometheus
    NotifConsumer -.-> Prometheus
    TemporalServer -.-> Prometheus
    RabbitMQ -.-> Prometheus
    Grafana -->|query| Prometheus

    classDef app fill:#dbeafe,stroke:#2563eb,color:#1e3a8a;
    classDef saga fill:#ede9fe,stroke:#7c3aed,color:#4c1d95;
    classDef evt fill:#ffedd5,stroke:#ea580c,color:#7c2d12;
    classDef notify fill:#ccfbf1,stroke:#0d9488,color:#134e4a;
    classDef data fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef obs fill:#f3f4f6,stroke:#6b7280,color:#1f2937;
    classDef edge fill:#fff,stroke:#111827,color:#111827;

    class UserSvc,RestaurantSvc,MenuSvc,OrderSvc,PaymentSvc,RiderSvc,OrchestratorAPI,AnalyticsSvc app;
    class TemporalServer,OrchestratorWorker saga;
    class Kafka,SchemaRegistry evt;
    class NotifConsumer,RabbitMQ,NotifWorker notify;
    class UserDB,RestaurantDB,OrderDB,PaymentDB,MenuDB,RiderDB,AnalyticsDB,Redis data;
    class Jaeger,Prometheus,Grafana obs;
    class Client,Gateway edge;

    linkStyle default stroke:#111827,stroke-width:1.2px;

    style AppServices fill:#eff6ff,stroke:#2563eb,stroke-width:1px;
    style Saga fill:#f5f3ff,stroke:#7c3aed,stroke-width:1px;
    style Eventing fill:#fff7ed,stroke:#ea580c,stroke-width:1px;
    style Notify fill:#f0fdfa,stroke:#0d9488,stroke-width:1px;
    style Data fill:#f0fdf4,stroke:#16a34a,stroke-width:1px;
    style Observability fill:#f9fafb,stroke:#6b7280,stroke-width:1px;
```
