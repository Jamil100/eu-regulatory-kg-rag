-- pgvector schema for chunk embeddings.
--
-- Applied two ways, and the difference matters: docker-compose mounts this file
-- into /docker-entrypoint-initdb.d, which Postgres runs ONLY when the data
-- volume is empty. Every edit after the container's first start is invisible
-- that way. `pgvector_schema.ensure_schema()` applies it from Python instead and
-- is the path that actually works on an existing database.
--
-- Every statement is IF NOT EXISTS, so applying it repeatedly is a no-op.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per chunk. Column types are read off the corpus, not assumed: the
-- chunker writes article/paragraph/definition/point/token_count as integers,
-- and `annex` as a roman numeral string.
--
-- The three shapes are disjoint and each leaves the other's columns NULL:
--   paragraph  (906 rows) - article, article_title, paragraph
--   annex      (108 rows) - annex, annex_title, section?, point
--   definition ( 94 rows) - article, article_title, definition
--
-- An earlier version of this file declared `article` and `paragraph` only, which
-- would have loaded the 202 annex and definition chunks as anonymous text --
-- Annex III is the high-risk list and Annexes VI/VII the conformity procedures,
-- the chunks a citation most needs to name precisely.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,
    regulation     TEXT NOT NULL,
    shape          TEXT NOT NULL CHECK (shape IN ('paragraph', 'annex', 'definition')),

    article        INTEGER,
    article_title  TEXT,
    paragraph      INTEGER,
    definition     INTEGER,

    annex          TEXT,
    annex_title    TEXT,
    section        TEXT,          -- only Annexes VIII (A/B/C) and XI (1/2)
    point          INTEGER,

    token_count    INTEGER,

    -- The user-facing locator, e.g. 'AIA Art. 9(2)' / 'AIA Annex VIII(A)(1)'.
    -- Derived once by Chunk.citation_label and stored, so the answer path never
    -- re-derives a citation format that could drift from the eval set's golds.
    citation_label TEXT NOT NULL UNIQUE,

    text           TEXT NOT NULL,

    -- Resolved graph node names asserted by this chunk. Same string as the
    -- Neo4j MERGE key, so a value here is usable as a Cypher parameter with no
    -- translation. Empty for chunks that yielded no entities.
    entity_ids     TEXT[] NOT NULL DEFAULT '{}',

    -- Both arms of the ADR-0004 dimension experiment, on the same rows: the
    -- comparison is only meaningful if 1536 and 512 see an identical row set,
    -- and one table makes that structural rather than something to verify.
    -- The losing column gets dropped once the ADR resolves.
    embedding_1536 vector(1536),
    embedding_512  vector(512)
);

CREATE INDEX IF NOT EXISTS chunks_regulation ON chunks (regulation);
CREATE INDEX IF NOT EXISTS chunks_shape ON chunks (shape);
CREATE INDEX IF NOT EXISTS chunks_entity_ids ON chunks USING gin (entity_ids);

-- The enumeration path's access pattern: every paragraph of one article, in
-- order. Added 2026-08-16 with `retrieve_by_article`; before it, `article` and
-- `paragraph` were columns with no index and the lookup was a seq scan.
--
-- At 1,108 rows the scan costs a few milliseconds and this index buys almost
-- nothing measurable -- it is here because the query is a locator lookup by
-- primary structure, which is the one access pattern that should never depend
-- on corpus size, not because a benchmark asked for it. The composite order
-- (regulation, article, paragraph) matches both the WHERE and the ORDER BY, so
-- the sort is free as well.
CREATE INDEX IF NOT EXISTS chunks_article ON chunks (regulation, article, paragraph);
CREATE INDEX IF NOT EXISTS chunks_annex ON chunks (regulation, annex, section, point);
