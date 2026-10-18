-- Migration (D66): the document tables keep only the chunk, its hash and its embedding. Name, price,
-- availability, tags, address, location and the active flag are no longer copied from the Menu and
-- Restaurant Services; search reads them live.
--
-- For a database created before this change. A fresh database from db/vector/init.sql already has
-- the new shape. Run by hand:
--   docker exec -i sfo-vector-db psql -U sfo_vector_admin -d sfo_vector_core < db/vector/slim_document_tables.sql
--   docker exec sfo-ai-ingestion-worker python -m ai.ingestion --backfill
--
-- The tables are derived state (see init.sql): dropping them loses no fact another service owns,
-- and the backfill rebuilds every row. Until it has run, search returns nothing. The usage log
-- (`assistant_interactions`) is not touched.

DROP TABLE IF EXISTS menu_item_documents;
DROP TABLE IF EXISTS restaurant_documents;

CREATE TABLE menu_item_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id UUID NOT NULL,
    item_key VARCHAR(255) NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_hash CHAR(64) NOT NULL,
    embedding vector(384) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (restaurant_id, item_key)
);

CREATE TABLE restaurant_documents (
    restaurant_id UUID PRIMARY KEY,
    chunk_text TEXT NOT NULL,
    chunk_hash CHAR(64) NOT NULL,
    embedding vector(384) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_menu_item_documents_embedding
    ON menu_item_documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_restaurant_documents_embedding
    ON restaurant_documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_menu_item_documents_restaurant ON menu_item_documents(restaurant_id);
