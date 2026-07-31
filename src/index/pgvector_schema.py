"""Programmatic access to the pgvector schema (see schema.sql).

Connect + ensure-schema helpers so the index can be created outside the docker
init hook. That hook fires only when the Postgres data volume is empty, so on
any container that has already started once, editing `schema.sql` does nothing
at all -- this module is the path that actually applies it. It is also what the
Azure Postgres stretch goal would use, where there is no init hook to begin with.

Index creation is deliberately NOT part of `ensure_schema()`. Building HNSW over
an empty table and then maintaining it through 1,108 inserts is slower and gives
a worse graph than building it once over the loaded data, so the embedder calls
`create_indexes()` after the bulk load.

Usage:
    python -m src.index.pgvector_schema            # report what exists
    python -m src.index.pgvector_schema --apply    # create table
    python -m src.index.pgvector_schema --indexes  # build the HNSW indexes
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from src.config import settings

if TYPE_CHECKING:
    from psycopg import Connection

SCHEMA_SQL = Path(__file__).parent / "schema.sql"

# m=16, ef_construction=64 to start; ef_search is the query-time knob and is
# swept by the recall harness rather than fixed here.
HNSW = {
    "chunks_embedding_1536_hnsw": "embedding_1536",
    "chunks_embedding_512_hnsw": "embedding_512",
}


def connect() -> Connection:
    """A live psycopg connection with the vector type registered, or raise."""
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(settings.postgres_dsn, autocommit=True)
    # The extension has to exist before the type can be registered, so a first
    # connection against a virgin database applies that one statement itself.
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def ensure_schema(conn: Connection) -> None:
    """Apply schema.sql (idempotent). Does not build the HNSW indexes."""
    conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))


def create_indexes(conn: Connection) -> list[str]:
    """Build the HNSW indexes. Call after the bulk load, not before."""
    built = []
    for name, column in HNSW.items():
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON chunks "
            f"USING hnsw ({column} vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64)"
        )
        built.append(name)
    return built


def drop_indexes(conn: Connection) -> None:
    """Drop the HNSW indexes so a reload does not maintain them per row."""
    for name in HNSW:
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def status(conn: Connection) -> dict:
    """What is actually in the database -- the input to every claim about it."""
    table_exists = conn.execute(
        "SELECT to_regclass('public.chunks') IS NOT NULL"
    ).fetchone()[0]
    if not table_exists:
        return {"table": False}

    rows = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    by_shape = dict(
        conn.execute("SELECT shape, count(*) FROM chunks GROUP BY shape ORDER BY shape").fetchall()
    )
    embedded = {
        column: conn.execute(
            f"SELECT count(*) FROM chunks WHERE {column} IS NOT NULL"
        ).fetchone()[0]
        for column in HNSW.values()
    }
    indexes = [
        r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'chunks' ORDER BY indexname"
        ).fetchall()
    ]
    with_entities = conn.execute(
        "SELECT count(*) FROM chunks WHERE cardinality(entity_ids) > 0"
    ).fetchone()[0]

    return {
        "table": True,
        "rows": rows,
        "by_shape": by_shape,
        "embedded": embedded,
        "with_entities": with_entities,
        "indexes": indexes,
        "pgvector": conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()[0],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Create or inspect the pgvector schema.")
    ap.add_argument("--apply", action="store_true", help="apply schema.sql")
    ap.add_argument("--indexes", action="store_true", help="build the HNSW indexes")
    ap.add_argument("--drop-indexes", action="store_true", help="drop the HNSW indexes")
    args = ap.parse_args()

    conn = connect()
    if args.apply:
        ensure_schema(conn)
        print(f"applied {SCHEMA_SQL.name}")
    if args.drop_indexes:
        drop_indexes(conn)
        print("dropped HNSW indexes")
    if args.indexes:
        print("building HNSW indexes (this reads the whole table)...")
        for name in create_indexes(conn):
            print(f"  {name}")

    for key, value in status(conn).items():
        print(f"{key:15} {value}")
    conn.close()


if __name__ == "__main__":
    main()
