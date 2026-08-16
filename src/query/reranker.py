"""Rerank 3.5 cross-encoder: reorder top-50 -> top-5 passages.

WHAT THIS MODULE IS FOR. `docs/metrics/vector-index.md` §Open named Rerank 3.5
over the top-50 as "the obvious lever on the 30% two-hop figure", and this is
that claim made falsifiable. The lever has room: measured 2026-08-03 at 512 dims
over the 21 labeled questions, the top-50 candidate pool contains 41 of 51 gold
references while the top-10 contains 28. Reordering alone can therefore reach
41/51 -- so a reranker that fails to move the number has failed on its merits,
not because the candidates were not there.

THE COMPARISON IS SAME-k, AND THAT IS A CORRECTION TO THE PHASE PLAN.

The plan asked for "recall@5-after-rerank against recall@10-before". That is not
a fair comparison and the arithmetic says so before any measurement runs. Micro
recall is capped by `sum(min(gold_i, k))`: 45/51 at k=5 against 50/51 at k=10.
A *perfect* reranker scores 88.2% at k=5 against a pre-rerank ceiling of 98.0%,
so reporting that difference as a rerank delta charges the reranker 9.8
percentage points of pure arithmetic. The whole gap is one row -- `ag-001`
declares 11 gold chunks and every other row has at most 4.

So `scoreboard()` reports pre@k against post@k at the same k, prints the ceiling
next to every figure, and reports the top-5 slice separately as a production
choice rather than as the headline. Two-hop and every other stratum except
`aggregation` cap at 100% at both k, so their deltas need no adjustment at all.

BILLING. Rerank is charged per search unit, not per token, and Cohere reports the
figure on the response (`meta.billed_units.search_units`). That value is recorded
per call and stored in the artifact, so the quantity behind every published cost
is auditable. The *rate* is the weakest number in `src/config.py` -- see the
block above `price_of_rerank()`.

SCORE SCALE. The output's `score` is Cohere's `relevance_score`, in [0, 1]. The
input's was cosine similarity, in [-1, 1]. Both are higher-is-better, so neither
inverts the other, but they are not comparable and nothing may threshold or sort
across sources on that field.

Usage:
    python -m src.query.reranker --question "..."     # retrieve, then rerank
    python -m src.query.reranker --eval               # the k-matrix, from the artifact
    python -m src.query.reranker --eval --refresh     # re-run live (needs an API key)
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import RERANK_PRICE_SOURCE, price_of, price_of_rerank, settings
from src.query.retriever import DIM, RetrieverError, retrieve_detailed
from src.schemas import ContextDoc

if TYPE_CHECKING:
    from psycopg import Connection

__all__ = ["RerankError", "RerankResult", "rerank", "rerank_detailed"]

ROOT = Path(__file__).resolve().parents[2]

# Beside the eval set it measures, and tracked, for the reason router.py:66 gives:
# the tests and the metrics doc read their numbers out of it, and both must work
# with no API key and no spend.
ARTIFACT = ROOT / "eval" / "rerank-eval.jsonl"

# The candidate pool the reranker reorders, and the ks the matrix reports.
CANDIDATES = 50
KS = (5, 10, 50)

# Pre-registered 2026-08-03, before the first rerank ran. ADR-0004 declared 2
# gold chunks out of 51 to be inside this eval set's resolution and refused to
# decide 1536-vs-512 on a 1-chunk difference. The same threshold binds here, or
# the rule was never a rule.
RESOLUTION_CHUNKS = 2

# A rerank call slower than this is reported by name rather than folded into a
# percentile. See the comment in `scoreboard()`: the tail measured here belongs
# to the API key's tier, not to the model.
STALL_MS = 1_000


class RerankError(RuntimeError):
    """Reranking could not produce an ordering.

    Not `SystemExit`, for the reason recorded at src/query/router.py:101.
    """


@dataclass
class RerankResult:
    """One rerank call, with the billed quantity Cohere actually reported."""

    docs: list[ContextDoc] = field(default_factory=list)
    documents_sent: int = 0
    search_units: float = 0.0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    # 1 means the call succeeded first time and `latency_ms` is the model's
    # latency. Anything higher means most of `latency_ms` was retry backoff.
    attempts: int = 1


def get_client() -> Any:
    """A Cohere client that raises `RerankError` rather than exiting."""
    import cohere

    if not settings.cohere_api_key:
        raise RerankError(f"{settings.cohere_api_key_var} is not set")
    return cohere.ClientV2(api_key=settings.cohere_api_key)


def _rerank_call(
    client: Any, question: str, documents: list[str], top_n: int
) -> tuple[Any, int]:
    """The retrying unit, mirroring `embedder._embed_call`. Returns the attempts.

    Retries exist here for the same reason they exist on every other API call
    site in this repo, and the omission was not hypothetical: the first eval
    sweep died on a 429 partway through, because a Cohere trial key allows 10
    calls a minute. Without a retry the reranker propagates a transient rate
    limit as a hard `RerankError` -- which on the Step 7 request path means a
    question fails for a reason that would have cleared in two seconds.

    The attempt count is returned because it is the difference between a latency
    measurement and a rate-limit measurement. A call that retried spent most of
    its wall clock asleep in the backoff, and averaging that into a p95 produces
    a number about the API key rather than about the model -- the first sweep
    reported a rerank p95 of 83 seconds for exactly that reason.
    """
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    from src.index.embedder import RETRYABLE_ERRORS

    @retry(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _call() -> Any:
        return client.rerank(
            model=settings.model_rerank,
            query=question,
            documents=documents,
            top_n=top_n,
        )

    response = _call()
    # `_call.statistics` and not `_call.retry.statistics`. The second is
    # permanently `{}` -- since tenacity 8.2.3 the wrapper runs `copy =
    # self.copy()` per call and assigns *the copy's* statistics to
    # `wrapped_f.statistics`, leaving `wrapped_f.retry` as the original
    # controller which never executes. Corrected in Step 6 against tenacity
    # 9.1.4; every `attempts` value in `eval/rerank-eval.jsonl` was written by
    # the broken accessor and is therefore 1 by construction, which is what
    # `docs/metrics/query-path.md` now says beside the three stalls it used to
    # attribute to the API tier on the strength of that field.
    return response, int(_call.statistics.get("attempt_number", 1))


def rerank_detailed(
    question: str,
    candidates: list[ContextDoc],
    top_n: int = 5,
    *,
    client: Any | None = None,
) -> RerankResult:
    """`rerank()` plus the billed search units, cost and latency for Step 7."""
    if not candidates:
        # No call at all. An empty search would still be a billable request, and
        # there is nothing to order.
        return RerankResult(cost_usd=0.0)
    if top_n < 1:
        raise RerankError(f"top_n must be at least 1, got {top_n}")

    blank = [doc.chunk_id for doc in candidates if not doc.text.strip()]
    if blank:
        # `text` is NOT NULL in schema.sql but carries no length CHECK. Cohere
        # rejects empty documents, and the resulting 400 would name an offset
        # rather than a chunk.
        raise RerankError(f"candidates have empty text: {blank}")

    # Clamped rather than passed through: asking for more than was sent is a
    # caller's arithmetic slip, not a request Cohere should adjudicate.
    top_n = min(top_n, len(candidates))

    from cohere.core import ApiError

    client = client or get_client()
    started = time.perf_counter()
    try:
        response, attempts = _rerank_call(
            client, question, [doc.text for doc in candidates], top_n
        )
    except ApiError as exc:
        raise RerankError(f"rerank failed: {type(exc).__name__}: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000

    results = list(response.results or [])
    for result in results:
        if not 0 <= result.index < len(candidates):
            raise RerankError(
                f"rerank returned index {result.index} for {len(candidates)} candidates"
            )

    # Cohere already returns these sorted by relevance, but the corpus holds 12
    # duplicated `text` values across 24 rows, which draw exactly equal scores.
    # Without the `index` tiebreak the row that lands at position n varies
    # between runs and the committed artifact flakes at the k boundary.
    results.sort(key=lambda r: (-r.relevance_score, r.index))

    # `model_copy` rather than assignment: a long-lived worker can hand the same
    # candidate list to two requests, and mutating `score` in place would let one
    # request's rerank scores appear inside another's retrieval results.
    docs = [
        candidates[result.index].model_copy(update={"score": float(result.relevance_score)})
        for result in results
    ]

    billed = response.meta.billed_units if response.meta else None
    search_units = float(getattr(billed, "search_units", None) or 0.0) if billed else 0.0

    return RerankResult(
        docs=docs,
        documents_sent=len(candidates),
        search_units=search_units,
        cost_usd=price_of_rerank(search_units),
        latency_ms=latency_ms,
        attempts=attempts,
    )


def rerank(
    question: str,
    candidates: list[ContextDoc],
    top_n: int = 5,
    *,
    client: Any | None = None,
) -> list[ContextDoc]:
    """Rerank retrieved candidates with Rerank 3.5.

    ContextDoc, not Chunk (ADR-0011) -- the input already carries retrieve()'s
    similarity score, and the output's score becomes the rerank score, so a
    caller cannot mistake a similarity number for a relevance one. The input list
    is not modified.
    """
    return rerank_detailed(question, candidates, top_n, client=client).docs


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def load_artifact() -> list[dict]:
    if not ARTIFACT.exists():
        raise SystemExit(
            f"{ARTIFACT} does not exist. Run --eval --refresh with an API key to build it."
        )
    return [
        json.loads(line)
        for line in ARTIFACT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sweep(
    conn: Connection | None = None, client: Any | None = None
) -> list[dict]:
    """Retrieve top-50 and rerank all of it, for every labeled question.

    The full reranked 50 is stored, not the top 5. Storing only what goes into a
    prompt would mean paying again to answer "what would recall@10 have been?",
    which is the question the whole k-matrix is made of.

    The two unscored rows -- one `out-of-scope`, one `unanswerable` -- are swept
    too. They have no gold so they contribute nothing to recall, but what a
    cross-encoder does with a question the corpus cannot answer is Step 6's
    problem and this is the only place it costs nothing to observe.

    Every question is embedded in **one** batched call and the vectors are handed
    to `retrieve_detailed(vector=...)`. That is what the injection point is for:
    it makes the sweep one embed call instead of 23, which halves the API calls
    and keeps the arms comparable, since every k in the matrix is then measured
    against the identical vector.
    """
    from src.index.embedder import embed_texts
    from src.index.recall_harness import QUESTIONS

    rows = [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    client = client or get_client()

    started = time.perf_counter()
    vectors, embed_tokens = embed_texts(
        client, [row["question"] for row in rows], dim=DIM, input_type="search_query"
    )
    embed_ms = (time.perf_counter() - started) * 1000 / len(rows)
    tokens_each = embed_tokens / len(rows)

    from src.query.lexical import LEXICAL_DEPTH
    from src.query.retriever import retrieve_pool_detailed

    out: list[dict] = []
    try:
        for row, vector in zip(rows, vectors, strict=True):
            retrieved = retrieve_detailed(
                row["question"], CANDIDATES, dim=DIM, conn=conn, vector=vector
            )
            reranked = rerank_detailed(
                row["question"], retrieved.docs, top_n=CANDIDATES, client=client
            )
            # The lexical-union arm, swept beside the vector-only one rather than
            # in place of it. Both orderings in one artifact is what lets the
            # end-to-end comparison replay two arms from a single sweep, and it
            # is the same reasoning that put `retrieved` next to `reranked`: an
            # arm you cannot replay is an arm you pay to re-measure.
            #
            # The vector half is NOT re-drawn -- `vector=` reuses the identical
            # embedding, so the two pools differ only by the lexical union and a
            # difference between the arms cannot be a different vector draw.
            pooled = retrieve_pool_detailed(
                row["question"], CANDIDATES, dim=DIM, conn=conn, vector=vector,
                lexical_depth=LEXICAL_DEPTH,
            )
            pool_reranked = rerank_detailed(
                row["question"], pooled.docs, top_n=len(pooled.docs), client=client
            )
            out.append({
                "id": row["id"],
                "stratum": row["stratum"],
                "gold": row.get("source_chunk_ids", []),
                "scored": bool(row.get("source_chunk_ids")),
                "dim": retrieved.dim,
                "plan": "exact",
                "candidates": CANDIDATES,
                "retrieved": [d.chunk_id for d in retrieved.docs],
                "retrieved_scores": [round(d.score, 6) for d in retrieved.docs],
                "reranked": [d.chunk_id for d in reranked.docs],
                "rerank_scores": [round(d.score, 6) for d in reranked.docs],
                # The lexical-union arm. `pool` is the enlarged candidate set in
                # retrieval order (vector draw first, then lexical-only);
                # `pool_reranked` is that set ordered by the cross-encoder.
                "pool": [d.chunk_id for d in pooled.docs],
                "pool_size": len(pooled.docs),
                "lexical_added": pooled.lexical_added,
                "lexical_depth": LEXICAL_DEPTH,
                "pool_reranked": [d.chunk_id for d in pool_reranked.docs],
                "pool_rerank_scores": [round(d.score, 6) for d in pool_reranked.docs],
                "pool_documents_sent": pool_reranked.documents_sent,
                "pool_search_units": pool_reranked.search_units,
                "pool_rerank_cost_usd": pool_reranked.cost_usd,
                "lexical_ms": round(pooled.lexical_ms, 2),
                # Amortised: the sweep embeds all questions in one batched call,
                # so there is no per-question token count or round trip to
                # report. The batch total divided by the row count is the honest
                # per-question figure for a *sweep*; a single `/ask` request pays
                # a full round trip, which is what `--question` prints.
                "embed_tokens": round(tokens_each, 2),
                "embed_amortised": True,
                "documents_sent": reranked.documents_sent,
                "search_units": reranked.search_units,
                "rerank_attempts": reranked.attempts,
                "embed_cost_usd": price_of(settings.model_embed, tokens_each),
                "rerank_cost_usd": reranked.cost_usd,
                "latency_ms": {
                    "embed": round(embed_ms, 2),
                    "search": round(retrieved.search_ms, 2),
                    "rerank": round(reranked.latency_ms, 2),
                    # Kept as separate keys, not folded into the two above: the
                    # union arm's rerank sends ~2x the documents and its latency
                    # is the cost of the +19 gold, which a merged figure hides.
                    "lexical": round(pooled.lexical_ms, 2),
                    "pool_rerank": round(pool_reranked.latency_ms, 2),
                },
            })
    finally:
        if owned:
            conn.close()
    return out


def _found(ranked: list[str], gold: list[str], k: int) -> int:
    return len(set(gold) & set(ranked[:k]))


def scoreboard(artifact: list[dict]) -> dict[str, Any]:
    """Every published number, recomputed from the artifact alone.

    Pure -- no database, no API key, no network. That is what lets the tests and
    `docs/metrics/query-path.md` assert on these figures.

    `cap` is the arithmetic ceiling `sum(min(|gold_i|, k))`; `oracle` is the
    ceiling imposed by the candidate pool, `sum(min(|gold_i & top50_i|, k))`,
    computed per query rather than as `min(recall@50, cap@k)` -- the aggregate
    form is loose and would permit a "shortfall" that is impossible to close.
    """
    scored = [row for row in artifact if row["scored"]]
    gold_total = sum(len(row["gold"]) for row in scored)

    def block(rows: list[dict]) -> dict[str, Any]:
        gold = sum(len(r["gold"]) for r in rows)
        out: dict[str, Any] = {"n": len(rows), "gold": gold}
        for k in KS:
            out[f"cap@{k}"] = sum(min(len(r["gold"]), k) for r in rows)
            out[f"pre@{k}"] = sum(_found(r["retrieved"], r["gold"], k) for r in rows)
            out[f"post@{k}"] = sum(_found(r["reranked"], r["gold"], k) for r in rows)
            out[f"delta@{k}"] = out[f"post@{k}"] - out[f"pre@{k}"]
            out[f"hit_pre@{k}"] = sum(_found(r["retrieved"], r["gold"], k) > 0 for r in rows)
            out[f"hit_post@{k}"] = sum(_found(r["reranked"], r["gold"], k) > 0 for r in rows)
        for k in (5, 10):
            out[f"oracle@{k}"] = sum(
                min(_found(r["retrieved"], r["gold"], r["candidates"]), k) for r in rows
            )

        # Where the gold that never reaches the prompt is lost. Three disjoint
        # causes that sum exactly to `gold - post@k`, separated because the fix
        # for each is a different piece of the system and "recall@5 is 49%" hides
        # which one is actually binding:
        #
        #   retrieval_loss  gold that is not in the candidate pool at all. No
        #                   reranker and no cap can reach it. Only embedding,
        #                   chunking or a wider pool moves this.
        #   cap_loss@k      gold that IS in the pool but cannot fit in k slots
        #                   even under a perfect ordering. Only raising k moves
        #                   this.
        #   order_loss@k    gold that is in the pool and would fit, but the
        #                   reranker placed non-gold above it. Only a better
        #                   ranker moves this.
        #
        # Measured 2026-08-16 at k=5: 46 / 6 / 51. The ordering term is 8.5x the
        # cap term, and the cap term is zero on every stratum except aggregation.
        # That is the reason raising PASSAGE_TOP_N is not the fix, and it is the
        # kind of claim that has to be recomputed on every refresh rather than
        # quoted from a doc -- hence a derived line here and not a note.
        out["pool"] = sum(
            _found(r["retrieved"], r["gold"], r["candidates"]) for r in rows
        )
        out["retrieval_loss"] = gold - out["pool"]
        for k in (5, 10):
            out[f"cap_loss@{k}"] = out["pool"] - out[f"oracle@{k}"]
            out[f"order_loss@{k}"] = out[f"oracle@{k}"] - out[f"post@{k}"]

        # THE LEXICAL-UNION ARM, on rows that carry it. Guarded with `all(...)`
        # rather than assumed: artifacts swept before 2026-08-16 have no `pool`
        # column, and a KeyError here would make every historical artifact
        # unreadable by the tool that publishes its numbers.
        if rows and all("pool_reranked" in r for r in rows):
            out["union_pool"] = sum(
                len(set(r["gold"]) & set(r["pool"])) for r in rows
            )
            out["union_retrieval_loss"] = gold - out["union_pool"]
            for k in (5, 10):
                oracle = sum(
                    min(len(set(r["gold"]) & set(r["pool"])), k) for r in rows
                )
                post = sum(_found(r["pool_reranked"], r["gold"], k) for r in rows)
                out[f"union_oracle@{k}"] = oracle
                out[f"union_post@{k}"] = post
                out[f"union_delta@{k}"] = post - out[f"post@{k}"]
                out[f"union_cap_loss@{k}"] = out["union_pool"] - oracle
                out[f"union_order_loss@{k}"] = oracle - post
        return out

    strata: dict[str, list[dict]] = collections.defaultdict(list)
    for row in scored:
        strata[row["stratum"]].append(row)

    # Rerank latency has a tail that is not the model's. Measured 2026-08-03 on a
    # Cohere trial key: 20 of 23 calls returned in 230-340 ms and three stalled
    # (3.9 s, 82.4 s, 83.5 s) -- all with `attempts=1`, so this was not retry
    # backoff. The trial tier held a single HTTP request open rather than
    # returning 429, which means a p95 over 23 calls describes the API key and
    # not Rerank 3.5. The stalls are listed individually instead of being
    # averaged into a percentile that would then get quoted as a model figure.
    clean = [row for row in artifact if row.get("rerank_attempts", 1) == 1]
    latencies = {
        "embed": sorted(row["latency_ms"]["embed"] for row in artifact),
        "search": sorted(row["latency_ms"]["search"] for row in artifact),
        "rerank": sorted(row["latency_ms"]["rerank"] for row in clean),
    }
    stalls = sorted(
        ((row["id"], row["latency_ms"]["rerank"]) for row in artifact
         if row["latency_ms"]["rerank"] > STALL_MS),
        key=lambda pair: -pair[1],
    )

    unscored = [row for row in artifact if not row["scored"]]
    return {
        "gold_total": gold_total,
        "queries": len(scored),
        "overall": block(scored),
        "strata": {name: block(rows) for name, rows in strata.items()},
        "latency_p50": {p: statistics.median(v) for p, v in latencies.items() if v},
        "latency_p95": {
            p: v[max(0, round(len(v) * 0.95) - 1)] for p, v in latencies.items() if v
        },
        "rerank_calls": len(artifact),
        "rerank_calls_clean": len(clean),
        "rerank_stalls": stalls,
        "rerank_retried": [row["id"] for row in artifact
                           if row.get("rerank_attempts", 1) > 1],
        "search_units_total": sum(row["search_units"] for row in artifact),
        "rerank_cost_total": (
            None
            if any(row["rerank_cost_usd"] is None for row in artifact)
            else sum(row["rerank_cost_usd"] for row in artifact)
        ),
        "embed_cost_total": (
            None
            if any(row["embed_cost_usd"] is None for row in artifact)
            else sum(row["embed_cost_usd"] for row in artifact)
        ),
        # Not a recall number: these two rows have no gold. The question is
        # whether a cross-encoder signals low confidence on something the corpus
        # cannot answer, which Step 6's refusal path will care about. n=2.
        "unscored_top_score": {
            row["id"]: (row["rerank_scores"][0] if row["rerank_scores"] else None)
            for row in unscored
        },
    }


def _report(artifact: list[dict]) -> int:
    board = scoreboard(artifact)
    total = board["gold_total"]
    overall = board["overall"]

    print(
        f"\n{board['queries']} labeled queries, {total} gold references, "
        f"dim={artifact[0]['dim']}, exact search, {CANDIDATES} candidates reranked\n"
    )

    print("Micro recall. `cap` is what the gold counts allow; `oracle` is what the")
    print("candidate pool allows. A delta of <= "
          f"{RESOLUTION_CHUNKS} chunks is inside this set's resolution (ADR-0004).\n")
    print(f"{'k':>4} {'pre':>12} {'post':>12} {'delta':>8} {'cap':>10} {'oracle':>10} "
          f"{'hit pre':>9} {'hit post':>9}")
    print("-" * 82)
    for k in KS:
        oracle = overall.get(f"oracle@{k}")
        print(
            f"{k:>4} "
            f"{overall[f'pre@{k}']:>5}/{total:<3} {overall[f'pre@{k}'] / total:>5.1%} "
            f"{overall[f'post@{k}']:>5}/{total:<3} {overall[f'post@{k}'] / total:>5.1%} "
            f"{overall[f'delta@{k}']:>+8d} "
            f"{overall[f'cap@{k}']:>7}/{total:<2} "
            f"{(f'{oracle:>7}/{total:<2}' if oracle is not None else '      -   '):>10} "
            f"{overall[f'hit_pre@{k}'] / overall['n']:>8.1%} "
            f"{overall[f'hit_post@{k}'] / overall['n']:>8.1%}"
        )

    print(f"\n{'stratum':<17} {'n':>2} {'gold':>4}  "
          f"{'pre@5':>7} {'post@5':>7} {'d':>4}   {'pre@10':>7} {'post@10':>7} {'d':>4}   "
          f"{'cap@5':>5} {'cap@10':>6} {'orc@10':>6}")
    for name, s in sorted(board["strata"].items(), key=lambda kv: kv[1]["pre@10"] / kv[1]["gold"]):
        print(
            f"{name:<17} {s['n']:>2} {s['gold']:>4}  "
            f"{s['pre@5']:>4}/{s['gold']:<2} {s['post@5']:>4}/{s['gold']:<2} {s['delta@5']:>+4d}   "
            f"{s['pre@10']:>4}/{s['gold']:<2} {s['post@10']:>4}/{s['gold']:<2} {s['delta@10']:>+4d}   "
            f"{s['cap@5']:>5} {s['cap@10']:>6} {s['oracle@10']:>6}"
        )

    # The same shortfall the table above shows as one number, split by cause.
    # Printed per stratum because the split is not uniform: the cap term is zero
    # everywhere except aggregation, so a corpus-wide "the cap is tight" reading
    # of the overall row would be wrong on five of six strata.
    print(f"\nwhere the gold goes, k=5 (retrieval + cap + ordering = {total} - post@5):")
    print(f"  {'stratum':<17} {'gold':>4} {'not retrieved':>14} {'over cap':>9} "
          f"{'mis-ordered':>12} {'reached':>8}")
    for name, s in sorted(board["strata"].items(),
                          key=lambda kv: -kv[1]["order_loss@5"]):
        print(f"  {name:<17} {s['gold']:>4} {s['retrieval_loss']:>14} "
              f"{s['cap_loss@5']:>9} {s['order_loss@5']:>12} {s['post@5']:>8}")
    print(f"  {'ALL':<17} {overall['gold']:>4} {overall['retrieval_loss']:>14} "
          f"{overall['cap_loss@5']:>9} {overall['order_loss@5']:>12} "
          f"{overall['post@5']:>8}")
    if overall["cap_loss@5"] and overall["order_loss@5"] > overall["cap_loss@5"]:
        print(f"  ordering costs {overall['order_loss@5'] / overall['cap_loss@5']:.1f}x "
              "what the cap costs: raising k moves the smaller term.")

    if "union_pool" in overall:
        # THE HEADLINE OF THIS BLOCK IS THE GAP BETWEEN ITS TWO COLUMNS, so they
        # are printed side by side rather than in two tables a reader has to
        # difference by hand: the union buys a large amount of POOL coverage and
        # almost none of it survives into the PROMPT.
        sizes = sorted(row["pool_size"] for row in artifact)
        over = sum(1 for s in sizes if s > 100)
        print(f"\nlexical union arm (BM25 + Postgres FTS, depth "
              f"{artifact[0].get('lexical_depth', '?')}), measured not shipped --"
              f"\nsee answer_path.LEXICAL_DEPTH_LIVE:")
        print(f"  {'':<22}{'vector pool':>14}{'union pool':>13}{'delta':>8}")
        print(f"  {'gold in pool':<22}{overall['pool']:>14}{overall['union_pool']:>13}"
              f"{overall['union_pool'] - overall['pool']:>+8d}")
        for k in (5, 10):
            print(f"  {f'gold in top-{k}':<22}{overall[f'post@{k}']:>14}"
                  f"{overall[f'union_post@{k}']:>13}{overall[f'union_delta@{k}']:>+8d}")
        print(f"  {'-> retrieval loss':<22}{overall['retrieval_loss']:>14}"
              f"{overall['union_retrieval_loss']:>13}"
              f"{overall['union_retrieval_loss'] - overall['retrieval_loss']:>+8d}")
        print(f"  {'-> cap loss @5':<22}{overall['cap_loss@5']:>14}"
              f"{overall['union_cap_loss@5']:>13}"
              f"{overall['union_cap_loss@5'] - overall['cap_loss@5']:>+8d}")
        print(f"  {'-> ordering loss @5':<22}{overall['order_loss@5']:>14}"
              f"{overall['union_order_loss@5']:>13}"
              f"{overall['union_order_loss@5'] - overall['order_loss@5']:>+8d}")
        print(f"\n  documents sent to rerank: was a flat {CANDIDATES}, now "
              f"p50 {statistics.median(sizes):.0f}, p95 "
              f"{sizes[max(0, round(len(sizes) * 0.95) - 1)]}, max {max(sizes)}")
        units_old = sum(row["search_units"] for row in artifact)
        units_new = sum(row["pool_search_units"] for row in artifact)
        print(f"  pools over Cohere's 100-document search unit: {over}/{len(sizes)}")
        print(f"  billed search units: {units_old:.0f} -> {units_new:.0f} "
              f"({units_new / units_old:.2f}x)" if units_old else "")
        print("  READ THIS AS: the union moves gold from a stage that could not "
              "reach it\n  to a stage that ranks it below noise. Retrieval loss "
              "falls, ordering loss rises.")

    print("\nlatency by component, ms (p50 / p95) -- reported separately because "
          "vector-index.md's\n6.68 ms is SQL only and never included the embedding "
          "round trip:")
    for part in ("embed", "search", "rerank"):
        if part not in board["latency_p50"]:
            print(f"  {part:<8} no call to measure")
            continue
        note = "   (amortised over one batched call)" if part == "embed" else ""
        print(f"  {part:<8} {board['latency_p50'][part]:>8.2f} / "
              f"{board['latency_p95'][part]:>8.2f}{note}")

    if board["rerank_stalls"]:
        p50 = board["latency_p50"].get("rerank")
        print(f"\n  rerank p95 above is NOT a model figure: "
              f"{len(board['rerank_stalls'])} of {board['rerank_calls']} calls stalled")
        for qid, ms in board["rerank_stalls"]:
            print(f"    {qid:<10} {ms / 1000:>7.1f} s")
        cause = ("retry backoff" if board["rerank_retried"]
                 else "the API tier holding a single request open, not retry backoff")
        print(f"  Cause: {cause}.")
        if p50 is not None:
            print(f"  Quote the p50 ({p50:.0f} ms) and this list, not the p95.")

    cost = board["rerank_cost_total"]
    print(f"\nbilled search units: {board['search_units_total']:.1f} over "
          f"{len(artifact)} questions")
    print(f"rerank cost: {f'${cost:.6f}' if cost is not None else 'unpriced'}"
          f"  (rate source: {RERANK_PRICE_SOURCE})")

    print("\nrerank confidence on the two rows with no gold (n=2, an observation, "
          "not a threshold):")
    for qid, score in board["unscored_top_score"].items():
        print(f"  {qid:<10} top relevance {score:.4f}" if score is not None else f"  {qid} -")

    for k in KS:
        if 0 < abs(overall[f"delta@{k}"]) <= RESOLUTION_CHUNKS:
            print(f"\nCAUTION: the k={k} delta is {overall[f'delta@{k}']:+d} chunk(s) over "
                  f"{total} references, inside the resolution ADR-0004 declared for this "
                  f"eval set.\n  Treat it as no measured difference, not as a small win.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", help="retrieve then rerank one question")
    parser.add_argument("--eval", action="store_true", help="the k-matrix over the eval set")
    parser.add_argument(
        "--refresh", action="store_true",
        help=f"re-run live and rewrite {ARTIFACT.name} (needs an API key)",
    )
    parser.add_argument("-n", type=int, default=5, help="top_n for --question")
    args = parser.parse_args()

    if not args.question and not args.eval:
        parser.error("pass --question or --eval")

    if args.question:
        try:
            retrieved = retrieve_detailed(args.question, CANDIDATES)
            result = rerank_detailed(args.question, retrieved.docs, top_n=args.n)
        except (RetrieverError, RerankError) as exc:
            print(f"failed: {exc}")
            return 1
        before = {doc.chunk_id: rank for rank, doc in enumerate(retrieved.docs, 1)}
        cost = f"${result.cost_usd:.6f}" if result.cost_usd is not None else "unpriced"
        print(
            f"{len(retrieved.docs)} candidates -> top {len(result.docs)}  "
            f"({result.search_units:.1f} search units, {cost}, "
            f"{result.latency_ms:.0f} ms rerank)\n"
        )
        print(f"{'new':>4} {'was':>4} {'relevance':>10}  citation")
        for rank, doc in enumerate(result.docs, 1):
            print(f"{rank:>4} {before[doc.chunk_id]:>4} {doc.score:>10.4f}  "
                  f"{doc.citation_label}")
        return 0

    if args.refresh:
        print(f"sweeping the eval set: retrieve {CANDIDATES} + rerank {CANDIDATES}",
              file=sys.stderr)
        artifact = sweep()
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in artifact) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {ARTIFACT}", file=sys.stderr)
    else:
        artifact = load_artifact()

    return _report(artifact)


if __name__ == "__main__":
    raise SystemExit(main())
