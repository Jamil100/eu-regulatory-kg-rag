-- pgvector schema for chunk embeddings.
-- Auto-loaded by docker-compose on first Postgres init.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    regulation   TEXT NOT NULL,
    article      TEXT,
    paragraph    TEXT,
    text         TEXT NOT NULL,
    entity_ids   TEXT[] DEFAULT '{}',
    embedding    vector(1536)
);

-- HNSW index (m=16, ef_construction=64 to start; tune ef_search at query time).
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
