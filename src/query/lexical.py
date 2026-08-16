"""Lexical retrieval, used as a RECALL device and never as a ranker.

WHY THIS EXISTS, AND WHY IT DOES NOT RANK.

Measured 2026-08-16 over the 90 scoreable eval questions, 203 gold references:

    arm                     recall@5   vs rerank (w/l/tie)   McNemar p
    rerank 3.5 (shipping)      49.3%   --                    --
    vector only                47.8%   12/16/62              0.572
    BM25 only                  37.4%    7/30/53              <0.001
    pg_fts only                10.3%    2/67/21              <0.001
    RRF(vector, BM25)          45.3%    9/17/64              0.169
    RRF(vector, pg_fts)        30.5%    8/42/40              <0.001

Every lexical arm and every fusion LOSES at the cap that actually ships. Score
fusion is therefore not implemented here and should not be added without
re-running that table -- RRF dilutes a good dense ordering with a much worse
lexical one, and the k=5 slots are the scarcest resource in the system.

What lexical retrieval *is* good for is reaching text the embedding cannot. The
same measurement, at the pool level rather than the prompt level:

    pool (each arm top-50)        gold in pool   recall   unreachable
    vector top-50                          157    77.3%            46
    vector + BM25                          164    80.8%            39
    vector + pg_fts                        171    84.2%            32
    vector + BM25 + pg_fts                 176    86.7%            27

So the union is worth +19 gold references, and it is the largest single
retrieval gain measured on this corpus. BOTH lexical arms are needed for it:
BM25 alone is +7. They recover different chunks -- pg_fts finds Annex III's
near-identical enumerated points (6 of ag-008's 8), BM25 finds the Art. 99 fine
tiers -- which is why this module runs both and unions rather than picking one.

DEPTH AND WHAT IT COSTS. The recovery is back-loaded; most of it arrives in the
last 20 ranks of the lexical lists:

    depth   +gold   pool p50   pool max
        5      +2         55         59
       10      +4         60         67
       20      +9         71         83
       30     +13         82         99
       50     +19        104        126

Cohere bills rerank per SEARCH UNIT, defined as one query with up to 100
documents (src/config.py:59-71). At depth 50 the median pool is 104 documents,
so the shipping default **crosses that boundary and bills roughly two search
units per query instead of one**. That is a real doubling of rerank cost, taken
deliberately in exchange for the full +19. `LEXICAL_DEPTH = 30` is the
configuration that keeps every pool under 100 and still buys +13; it is the
first thing to try if rerank cost becomes the binding constraint.

SCORE SCALE. Nothing in this module writes `ContextDoc.score`. BM25 scores are
unbounded and corpus-relative, ts_rank_cd is a coverage density, and neither is
comparable to the cosine similarity the vector arm produces or to the [0,1]
relevance the reranker produces. A lexical-only candidate reaches the pool with
`score=None`, which is the honest value: it was retrieved, it was not scored on
any scale the rest of the system understands. See src/query/retriever.py:26-30.
"""

from __future__ import annotations

import collections
import math
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg import Connection

__all__ = [
    "K1",
    "LEXICAL_DEPTH",
    "B",
    "BM25Index",
    "bm25_search",
    "fts_search",
    "lexical_candidates",
    "reset_index",
]

# Okapi BM25 defaults. Not tuned on this corpus -- these are the standard values,
# and tuning them against a 90-question eval set would fit the instrument rather
# than the problem. If they are ever changed it is a finding for
# docs/metrics/query-path.md, with the sweep that justified it.
K1 = 1.2
B = 0.75

# How deep to take each lexical list before unioning. See the module docstring
# for the recall/cost curve and the 100-document search-unit boundary.
LEXICAL_DEPTH = 50

# Terms shorter than this are dropped from the tsquery. Digits are NOT dropped:
# "Article 26", "83(5)" and "35 000 000" are precisely the signal this module
# exists to catch, and a stopword list that removed numbers would answer the
# question before measuring it.
MIN_TERM_LEN = 3

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric runs, digits kept. No stemming.

    Deliberately NOT Postgres's `english` stemmer, which is what `fts_search`
    uses. The two arms tokenize differently and that is a feature: they recover
    different chunks, and making them agree would collapse the union back into
    one arm.
    """
    return _TOKEN.findall(text.lower())


class BM25Index:
    """Okapi BM25 over the whole corpus, held in memory.

    Built from the `chunks` table, which is 1,108 rows -- small enough that an
    in-process inverted index is the simplest thing that works and costs a few
    MB. This would be the wrong shape at 10^6 chunks; at 10^3 it avoids adding a
    Postgres extension (`pg_search`/`rum`) to the deployment for one probe that
    the measurement above says is a recall device, not a ranker.

    Postgres's own full-text search is NOT a substitute: `ts_rank_cd` has no
    term-frequency saturation and no document-length normalisation, which is the
    whole of what BM25 is. `fts_search` below is the second arm, not this one
    implemented differently.
    """

    def __init__(self, corpus: dict[str, str], k1: float = K1, b: float = B):
        if not corpus:
            raise ValueError("cannot build a BM25 index over an empty corpus")
        self.k1, self.b = k1, b
        self.tf: dict[str, collections.Counter[str]] = {}
        self.length: dict[str, int] = {}
        postings: dict[str, list[str]] = collections.defaultdict(list)
        df: collections.Counter[str] = collections.Counter()

        for chunk_id, text in corpus.items():
            terms = tokenize(text)
            counts = collections.Counter(terms)
            self.tf[chunk_id] = counts
            self.length[chunk_id] = len(terms)
            df.update(counts.keys())
            for term in counts:
                postings[term].append(chunk_id)

        n = len(corpus)
        self.n = n
        self.avgdl = sum(self.length.values()) / n
        self.postings = dict(postings)
        # Robertson/Sparck-Jones idf, +1 smoothed so a term in every document
        # scores 0 rather than going negative.
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, question: str, k: int = LEXICAL_DEPTH) -> list[str]:
        """Top-k chunk ids, best first. Ties broken by chunk_id.

        The tiebreak matches `retriever.search_sql`'s, and for the same reason
        recorded there: without it the row at position k varies between runs and
        a committed measurement artifact flakes at the k boundary.
        """
        scores: dict[str, float] = collections.defaultdict(float)
        for term in set(tokenize(question)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for chunk_id in self.postings[term]:
                freq = self.tf[chunk_id][term]
                norm = 1 - self.b + self.b * self.length[chunk_id] / self.avgdl
                scores[chunk_id] += idf * freq * (self.k1 + 1) / (freq + self.k1 * norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [chunk_id for chunk_id, _ in ranked[:k]]


_INDEX: BM25Index | None = None
_LOCK = threading.Lock()


def get_index(conn: Connection | None = None) -> BM25Index:
    """The process-wide BM25 index, built on first use.

    Cached because the corpus is static between ingestions and rebuilding it per
    request would add ~0.4 s to every query for no benefit. Lock-guarded because
    Step 7 serves from a FastAPI worker that can field concurrent requests, and
    two threads racing to build the same index would double the memory for the
    duration of the race.

    `reset_index()` is the invalidation hook. There is no automatic staleness
    check: this index does not know when `chunks` is re-embedded, so a reingest
    inside a live process needs an explicit reset or a restart. That is written
    down rather than solved because ingestion is a batch job that has never run
    against a live API process.
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _LOCK:
        if _INDEX is not None:
            return _INDEX
        owned = conn is None
        if conn is None:
            from src.index.pgvector_schema import connect

            conn = connect()
        try:
            corpus = {
                chunk_id: text
                for chunk_id, text in conn.execute(
                    "SELECT chunk_id, text FROM chunks"
                ).fetchall()
            }
        finally:
            if owned:
                conn.close()
        _INDEX = BM25Index(corpus)
    return _INDEX


def reset_index() -> None:
    """Drop the cached index. For tests and for post-reingest invalidation."""
    global _INDEX
    with _LOCK:
        _INDEX = None


def bm25_search(
    question: str, conn: Connection | None = None, k: int = LEXICAL_DEPTH
) -> list[str]:
    """Top-k chunk ids by Okapi BM25."""
    return get_index(conn).search(question, k)


def fts_search(
    question: str, conn: Connection | None = None, k: int = LEXICAL_DEPTH
) -> list[str]:
    """Top-k chunk ids by Postgres `ts_rank_cd`, OR semantics.

    `plainto_tsquery` is the obvious call and it is the WRONG one. It ANDs every
    term, so a sixteen-term legal question matches **zero** of 1,108 chunks --
    verified on every question in the eval set, where it returned an empty set
    each time rather than a weak ranking. Ranked retrieval needs the disjunction;
    the ranking is what `ts_rank_cd` is for.

    No GIN index is created for this, and unlike the vector path that is NOT
    because the scan is cheap. Measured 2026-08-16: **101 ms**, against ~9 ms for
    the pgvector search beside it, because `to_tsvector('english', text)` is
    recomputed for all 1,108 rows on every query. A functional index --
    `CREATE INDEX ... USING gin (to_tsvector('english', text))`, which is legal
    since the two-argument form is IMMUTABLE -- would take it to single-digit
    milliseconds.

    It is left out because 101 ms sits inside a request whose p95 is 9.8-19.2 s,
    essentially all of it generation: this is ~1% of the latency and indexing it
    would be optimising the wrong term. The number is recorded here so that
    stops being a guess, and so the decision can be revisited the moment either
    the corpus grows or generation stops dominating.

    Terms are filtered to `[a-z0-9]+` by `tokenize`, so interpolating them into
    the tsquery string cannot inject tsquery operators.
    """
    terms = [t for t in dict.fromkeys(tokenize(question)) if len(t) >= MIN_TERM_LEN]
    if not terms:
        return []

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    try:
        rows = conn.execute(
            "SELECT chunk_id "
            "FROM chunks, to_tsquery('english', %(q)s) AS q "
            "WHERE to_tsvector('english', text) @@ q "
            "ORDER BY ts_rank_cd(to_tsvector('english', text), q) DESC, chunk_id "
            "LIMIT %(k)s",
            {"q": " | ".join(terms), "k": k},
        ).fetchall()
    finally:
        if owned:
            conn.close()
    return [row[0] for row in rows]


def lexical_candidates(
    question: str, conn: Connection | None = None, k: int = LEXICAL_DEPTH
) -> list[str]:
    """Union of both lexical arms, BM25 first, then pg_fts, deduped.

    The order is a stable, reproducible enumeration and carries NO ranking
    claim -- the caller reranks the whole pool, and nothing downstream may read
    position here as relevance. BM25 goes first only because it is the stronger
    of the two as a standalone ranker (37.4% vs 10.3% recall@5), so if a caller
    ever truncates this list it truncates the weaker arm.
    """
    seen = dict.fromkeys(bm25_search(question, conn, k))
    for chunk_id in fts_search(question, conn, k):
        seen.setdefault(chunk_id, None)
    return list(seen)
