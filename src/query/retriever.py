"""Vector retrieval path: Embed v4 query at 512 dims -> pgvector top-k.

Feeds the reranker. Graph retrieval lives in cypher_templates + entity_linker.

THE QUERY SHAPE IS `recall_harness.search()`, WIDENED -- AND ONE PART OF IT IS
DELIBERATELY NOT COPIED.

`src/index/recall_harness.py:62` proved the SQL, including the `::vector` cast
that this file repeats for the same reason: psycopg adapts a plain Python list to
`double precision[]`, and an ORDER BY has nothing to infer the target type from.

What is *not* copied is the pair of `SET` statements. `enable_seqscan` and
`hnsw.ef_search` are measurement instruments -- they exist so the harness can
force a plan it would not otherwise get. The connection is autocommit, so there
is no transaction for `SET LOCAL` to scope to and a plain `SET` **persists on the
connection**. At Step 7 that connection comes out of a pool and the next request
inherits it. This module issues no `SET` at all and lets the planner choose,
which at 1,108 rows is the exhaustive scan the eval numbers were measured on.

DIMENSION. ADR-0004 is Accepted at 512. `embedder.embed_query()` defaults to
1536, and that default is *not* changed here -- changing a default silently
changes every caller. `DIM` below is explicit and pinned by a test, because the
failure mode is not an exception: querying the wrong column works, and costs 8x
the latency the ADR was decided on.

SCORE SCALE. `ContextDoc.score` is cosine similarity here, in [-1, 1], and it is
Cohere's `relevance_score`, in [0, 1], after `rerank()`. Both are
higher-is-better so neither ever means the opposite of the other, but they are
**not comparable to each other**. Nothing may threshold or sort across sources on
this field. See src/query/reranker.py.

Usage:
    python -m src.query.retriever --question "..."
    python -m src.query.retriever --question "..." -k 10 --show-text
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.config import price_of, settings
from src.schemas import ContextDoc

if TYPE_CHECKING:
    from psycopg import Connection

__all__ = [
    "DIM",
    "MAX_ENUMERATION",
    "RetrievalResult",
    "RetrieverError",
    "enumerate_provision",
    "retrieve",
    "retrieve_by_annex",
    "retrieve_by_article",
    "retrieve_detailed",
    "retrieve_pool_detailed",
]

# ADR-0004, Accepted 2026-07-31: 512 loses one gold chunk out of 51 against 1536
# and wins 3x on index size and ~8x on latency. Not a preference -- a decision
# with an ADR. Change it there and here together.
DIM = 512


class RetrieverError(RuntimeError):
    """Retrieval could not produce candidates.

    Deliberately not `SystemExit`, for the reason `RouterError` gives at
    src/query/router.py:101 -- this runs inside a FastAPI worker at Step 7 and
    one question getting a 400 must not take the process down with it.
    """


@dataclass
class RetrievalResult:
    """One retrieval, with everything Step 7's cost accumulator needs.

    Latency is split rather than totalled because the two halves have different
    orders of magnitude and different causes: `search_ms` is local SQL (~6 ms at
    this corpus size), `embed_ms` is a network round trip to Cohere. Reporting
    one number would hide which one moved. `docs/metrics/vector-index.md` quotes
    6.68 ms p50 for search alone, and that figure has never included the embed.
    """

    docs: list[ContextDoc] = field(default_factory=list)
    dim: int = DIM
    tokens: int = 0
    cost_usd: float | None = None
    embed_ms: float = 0.0
    search_ms: float = 0.0
    # Populated only by `retrieve_pool_detailed`. `lexical_ms` is a second SQL
    # round trip plus the in-process BM25 scan, kept out of `search_ms` so the
    # vector figure stays comparable to every number measured before the lexical
    # union existed. `lexical_added` is how many candidates the union contributed
    # that the vector draw did not already hold -- the quantity that decides
    # whether the reranker crosses Cohere's 100-document search-unit boundary.
    lexical_ms: float = 0.0
    lexical_added: int = 0

    @property
    def latency_ms(self) -> float:
        return self.embed_ms + self.search_ms + self.lexical_ms


def get_client() -> Any:
    """A Cohere client that raises `RetrieverError` rather than exiting.

    `embedder.get_client()` raises `SystemExit` on a missing key, which is right
    for a CLI and wrong here. Same split as `router.get_client()` at
    src/query/router.py:243.
    """
    import cohere

    if not settings.cohere_api_key:
        raise RetrieverError(f"{settings.cohere_api_key_var} is not set")
    return cohere.ClientV2(api_key=settings.cohere_api_key)


def embed_question(
    question: str, dim: int = DIM, client: Any | None = None
) -> tuple[list[float], int]:
    """Embed one question as a *query*. Returns the vector and billed tokens.

    Calls `embedder._embed_call` rather than `embed_texts`. That is one layer
    down on purpose: `_embed_call` is the tenacity-wrapped unit, so this inherits
    the retry policy and the exact API call shape, while `embed_texts` sits above
    it and converts a non-retryable `ApiError` into `SystemExit`
    (`embedder.py:141`). Catching `SystemExit` would mean catching
    `BaseException`, and `embed_texts` raises it from two sites that mean
    different things. Reaching past the public function is the same trade
    `entity_linker` makes when it imports `_plural_map` and `_trim`.

    `input_type="search_query"` is load-bearing and silent when wrong: Embed v4
    is asymmetric, and passing `search_document` here returns a perfectly valid
    vector that retrieves worse. There is no error to catch, so a test asserts it.
    """
    from cohere.core import ApiError

    from src.index.embedder import RETRYABLE_ERRORS, _embed_call

    client = client or get_client()
    try:
        vectors, tokens = _embed_call(client, [question], dim, "search_query")
    except (ApiError, *RETRYABLE_ERRORS) as exc:
        raise RetrieverError(
            f"embedding the question failed: {type(exc).__name__}: {exc}"
        ) from exc
    return vectors[0], tokens


def _column(dim: int) -> str:
    """The embedding column for a dimension, from `embedder.DIMENSIONS`.

    Imported here rather than at module scope: `embedder` pulls in
    `src.ingest.extract` (embedder.py:44), and the FastAPI app imports this
    module at Step 7. The request path should not import the ingest pipeline to
    look up a column name. Same move as router.py:270.
    """
    from src.index.embedder import DIMENSIONS

    if dim not in DIMENSIONS:
        raise RetrieverError(f"no embedding column for dim={dim}; have {sorted(DIMENSIONS)}")
    return DIMENSIONS[dim]


def search_sql(column: str) -> str:
    """The top-k query. Four details, each of which is a defect if changed.

    1. `%(vec)s::vector` -- see the module docstring. Named rather than
       positional because the vector now appears twice and `(vec, vec, k)`
       positional is a silent swap waiting to happen.
    2. `ORDER BY` is on the **distance expression**, not on the `similarity`
       alias. An alias does not match the HNSW index expression and silently
       disables the index. At 1,108 rows the planner picks a Seq Scan either way
       so the results are identical today -- which is exactly what makes this
       worth writing down rather than discovering on a bigger corpus.
    3. `, chunk_id` breaks ties deterministically. The corpus holds 12 duplicated
       `text` values across 24 rows (e.g. "The name, address and contact details
       of the provider;"), which produce exactly equal distances; without a
       tiebreak the row that lands at position k varies between runs and a
       committed measurement artifact flakes at the k boundary. `recall_harness`
       has no such tiebreak, so the ADR-0004 numbers carry this latent flake --
       noted rather than fixed, because changing the instrument changes the
       numbers the ADR cites.
    4. `citation_label` is SELECTed, never recomputed. It is `NOT NULL UNIQUE` in
       src/index/schema.sql:46 precisely so the answer path reads it.
    """
    return (
        f"SELECT chunk_id, text, citation_label, "
        f"1 - ({column} <=> %(vec)s::vector) AS similarity "
        f"FROM chunks "
        f"WHERE {column} IS NOT NULL "
        f"ORDER BY {column} <=> %(vec)s::vector ASC, chunk_id "
        f"LIMIT %(k)s"
    )


def retrieve_detailed(
    question: str,
    top_k: int = 50,
    *,
    dim: int = DIM,
    conn: Connection | None = None,
    client: Any | None = None,
    vector: list[float] | None = None,
) -> RetrievalResult:
    """`retrieve()` plus the tokens, cost and split latency Step 7 accumulates.

    `vector` is an injection point, mirroring `recall_at_k(vectors=...)`: the
    eval embeds all 21 questions once and reuses them across every k, and a test
    can pass a chunk's own stored embedding to exercise the SQL with no API key.
    `conn` follows the house lifecycle -- caller-owned if passed, closed here if
    not -- so Step 7 hands in a pooled connection and this module owns nothing.
    """
    if top_k < 1:
        raise RetrieverError(f"top_k must be at least 1, got {top_k}")

    column = _column(dim)

    embed_ms = 0.0
    tokens = 0
    if vector is None:
        started = time.perf_counter()
        vector, tokens = embed_question(question, dim, client)
        embed_ms = (time.perf_counter() - started) * 1000
    elif len(vector) != dim:
        raise RetrieverError(f"vector has {len(vector)} components, expected {dim}")

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    try:
        started = time.perf_counter()
        rows = conn.execute(search_sql(column), {"vec": vector, "k": top_k}).fetchall()
        search_ms = (time.perf_counter() - started) * 1000
    finally:
        if owned:
            conn.close()

    if not rows:
        # `WHERE ... IS NOT NULL` means an unloaded or dropped column returns an
        # empty set rather than an error. Silently returning [] would report as
        # 0.0% recall everywhere and read as a catastrophic retrieval finding.
        # `vector-index.md` §Open already contemplates dropping a column.
        raise RetrieverError(
            f"no rows from chunks.{column}: the column is empty or the table is unloaded "
            f"(run `python -m src.index.embedder --apply`)"
        )

    docs = [
        ContextDoc(
            chunk_id=chunk_id,
            text=text,
            citation_label=citation_label,
            source="PASSAGE",
            score=float(similarity),
        )
        for chunk_id, text, citation_label, similarity in rows
    ]
    return RetrievalResult(
        docs=docs,
        dim=dim,
        tokens=tokens,
        cost_usd=price_of(settings.model_embed, tokens),
        embed_ms=embed_ms,
        search_ms=search_ms,
    )


# An article large enough to be a corpus in itself is not an enumeration target.
# `aia-art3` is the definitions article at **68** paragraphs; `aia-annex8` is 27
# and `gdpr-art4` 26. Enumerating any of those would put more text in the prompt
# than the graph budget ever did, which is the failure mode four measurements
# have already found. Above this bound `enumerate_provision` returns [] and the
# caller falls back to ranking -- refusing to enumerate is a valid answer.
MAX_ENUMERATION = 16


def enumerate_provision(
    regulation: str,
    *,
    article: int | None = None,
    annex: str | None = None,
    conn: Connection | None = None,
    limit: int = MAX_ENUMERATION,
) -> list[ContextDoc]:
    """Every chunk of one article or one annex, in statutory order.

    This is the deterministic path: no embedding, no reranking, no scoring. The
    ordering is the legislation's own (`paragraph` / `section, point` ascending),
    which is the only ordering that is correct by construction rather than by
    measurement -- and the whole reason this exists is that the measured
    orderings are what fail on this stratum.

    Returns `[]` rather than raising when the provision does not exist or is
    larger than `limit`. A missing article is a normal outcome of a detector
    guessing a target from a question, and the caller's fallback -- ordinary
    retrieval -- is already correct.

    `score` is None on every doc, for the reason `retrieve_pool_detailed` gives:
    statutory order is not a relevance scale and must not be sorted against one.
    """
    if (article is None) == (annex is None):
        raise RetrieverError("pass exactly one of article= or annex=")

    if article is not None:
        where = "regulation = %(reg)s AND article = %(art)s"
        # `paragraph` and `definition` are disjoint by shape (schema.sql:17-20);
        # COALESCE orders a paragraph article by paragraph and the definitions
        # article by definition number without needing to know which it is.
        order = "COALESCE(paragraph, definition), chunk_id"
        params: dict[str, object] = {"reg": regulation, "art": article}
    else:
        where = "regulation = %(reg)s AND annex = %(annex)s"
        order = "section NULLS FIRST, point, chunk_id"
        params = {"reg": regulation, "annex": annex}

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    try:
        rows = conn.execute(
            f"SELECT chunk_id, text, citation_label FROM chunks "
            f"WHERE {where} ORDER BY {order} LIMIT %(lim)s",
            {**params, "lim": limit + 1},
        ).fetchall()
    finally:
        if owned:
            conn.close()

    # limit + 1 above, then refuse: a truncated enumeration is worse than none.
    # Half of Article 3 is not "the definitions", it is an arbitrary prefix that
    # reads as complete, and an answer built on it would be confidently partial.
    if len(rows) > limit:
        return []

    return [
        ContextDoc(
            chunk_id=chunk_id, text=text, citation_label=citation_label,
            source="PASSAGE", score=None,
        )
        for chunk_id, text, citation_label in rows
    ]


def retrieve_by_article(
    regulation: str, article: int, *, conn: Connection | None = None,
    limit: int = MAX_ENUMERATION,
) -> list[ContextDoc]:
    """Every paragraph of one article, in statutory order. See `enumerate_provision`."""
    return enumerate_provision(regulation, article=article, conn=conn, limit=limit)


def retrieve_by_annex(
    regulation: str, annex: str, *, conn: Connection | None = None,
    limit: int = MAX_ENUMERATION,
) -> list[ContextDoc]:
    """Every point of one annex, in statutory order. `annex` is a roman numeral."""
    return enumerate_provision(regulation, annex=annex, conn=conn, limit=limit)


def retrieve_pool_detailed(
    question: str,
    top_k: int = 50,
    *,
    dim: int = DIM,
    conn: Connection | None = None,
    client: Any | None = None,
    vector: list[float] | None = None,
    lexical_depth: int | None = None,
) -> RetrievalResult:
    """Vector top-k UNIONED with the lexical candidates. The reranker's input.

    UNION, NOT FUSION. The two orderings are concatenated and deduped; no score
    is combined, compared or rescaled across them. That is a measured choice, not
    a simplification -- reciprocal rank fusion of these same two lists LOSES at
    k=5 (45.3% vs 49.3% for the shipping arm, 9 wins against 17 losses). The
    lexical arms are here to widen what the cross-encoder can see, and the
    cross-encoder does the ranking. `src/query/lexical.py` carries the table.

    The vector draw keeps its cosine scores and its order; lexical-only
    candidates are appended with `score=None`, because BM25 and ts_rank_cd are
    not on any scale comparable to cosine similarity. Anything downstream that
    sorts this list on `score` is a defect -- see the module docstring.

    Ordering of the returned list is vector-first, then lexical-only in
    `lexical_candidates` order. That is a stable enumeration for reproducibility
    and NOT a relevance claim. Cohere's rerank is order-insensitive (it scores
    every document against the query independently), so the concatenation order
    cannot leak into the ranking -- but a future caller that truncates this list
    would be truncating the lexical arm, which is why the vector draw goes first.

    Set `lexical_depth=0` to get exactly `retrieve_detailed`'s result with the
    lexical fields zeroed. That is the A/B control, and it is how the benchmark
    runs the pre-union arm through the identical code path.
    """
    from src.query.lexical import LEXICAL_DEPTH, lexical_candidates

    depth = LEXICAL_DEPTH if lexical_depth is None else lexical_depth
    result = retrieve_detailed(
        question, top_k, dim=dim, conn=conn, client=client, vector=vector
    )
    if depth < 1:
        return result

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    try:
        started = time.perf_counter()
        candidates = lexical_candidates(question, conn, depth)
        have = {doc.chunk_id for doc in result.docs}
        missing = [chunk_id for chunk_id in candidates if chunk_id not in have]
        if missing:
            rows = conn.execute(
                "SELECT chunk_id, text, citation_label FROM chunks "
                "WHERE chunk_id = ANY(%s)",
                (missing,),
            ).fetchall()
            # Hydrate in `missing` order, not in the order Postgres returned
            # them: `= ANY` gives no ordering guarantee, and the artifact this
            # feeds is diffed between runs.
            by_chunk = {cid: (text, label) for cid, text, label in rows}
            for chunk_id in missing:
                found = by_chunk.get(chunk_id)
                if found is None:
                    continue
                text, label = found
                result.docs.append(
                    ContextDoc(
                        chunk_id=chunk_id,
                        text=text,
                        citation_label=label,
                        source="PASSAGE",
                        score=None,
                    )
                )
        result.lexical_ms = (time.perf_counter() - started) * 1000
        result.lexical_added = len(missing)
    finally:
        if owned:
            conn.close()
    return result


def retrieve(
    question: str,
    top_k: int = 50,
    *,
    dim: int = DIM,
    conn: Connection | None = None,
    client: Any | None = None,
    vector: list[float] | None = None,
) -> list[ContextDoc]:
    """Top-k retrieval over pgvector, nearest first.

    Returns ContextDoc, not Chunk (ADR-0011): a similarity score has to travel
    with each result for the reranker to have anything to improve on, and Chunk
    is extra="forbid" with no score field on purpose -- see
    docs/adr/adr-0011-context-document-model.md.
    """
    return retrieve_detailed(
        question, top_k, dim=dim, conn=conn, client=client, vector=vector
    ).docs


def main() -> int:
    from src.index.embedder import DIMENSIONS

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", required=True)
    parser.add_argument("-k", type=int, default=50)
    parser.add_argument("--dim", type=int, default=DIM, choices=sorted(DIMENSIONS))
    parser.add_argument("--show-text", action="store_true", help="first line of each chunk")
    args = parser.parse_args()

    try:
        result = retrieve_detailed(args.question, args.k, dim=args.dim)
    except RetrieverError as exc:
        print(f"retrieval failed: {exc}")
        return 1

    cost = f"${result.cost_usd:.8f}" if result.cost_usd is not None else "unpriced"
    print(
        f"{len(result.docs)} docs at dim={result.dim}  "
        f"(embed {result.embed_ms:.0f} ms, search {result.search_ms:.1f} ms, "
        f"{result.tokens} tokens, {cost})\n"
    )
    for rank, doc in enumerate(result.docs, 1):
        line = f"{rank:>3}  {doc.score:.4f}  {doc.citation_label:<28} {doc.chunk_id}"
        if args.show_text:
            line += f"\n     {doc.text.splitlines()[0][:96]}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
