"""Embed v4 embedding of the chunk corpus into pgvector.

`input_type="search_document"` for the corpus, `"search_query"` for questions --
Embed v4 is asymmetric and using the wrong one silently costs recall rather than
raising.

Both arms of the ADR-0004 dimension experiment load into the same table:
`output_dimension` is passed to the API, so 512 is Cohere's own Matryoshka
truncation-and-renormalisation rather than a client-side slice. The corpus is
~80k tokens, about a cent per arm, which is why there is no response cache here
-- an idempotent upsert plus `--only-missing` is cheaper than cache machinery and
has one less thing to go stale.

`entity_ids` is populated from the same resolver the graph loader uses, so a
value in that column is the exact string Neo4j MERGEd on.

Usage:
    python -m src.index.embedder                       # report, no writes
    python -m src.index.embedder --apply               # metadata + both arms
    python -m src.index.embedder --apply --dim 512     # one arm
    python -m src.index.embedder --apply --only-missing
"""

from __future__ import annotations

import argparse
import json
import time
from typing import TYPE_CHECKING

import cohere
import cohere.errors
import httpx
from cohere.core import ApiError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config import settings
from src.index.pgvector_schema import connect, create_indexes, ensure_schema, status
from src.ingest.extract import CHUNK_FILES
from src.schemas import Chunk

if TYPE_CHECKING:
    from psycopg import Connection

# Embed v4 rejects more than 96 texts per call -- the same ceiling
# entity_resolution.py already hit.
BATCH = 96

DIMENSIONS = {1536: "embedding_1536", 512: "embedding_512"}

# Cohere list price for Embed v4, USD per input token. Stated here so the cost
# line in docs/metrics/vector-index.md is auditable.
PRICE_INPUT_PER_TOKEN = 0.12 / 1_000_000

# Same retryable set as the extraction run, which was interrupted often enough to
# have learned which errors are worth waiting out.
RETRYABLE_ERRORS = (
    cohere.errors.TooManyRequestsError,
    cohere.errors.ServiceUnavailableError,
    cohere.errors.InternalServerError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
)


def load_corpus() -> list[Chunk]:
    """Every chunk, validated. Reuses CHUNK_FILES rather than re-globbing."""
    chunks = []
    for path in CHUNK_FILES:
        with path.open(encoding="utf-8") as fh:
            chunks.extend(
                Chunk.model_validate(json.loads(line)) for line in fh if line.strip()
            )
    return chunks


def entity_ids_by_chunk() -> dict[str, list[str]]:
    """chunk_id -> the resolved graph node names that chunk asserts.

    Inverts `resolve_corpus()["nodes"][*]["chunk_ids"]`. The resolver is imported
    rather than `resolved-entities.json` read, for the reason Step 4 recorded:
    that file holds nodes only, and joining by raw name matches ~48% of endpoints
    against 98.4% through the resolver's key map.
    """
    from src.ingest.entity_resolution import resolve_corpus

    out: dict[str, list[str]] = {}
    for node in resolve_corpus()["nodes"].values():
        for chunk_id in node["chunk_ids"]:
            out.setdefault(chunk_id, []).append(node["canonical_name"])
    return {chunk_id: sorted(names) for chunk_id, names in out.items()}


def get_client() -> cohere.ClientV2:
    if not settings.cohere_api_key:
        raise SystemExit("COHERE_API_KEY is not set")
    return cohere.ClientV2(api_key=settings.cohere_api_key)


@retry(
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    wait=wait_exponential_jitter(initial=2, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)
def _embed_call(
    client: cohere.ClientV2, texts: list[str], dim: int, input_type: str
) -> tuple[list[list[float]], int]:
    response = client.embed(
        model=settings.model_embed,
        texts=texts,
        input_type=input_type,
        output_dimension=dim,
        embedding_types=["float"],
    )
    billed = response.meta.billed_units if response.meta else None
    tokens = int(billed.input_tokens or 0) if billed else 0
    return response.embeddings.float_, tokens


def embed_texts(
    client: cohere.ClientV2,
    texts: list[str],
    dim: int = 1536,
    input_type: str = "search_document",
    progress: bool = False,
) -> tuple[list[list[float]], int]:
    """Embed in batches of 96. Returns the vectors and the billed input tokens."""
    vectors: list[list[float]] = []
    tokens = 0
    for i in range(0, len(texts), BATCH):
        batch = texts[i : i + BATCH]
        try:
            got, billed = _embed_call(client, batch, dim, input_type)
        except ApiError as exc:
            raise SystemExit(
                f"embed failed at batch starting {i} ({len(batch)} texts, dim={dim}): {exc}"
            ) from exc
        vectors.extend(got)
        tokens += billed
        if progress:
            print(f"    {min(i + BATCH, len(texts)):>5} / {len(texts)}", flush=True)

    if len(vectors) != len(texts):
        raise SystemExit(f"asked for {len(texts)} embeddings, got {len(vectors)}")
    return vectors, tokens


def embed_query(text: str, dim: int = 1536) -> list[float]:
    """Embed a question. `search_query`, not `search_document` -- see module docstring."""
    vectors, _ = embed_texts(get_client(), [text], dim=dim, input_type="search_query")
    return vectors[0]


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

UPSERT = """
    INSERT INTO chunks (
        chunk_id, regulation, shape, article, article_title, paragraph, definition,
        annex, annex_title, section, point, token_count, citation_label, text, entity_ids
    )
    VALUES (
        %(chunk_id)s, %(regulation)s, %(shape)s, %(article)s, %(article_title)s,
        %(paragraph)s, %(definition)s, %(annex)s, %(annex_title)s, %(section)s,
        %(point)s, %(token_count)s, %(citation_label)s, %(text)s, %(entity_ids)s
    )
    ON CONFLICT (chunk_id) DO UPDATE SET
        regulation = EXCLUDED.regulation,
        shape = EXCLUDED.shape,
        article = EXCLUDED.article,
        article_title = EXCLUDED.article_title,
        paragraph = EXCLUDED.paragraph,
        definition = EXCLUDED.definition,
        annex = EXCLUDED.annex,
        annex_title = EXCLUDED.annex_title,
        section = EXCLUDED.section,
        point = EXCLUDED.point,
        token_count = EXCLUDED.token_count,
        citation_label = EXCLUDED.citation_label,
        text = EXCLUDED.text,
        entity_ids = EXCLUDED.entity_ids
"""


def upsert_metadata(
    conn: Connection, chunks: list[Chunk], entity_ids: dict[str, list[str]]
) -> int:
    """Write every chunk's metadata. Embeddings are filled in separately."""
    with conn.cursor() as cur:
        cur.executemany(
            UPSERT,
            [
                {
                    **chunk.model_dump(
                        include={
                            "chunk_id", "regulation", "article", "article_title",
                            "paragraph", "definition", "annex", "annex_title",
                            "section", "point", "token_count", "text",
                        }
                    ),
                    "shape": chunk.shape,
                    "citation_label": chunk.citation_label,
                    "entity_ids": entity_ids.get(chunk.chunk_id, []),
                }
                for chunk in chunks
            ],
        )
    return len(chunks)


def embed_chunks(
    chunks: list[Chunk], dim: int = 1536, conn: Connection | None = None
) -> dict:
    """Embed the given chunks and write the vectors into the matching column."""
    if dim not in DIMENSIONS:
        raise ValueError(f"dim must be one of {sorted(DIMENSIONS)}, got {dim}")
    column = DIMENSIONS[dim]
    owned = conn is None
    conn = conn or connect()

    started = time.perf_counter()
    vectors, tokens = embed_texts(
        get_client(), [c.text for c in chunks], dim=dim, progress=True
    )
    embed_seconds = time.perf_counter() - started

    with conn.cursor() as cur:
        cur.executemany(
            f"UPDATE chunks SET {column} = %(vector)s WHERE chunk_id = %(chunk_id)s",
            [
                {"chunk_id": chunk.chunk_id, "vector": vector}
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        )
    if owned:
        conn.close()

    return {
        "dim": dim,
        "chunks": len(chunks),
        "input_tokens": tokens,
        "cost_usd": tokens * PRICE_INPUT_PER_TOKEN,
        "embed_seconds": round(embed_seconds, 1),
    }


def missing_for(conn: Connection, dim: int) -> set[str]:
    column = DIMENSIONS[dim]
    return {
        r[0] for r in conn.execute(
            f"SELECT chunk_id FROM chunks WHERE {column} IS NULL"
        ).fetchall()
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Embed the corpus into pgvector.")
    ap.add_argument("--apply", action="store_true", help="write to the database")
    ap.add_argument(
        "--dim", type=int, action="append", choices=sorted(DIMENSIONS),
        help="which arm to embed; repeatable, defaults to both",
    )
    ap.add_argument(
        "--only-missing", action="store_true",
        help="embed only chunks whose vector for that dimension is NULL",
    )
    ap.add_argument("--no-indexes", action="store_true", help="skip the HNSW build")
    args = ap.parse_args()

    dims = args.dim or sorted(DIMENSIONS)
    chunks = load_corpus()
    print(f"corpus: {len(chunks)} chunks, {sum(c.token_count or 0 for c in chunks):,} tokens")

    if not args.apply:
        by_shape: dict[str, int] = {}
        for chunk in chunks:
            by_shape[chunk.shape] = by_shape.get(chunk.shape, 0) + 1
        est = sum(c.token_count or 0 for c in chunks) * PRICE_INPUT_PER_TOKEN * len(dims)
        print(f"shapes: {by_shape}")
        print(f"would embed at {dims}, estimated ${est:.4f}")
        print("dry run -- pass --apply to write")
        return

    conn = connect()
    ensure_schema(conn)

    print("resolving entities for entity_ids...")
    entity_ids = entity_ids_by_chunk()
    covered = sum(1 for c in chunks if entity_ids.get(c.chunk_id))
    print(f"  {covered} of {len(chunks)} chunks carry at least one resolved entity")

    upsert_metadata(conn, chunks, entity_ids)
    print(f"metadata upserted for {len(chunks)} chunks")

    reports = []
    for dim in dims:
        todo = chunks
        if args.only_missing:
            missing = missing_for(conn, dim)
            todo = [c for c in chunks if c.chunk_id in missing]
        if not todo:
            print(f"dim {dim}: nothing missing, skipping")
            continue
        print(f"dim {dim}: embedding {len(todo)} chunks...")
        reports.append(embed_chunks(todo, dim=dim, conn=conn))

    if not args.no_indexes:
        print("building HNSW indexes...")
        started = time.perf_counter()
        create_indexes(conn)
        print(f"  built in {time.perf_counter() - started:.1f}s")

    for report in reports:
        print(
            f"dim {report['dim']:>4}: {report['chunks']} chunks, "
            f"{report['input_tokens']:,} tokens, ${report['cost_usd']:.4f}, "
            f"{report['embed_seconds']}s"
        )
    for key, value in status(conn).items():
        print(f"{key:15} {value}")
    conn.close()


if __name__ == "__main__":
    main()
