"""Recall measurement for the ADR-0004 dimension experiment.

Gold data is the rows of `eval/eval-questions.jsonl` that carry a non-empty
`source_chunk_ids`. Two rows correctly have none -- one `out-of-scope`, one
`unanswerable`, where the right behaviour is to retrieve nothing and refuse --
so they are excluded rather than scored as misses.

**Two metrics, reported separately.** The set averages 2.4 gold chunks per
question, so one number cannot carry both facts:

  micro recall@k  gold chunks retrieved / gold chunks total.  Answers "how much
                  of the supporting text does the retriever actually surface?"
  hit rate@k      questions with at least one gold chunk in the top k.  Answers
                  "how often does it find the thread at all?"

A high hit rate with low micro recall is a system that finds one relevant
paragraph and misses the rest of a multi-paragraph answer -- which is the
failure mode that matters for a legal citation, and it is invisible if you
report only the metric that looks better.

The honest caveat, which belongs next to any number this prints: the gold set is
40 distinct chunks, **3.6% of the corpus**, and 34 of them are AI Act. That is
sound for the 1536-vs-512 *comparison*, where both arms see exactly the same
slice and the difference is what is being measured. It is thin as an *absolute*
retrieval claim. See docs/metrics/eval-set.md.

Usage:
    python -m src.index.recall_harness              # both dims, ef_search sweep
    python -m src.index.recall_harness --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING

from src.index.embedder import DIMENSIONS, embed_texts, get_client
from src.index.pgvector_schema import connect

if TYPE_CHECKING:
    from psycopg import Connection

QUESTIONS = Path(__file__).resolve().parents[2] / "eval" / "eval-questions.jsonl"

EF_SEARCH = (40, 100, 200)


def load_labeled_queries() -> list[dict]:
    """The scoreable rows: a question and at least one gold chunk."""
    rows = [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if r.get("source_chunk_ids")]


def search(
    conn: Connection, vector: list[float], dim: int, k: int, ef_search: int,
    force_index: bool = False,
) -> list[str]:
    """Top-k chunk ids by cosine distance, with ef_search set for this query.

    The `::vector` cast is load-bearing: psycopg adapts a plain Python list to
    `double precision[]`, and while an UPDATE can infer the target type from the
    column, an ORDER BY has nothing to infer from and fails with "operator does
    not exist: vector <=> double precision[]".

    `SET` rather than `SET LOCAL` because the connection is in autocommit mode,
    where there is no surrounding transaction for LOCAL to be scoped to -- it
    would silently do nothing and every sweep row would report the default.

    `force_index` exists because at 1,108 rows the planner chooses a Seq Scan
    over the HNSW index every time, and it is right to: an exhaustive scan of the
    whole corpus costs ~6 ms. That makes the default measurement *exact* search,
    which is the honest baseline -- but it also means an ef_search sweep with the
    planner left alone compares three identical plans and reports a flat line
    that looks like a finding. Forcing the index is what actually exercises HNSW.
    """
    column = DIMENSIONS[dim]
    if force_index:
        conn.execute(f"SET hnsw.ef_search = {int(ef_search)}")
    conn.execute(f"SET enable_seqscan = {'off' if force_index else 'on'}")
    return [
        r[0] for r in conn.execute(
            f"SELECT chunk_id FROM chunks WHERE {column} IS NOT NULL "
            f"ORDER BY {column} <=> %s::vector LIMIT %s",
            (vector, k),
        ).fetchall()
    ]


def recall_at_k(
    labeled_queries: list[dict] | None = None,
    k: int = 10,
    dim: int = 1536,
    ef_search: int = 100,
    conn: Connection | None = None,
    vectors: list[list[float]] | None = None,
    force_index: bool = False,
) -> dict:
    """Micro recall@k, hit rate@k and query latency for one (dim, ef_search)."""
    queries = labeled_queries if labeled_queries is not None else load_labeled_queries()
    owned = conn is None
    conn = conn or connect()

    if vectors is None:
        vectors, _ = embed_texts(
            get_client(), [q["question"] for q in queries], dim=dim,
            input_type="search_query",
        )

    found = total = hits = 0
    latencies: list[float] = []
    per_query = []
    for query, vector in zip(queries, vectors, strict=True):
        gold = set(query["source_chunk_ids"])
        started = time.perf_counter()
        retrieved = search(conn, vector, dim, k, ef_search, force_index)
        latencies.append((time.perf_counter() - started) * 1000)

        got = gold & set(retrieved)
        found += len(got)
        total += len(gold)
        hits += bool(got)
        per_query.append({
            "id": query["id"],
            "stratum": query.get("stratum"),
            "gold": len(gold),
            "found": len(got),
            "missed": sorted(gold - set(retrieved)),
        })

    if owned:
        conn.close()

    latencies.sort()
    return {
        "dim": dim,
        "k": k,
        "ef_search": ef_search,
        "plan": "hnsw" if force_index else "exact",
        "queries": len(queries),
        "gold_total": total,
        "gold_found": found,
        "micro_recall": found / total if total else 0.0,
        "hit_rate": hits / len(queries) if queries else 0.0,
        "latency_p50_ms": round(statistics.median(latencies), 2),
        "latency_p95_ms": round(latencies[max(0, round(len(latencies) * 0.95) - 1)], 2),
        "per_query": per_query,
    }


def sweep(k: int = 10) -> list[dict]:
    """Both dimensions, exact search plus the forced-HNSW ef_search sweep.

    Queries are embedded once per dimension and reused across every plan, so the
    arms differ only in how they are searched.
    """
    queries = load_labeled_queries()
    conn = connect()
    client = get_client()

    results = []
    for dim in sorted(DIMENSIONS):
        vectors, _ = embed_texts(
            client, [q["question"] for q in queries], dim=dim, input_type="search_query"
        )
        # Exact once -- ef_search does not apply to a Seq Scan, so running it
        # three times would print three identical rows dressed as a sweep.
        results.append(
            recall_at_k(queries, k=k, dim=dim, ef_search=0, conn=conn,
                        vectors=vectors, force_index=False)
        )
        for ef in EF_SEARCH:
            results.append(
                recall_at_k(queries, k=k, dim=dim, ef_search=ef, conn=conn,
                            vectors=vectors, force_index=True)
            )
    conn.close()
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure recall@k at both dimensions.")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    results = sweep(k=args.k)
    if args.json:
        print(json.dumps(results, indent=2))
        return

    first = results[0]
    print(
        f"{first['queries']} labeled queries, {first['gold_total']} gold chunk "
        f"references, k={args.k}\n"
    )
    print(f"{'dim':>5} {'plan':>6} {'ef':>5} {'micro recall':>13} {'hit rate':>9} "
          f"{'p50 ms':>8} {'p95 ms':>8}")
    for r in results:
        ef = str(r["ef_search"]) if r["plan"] == "hnsw" else "-"
        print(
            f"{r['dim']:>5} {r['plan']:>6} {ef:>5} "
            f"{r['gold_found']:>3}/{r['gold_total']:<3} {r['micro_recall']:>6.1%} "
            f"{r['hit_rate']:>8.1%} {r['latency_p50_ms']:>8.2f} {r['latency_p95_ms']:>8.2f}"
        )

    # Recall per stratum, which is the number that actually says something. A
    # single corpus-wide figure averages "find one paragraph" together with
    # "find all eleven" and hides that they behave nothing alike.
    exact = {r["dim"]: r for r in results if r["plan"] == "exact"}
    strata: dict[str, list[int]] = {}
    for q in exact[1536]["per_query"]:
        acc = strata.setdefault(q["stratum"], [0, 0])
        acc[0] += q["found"]
        acc[1] += q["gold"]
    print("\nmicro recall by stratum (exact, 1536):")
    for stratum, (found, gold) in sorted(strata.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        print(f"  {stratum:<17} {found:>2}/{gold:<3} {found / gold:>6.0%}")

    # The ADR-0004 decision rule, computed rather than eyeballed -- and reported
    # in whole gold chunks, because a percentage over 51 references makes a
    # one-chunk difference look like a measurement.
    best = {dim: max(r["gold_found"] for r in results if r["dim"] == dim)
            for dim in sorted(DIMENSIONS)}
    total = exact[1536]["gold_total"]
    delta_chunks = best[1536] - best[512]
    delta = delta_chunks / total
    print(
        f"\nbest micro recall -- 1536: {best[1536]}/{total} ({best[1536] / total:.1%}), "
        f"512: {best[512]}/{total} ({best[512] / total:.1%})"
    )
    print(f"difference: {delta_chunks} gold chunk(s), {delta:+.1%}")
    print(
        "ADR-0004 rule (adopt 512 if it loses <2% recall): "
        + ("ADOPT 512" if delta < 0.02 else "KEEP 1536")
    )
    if abs(delta_chunks) <= 2:
        print(
            f"  CAUTION: {abs(delta_chunks)} chunk(s) over {len(exact[1536]['per_query'])} "
            "queries is within the resolution of this eval set. Treat the arms as "
            "indistinguishable on recall and decide on storage and latency, which "
            "differ by multiples rather than by one row."
        )

    missed = {
        chunk_id
        for q in exact[1536]["per_query"] for chunk_id in q["missed"]
    }
    if missed:
        print(f"\ngold chunks missed by exact search at 1536: {len(missed)}")
        for chunk_id in sorted(missed):
            print(f"  {chunk_id}")


if __name__ == "__main__":
    main()
