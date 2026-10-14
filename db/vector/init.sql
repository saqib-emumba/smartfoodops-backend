-- ============================================================================
-- AI Service database — sfo_vector_core (container sfo-vector-db, host port 5439)
--
-- The retrieval layer for the GenAI features (Week 4, D59). Holds vector embeddings of the
-- Menu and Restaurant Services' data, and nothing else of anyone's: like the Analytics
-- database, everything here is *derived* state, rebuildable by re-ingesting the menus
-- (`python -m ai.ingestion --backfill`), so dropping this database loses no fact that any
-- other service owns. That is also why it is a separate physical database (D01) rather than
-- a schema inside sfo_menu_core — the menu's write path never depends on a vector index.
--
-- Requires the pgvector extension, which is why docker-compose.yml runs this container from
-- pgvector/pgvector:pg15 instead of the plain postgres:15-alpine every other database uses.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- Ingestion-side dedup, the same shape as sfo_analytics_core.processed_events: the topic is
-- at-least-once, so "have I already applied this menu.published" has to be a real check.
CREATE TABLE IF NOT EXISTS processed_events (
    consumer_group VARCHAR(64) NOT NULL,
    event_id UUID NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (consumer_group, event_id)
);

-- One row per menu item: the natural-language chunk, its embedding, and the structured
-- metadata a search filters on. `vector(384)` is all-MiniLM-L6-v2's output width; switching
-- to a model of another width (OpenAI's text-embedding-3-small is 1536) means changing this
-- column and re-ingesting, which is deliberate rather than automatic.
--
-- `item_key` / `category_key` are the client-supplied keys the Menu Service stores and the
-- order API accepts (`item_id` on the wire) — the internal UUIDs it generates are rewritten
-- on every menu publish (a publish is a full replace), so they are useless as a stable id.
-- `source_version` is `menus.updated_at` of the menu this row was built from, which is how
-- ingestion skips a menu it has already embedded.
CREATE TABLE IF NOT EXISTS menu_item_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id UUID NOT NULL,
    item_key VARCHAR(255) NOT NULL,
    category_key VARCHAR(255) NOT NULL,
    category_name VARCHAR(255) NOT NULL,
    item_name VARCHAR(255) NOT NULL,
    restaurant_name VARCHAR(255) NOT NULL,
    base_price NUMERIC(10, 2) NOT NULL,
    is_available BOOLEAN NOT NULL,
    dietary_tags TEXT[] NOT NULL DEFAULT '{}',
    chunk_text TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL,
    source_version TIMESTAMPTZ NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (restaurant_id, item_key)
);

-- One row per restaurant, so a query like "a quiet Italian place" can match a restaurant
-- directly instead of only through its dishes. `is_active` rides along so a deactivated
-- restaurant stops appearing without waiting for its menu to be re-published.
CREATE TABLE IF NOT EXISTS restaurant_documents (
    restaurant_id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    address TEXT,
    latitude NUMERIC(9, 6),
    longitude NUMERIC(9, 6),
    is_active BOOLEAN NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL,
    source_version TIMESTAMPTZ NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Approximate nearest neighbour search by cosine distance (`<=>`). HNSW rather than IVFFlat:
-- it needs no training step on existing data, so it works on an empty table and stays
-- accurate as rows arrive. m / ef_construction are pgvector's defaults, written out so the
-- trade-off (build time and size against recall) is visible and tunable in one place.
CREATE INDEX IF NOT EXISTS idx_menu_item_documents_embedding
    ON menu_item_documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_restaurant_documents_embedding
    ON restaurant_documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- The structured half of a hybrid query: price and availability constraints, tag containment
-- (`dietary_tags @> ARRAY[...]`) and per-restaurant replacement during ingestion.
CREATE INDEX IF NOT EXISTS idx_menu_item_documents_restaurant ON menu_item_documents(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_menu_item_documents_filter ON menu_item_documents(is_available, base_price);
CREATE INDEX IF NOT EXISTS idx_menu_item_documents_tags ON menu_item_documents USING gin (dietary_tags);
