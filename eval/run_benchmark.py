"""Run three systems over the full eval set and report per-stratum accuracy.

Systems:
  (a) `vector`         vector-only          -- the raw HNSW draw, no reranker
  (b) `rerank`         vector + Rerank 3.5  -- the same draw, cross-encoder reordered
  (c) `hybrid`         full hybrid          -- router-chosen graph / vector / both
  (d) `hybrid-oracle`  hybrid, gold route   -- a CEILING, not a deployable system

The fourth arm was added 2026-08-15, after the router was re-measured at 70/99 on
the expanded eval set (it scored 21/22 on the 23-row set ADR-0012 published, which
it was overfitted to). Without it a weak `hybrid` cell confounds "the graph path
could not answer" with "the router never asked it to". See the note above
`ORACLE_SYSTEM`.

Also reports latency p50/p95, cost/query, and one-time ingestion cost. This is
the module the README benchmark table is computed from.

THE ACCURACY PASS AND THE COST PASS ARE DIFFERENT RUNS, ON PURPOSE.

`--refresh` replays committed passages: `replayed_passages(field=...)` reads the
orderings out of `eval/rerank-eval.jsonl` and fetches text from Postgres, so all
three systems see byte-identical passages and a difference between them is a
difference in the system rather than a different vector draw. That is what makes
the accuracy columns comparable, and it makes the vector half cost $0.00.

It also makes latency and cost meaningless: a replayed row never pays the embed
or rerank round trip. So `--refresh --live` is a second pass that spends the
whole request, and **the p95 and $/query columns come from it while the accuracy
columns come from the replay**. Rows carry `mode` and `scoreboard()` keeps them
apart. This is the same split `src/api/ask_eval.py` already draws between the
pipeline artifact and the interface artifact, for the same reason.

REPORTING RULES, PRE-REGISTERED HERE BEFORE ANY MONEY WAS SPENT.

1. **Per-system p95 is published; per-stratum p95 is not.** At 100 rows a system
   has enough observations for a tail figure. A stratum has 5 to 20, and a p95
   over 5 is the maximum wearing a percentile's name. `ask_eval.py:20-28` made
   the same refusal at 23 rows and `test_no_per_stratum_p95_is_published` makes
   it structural rather than habitual.
2. **The systems do not share a denominator unless one is imposed.** Each drops
   its own `MAX_TOKENS` and errored rows and they are not the same rows, so three
   per-system accuracies are three different populations. `common` is the set
   every system scored; it is smaller and weaker than the per-system number and
   it is the one that is comparable. Same defect `answer_path.scoreboard()`
   solved for budget arms and `template_selector._report` once shipped as
   "Ceiling 10 of 9".
3. **The three refusal strata are reported separately and never averaged.**
   `out-of-scope` and `unanswerable` must cite nothing; `hard-negative` must
   cite. One "refusal rate" over the three would score a system that got all
   three right the same as one that got one right in three different ways.
   `docs/metrics/eval-set.md:58-71` argues it; this enforces it.
4. **`expected_fail` rows go in their own bucket** -- counted as neither passes
   nor system failures.

    python -m eval.run_benchmark --eval                        # the table, from the artifact
    python -m eval.run_benchmark --refresh --system rerank     # one system, replayed
    python -m eval.run_benchmark --refresh --system hybrid --live   # one system, live
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

QUESTIONS = ROOT / "eval" / "eval-questions.jsonl"

# Beside the eval set it measures, and tracked, for the reason `router.py:66`
# gives: the tests and the metrics doc read their numbers out of it, and both
# must work with no API key and no spend. Keyed by `system` the way
# `answer-eval.jsonl` is keyed by `budget`.
ARTIFACT = ROOT / "eval" / "benchmark.jsonl"

# Rows carrying `expected_fail` are known-red for a recorded reason -- currently
# 3h-002, where the extractor emits PERMITS on a derogation that requires
# EXEMPT_FROM. They are reported in their own bucket: counting them as passes
# would silence the canary, and counting them as system failures would blame the
# retrieval stack for an extraction gap. See docs/adr/adr-0007-lawfulbasis-permits.md.
EXPECTED_FAIL_BUCKET = "expected_fail"

# How each system is assembled. `field` selects which committed ordering to
# replay; `route` of None means "ask the adopted router", anything else forces it.
#
# `vector` and `rerank` are both forced to the vector route because they ARE the
# vector baselines -- letting the router send a baseline row to `graph` would
# make the comparison a comparison of routers.
# `route: None` means "ask the adopted router"; `route: "gold"` means "replay the
# eval set's hand-verified label"; anything else forces that route.
SYSTEMS: dict[str, dict[str, Any]] = {
    "vector": {"field": "retrieved", "route": "vector", "reranks": False,
               "label": "Vector-only"},
    "rerank": {"field": "reranked", "route": "vector", "reranks": True,
               "label": "Vector + Rerank 3.5"},
    "hybrid": {"field": "reranked", "route": None, "reranks": True,
               "label": "Hybrid (graph+vector)"},
    "hybrid-oracle": {"field": "reranked", "route": "gold", "reranks": True,
                      "label": "Hybrid, gold route"},
    # The lexical-union arm. Identical to `rerank` in every respect except the
    # candidate pool the cross-encoder ranks: vector top-50 UNION BM25 top-50
    # UNION Postgres-FTS top-50, which lifts gold-in-pool from 157/203 to
    # 176/203. Forced to the vector route for the same reason `rerank` is --
    # this measures a pool change, and letting the router move rows would make
    # it measure a router.
    "rerank-pool": {"field": "pool_reranked", "route": "vector", "reranks": True,
                    "label": "Vector + BM25/FTS pool + Rerank 3.5"},
    # The enumeration arm: `rerank` plus, on questions the detector flags, every
    # limb of one provision read in statutory order. `enumerates` is what makes
    # the difference; the field and route are `rerank`'s, so the pair isolates
    # the enumeration and nothing else.
    "rerank-enum": {"field": "reranked", "route": "vector", "reranks": True,
                    "enumerates": True,
                    "label": "Vector + Rerank 3.5 + enumeration"},
}

SYSTEM_ORDER = ("vector", "rerank", "rerank-pool", "rerank-enum", "hybrid",
                "hybrid-oracle")

# THE FOURTH ARM EXISTS BECAUSE THE ROUTER AND THE HYBRID ARE TWO DIFFERENT
# THINGS AND ONE NUMBER CANNOT MEASURE BOTH.
#
# Re-measured 2026-08-15 on the 100-row set, the adopted rules router scores
# 70/99 -- down from the 21/22 ADR-0012 published on 23 rows, which was
# overfitted to them. The failure is systematic rather than noisy: `both` is
# reachable only through the R4-second-ask regex, and 24 rows whose gold is
# `both` are routed `vector` because they phrase the second hop in a way that
# regex does not match ("Which GDPR fine tier applies?").
#
# So a `hybrid` row that scores badly has two possible causes -- the graph path
# could not answer, or the router never asked it to -- and the arm as specified
# confounds them. `hybrid-oracle` replays the hand-verified gold route, which
# makes the pair a decomposition:
#
#   hybrid-oracle        what the hybrid can do when asked correctly
#   hybrid               what the deployed system does today
#   the gap between them THE ROUTER'S COST, as a number rather than a caveat
#
# `hybrid-oracle` is NOT the headline. It uses labels a live request does not
# have, so it is a ceiling and is labelled one; the honest per-query number is
# `hybrid`.
ORACLE_SYSTEM = "hybrid-oracle"
DEPLOYED_SYSTEM = "hybrid"

# The order the table prints, and the order roadmap S5.3 lists them in.
STRATUM_ORDER = (
    "single-hop", "two-hop", "three-hop", "cross-regulation", "aggregation",
    "out-of-scope", "unanswerable", "hard-negative",
)

# The three refusal modes, kept apart. See reporting rule 3.
REFUSAL_STRATA = ("out-of-scope", "unanswerable", "hard-negative")

# Verdicts that count in the numerator of an accuracy cell. `correct_refusal` is
# the correct outcome on a refusal row, so it is a pass there and cannot occur
# elsewhere; `partially_correct` is reported beside the cell, never inside it.
PASSING = ("correct", "correct_refusal")

# One-time ingestion cost, from docs/metrics/extraction-cost-and-findings.md.
# Reported beside the table per roadmap S6 Phase 5.3 -- a per-query cost column
# with no build cost next to it understates what the system cost to stand up.
INGESTION_COST_USD = 24.0


# The live pass exists only to observe latency, and latency does not need every
# row. `--sample N` takes a deterministic stratified slice, using the same
# even-spacing rule as `judge.holdout` so the subset is a property of the eval set
# rather than of when it was run.
def live_sample(rows: list[dict], n: int) -> list[dict]:
    """A stratified, deterministic subset of `n` rows for the live pass."""
    import collections

    if n >= len(rows):
        return rows
    by_stratum: dict[str, list[dict]] = collections.defaultdict(list)
    for row in sorted(rows, key=lambda r: r["id"]):
        by_stratum[row["stratum"]].append(row)

    share = n / len(rows)
    picked: list[dict] = []
    for stratum in sorted(by_stratum):
        group = by_stratum[stratum]
        take = max(1, round(len(group) * share))
        step = len(group) / take
        picked.extend(group[int(i * step)] for i in range(take))
    return sorted(picked, key=lambda r: r["id"])


class BenchmarkError(RuntimeError):
    """A system could not be swept. Recorded per row, never fatal to the run."""


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]


def bucket_of(row: dict) -> str | None:
    """Which reporting bucket a row belongs to, or None for the normal path."""
    if row.get("expected_fail", {}).get("reason", "").strip():
        return EXPECTED_FAIL_BUCKET
    return None


# --------------------------------------------------------------------------
# The sweep -- impure. Needs containers, and a key for the generation half.
# --------------------------------------------------------------------------

def sweep(
    rows: list[dict],
    system: str,
    live: bool = False,
    driver: Any | None = None,
    conn: Any | None = None,
    client: Any | None = None,
    grade: bool = True,
    run_tag: str = "",
    only_strata: tuple[str, ...] | None = None,
) -> list[dict]:
    """One system over every eval row. The only part that spends.

    `live=False` (the default) replays committed passages so the systems are
    comparable on accuracy; `live=True` pays the whole request so the latency and
    cost columns measure something real. See the module docstring.

    One system per invocation, for the reason `answer_path.sweep` records: ~100
    chat calls against a rate-limited key is long enough that a single sweep can
    exhaust the tenacity budget, and an arm that dies halfway should not take the
    other two down. `attempts` is recorded per row so a rate-limited row is
    visible in the artifact rather than inferred from a bad score.

    `run_tag` NAMES A REPETITION OF AN OTHERWISE IDENTICAL SWEEP.

    Every other field in a row says what was *configured*. Two runs of the same
    system on the same day are identical in all of them, so without a tag the
    artifact cannot express "these are two samples of one configuration" and the
    scoreboard's `system` grouping would silently pool them into one 200-row
    arm. The tag is recorded and is otherwise inert: `scoreboard()` does not read
    it, so a tagged sweep never joins the published table by accident.

    `only_strata` restricts the sweep to named strata. A stratum-local re-run is
    not a benchmark and must not be scored as one -- it is paired against the
    same rows of a previous artifact and reported on its own.
    """
    if system not in SYSTEMS:
        raise BenchmarkError(f"{system!r} is not a system; have {sorted(SYSTEMS)}")
    spec = SYSTEMS[system]

    from src.answer.answer_path import (
        AnswerPathError,
        answer,
        replayed_passages,
    )
    from src.answer.generate import GenerateError

    # LIVE MODE CANNOT JUST HAND THE VECTOR BASELINE TO `answer()`.
    #
    # `answer()` hardwires retrieve->rerank in its vector branch
    # (answer_path.py:294-312), so a live `vector` sweep would pay for and use a
    # rerank the baseline is defined as not having -- making the vector-only
    # system identical to the rerank system in exactly the two columns live mode
    # exists to produce. So the vector baseline does its own live retrieval and
    # injects the un-reranked top-5. It still pays the embed round trip, which is
    # the honest cost of a vector-only system; it does not pay the rerank.
    def live_passages(question: str) -> list | None:
        if not live or spec["field"] != "retrieved":
            return None
        from src.answer.answer_path import PASSAGE_TOP_N
        from src.query.reranker import CANDIDATES
        from src.query.retriever import DIM, retrieve_detailed

        got = retrieve_detailed(question, CANDIDATES, dim=DIM, conn=conn, client=client)
        return got.docs[:PASSAGE_TOP_N]

    owned_driver = driver is None
    if driver is None:
        from src.ingest.graph_writer import connect

        driver = connect()
    owned_conn = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect as pg_connect

        conn = pg_connect()

    passages = {} if live else replayed_passages(conn, field=spec["field"])

    # THE ENUMERATION ARM IS BUILT HERE RATHER THAN INSIDE `answer()`, because
    # this is a REPLAY: `passages=` short-circuits the vector branch entirely, so
    # the enumeration `answer()` would have applied never runs. Building it here
    # keeps the arm honest -- it replays exactly the passages a live enumerating
    # request would have assembled, from the same committed ordering, with no
    # retrieval and no spend.
    if not live and spec.get("enumerates"):
        from src.answer.answer_path import ENUM_VOTE_N, _with_enumeration
        from src.query.router import _ENUMERATE_PROVISION, enumeration_target

        votes = replayed_passages(conn, field=spec["field"], top_n=ENUM_VOTE_N)
        by_id = {r["id"]: r for r in rows}
        for qid, docs in list(passages.items()):
            question = (by_id.get(qid) or {}).get("question")
            if not question or not _ENUMERATE_PROVISION.search(question):
                continue
            passages[qid] = _with_enumeration(
                docs, votes.get(qid, docs), enumeration_target(question), conn=conn
            )

    if only_strata:
        rows = [r for r in rows if r["stratum"] in only_strata]

    out: list[dict] = []
    try:
        for row in rows:
            started = time.perf_counter()
            record: dict[str, Any] = {
                "system": system,
                "mode": "live" if live else "replay",
                "run_tag": run_tag,
                "id": row["id"],
                "stratum": row["stratum"],
                "gold_route": row.get("route"),
                "gold": row["source_chunk_ids"],
                "must_cite": row.get("must_cite"),
                "bucket": bucket_of(row),
            }
            try:
                result = answer(
                    row["question"],
                    route=(row["route"] if spec["route"] == "gold" else spec["route"]),
                    driver=driver,
                    conn=conn,
                    client=client,
                    passages=(
                        live_passages(row["question"]) if live
                        else passages.get(row["id"])
                    ),
                )
            except (AnswerPathError, GenerateError) as exc:
                # The system under measurement must not take the sweep down with
                # it; the failure is recorded as this row's result. Same call
                # `answer_path.sweep` makes at :693.
                record.update({
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                })
                out.append(record)
                print(f"  {row['id']:<9} {system:<7} ERROR {type(exc).__name__}", file=sys.stderr)
                continue

            cited = sorted({c.chunk_id for c in result.citations})
            record.update({
                "route": result.route,
                "answer": result.answer,
                "citations": [c.model_dump() for c in result.citations],
                "cited_chunk_ids": cited,
                "gold_cited": sorted(set(row["source_chunk_ids"]) & set(cited)),
                "documents_sent": result.documents_sent,
                "graph_sent": result.graph_sent,
                "passage_sent": result.passage_sent,
                "finish_reason": result.finish_reason,
                "attempts": result.attempts,
                "regenerated": result.regenerated,
                # E1's instrument. See `sweep(run_tag=...)`.
                "request_sha": result.request_sha,
                "cost_usd": result.cost_usd,
                "latency_ms": round(result.latency_ms, 2),
                "error": None,
            })

            if grade:
                from eval.judge import JudgeError, judge

                try:
                    verdict = judge(row, result.answer, result.citations, client=client)
                    record.update({
                        "verdict": verdict.verdict,
                        "judge_reason": verdict.reason,
                        "judge_defect": verdict.defect,
                        "judge_capped_from": verdict.capped_from,
                        "judge_cost_usd": verdict.cost_usd,
                        "judge_attempts": verdict.attempts,
                    })
                except JudgeError as exc:
                    # An ungraded row is recorded as ungraded, never as wrong. A
                    # grading failure is a fact about the grader and scoring it
                    # against the system would be the wrong attribution.
                    record.update({"verdict": None, "judge_error": str(exc)})

            print(
                f"  {row['id']:<9} {system:<7} {record.get('route','-'):<7} "
                f"{record.get('verdict') or 'ungraded':<18} "
                f"${record.get('cost_usd') or 0:.4f} {record['latency_ms']:>8.0f} ms",
                file=sys.stderr,
            )
            out.append(record)
    finally:
        if owned_driver:
            driver.close()
        if owned_conn:
            conn.close()
    return out


# --------------------------------------------------------------------------
# Measurement -- PURE. No DB, no key, no spend.
# --------------------------------------------------------------------------

# $/QUERY IS COMPUTED, NOT SAMPLED, AND THAT IS AN IMPROVEMENT RATHER THAN A
# CONCESSION.
#
# A live row's `cost_usd` is one observation of a quantity that is exactly
# determined by things already recorded: the generation tokens (in this
# artifact's replay rows) plus the query embedding and, for the systems that
# rerank, one rerank search unit (both in `eval/rerank-eval.jsonl`). Deriving it
# gives the cost over ALL 100 rows instead of over whichever subset the live pass
# could afford, and it cannot drift from the price table the way a sampled mean
# can.
#
# The replay row's own `cost_usd` is generation only: passages are injected, so no
# embed or rerank call is made, and the graph path is Cypher plus a local entity
# index and costs nothing. `retrieval_costs()` supplies the rest.
def retrieval_costs(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Per-question embed and rerank cost, from the committed rerank artifact.

    Pure: one file read, no network. Keyed by eval row id.
    """
    path = path or (ROOT / "eval" / "rerank-eval.jsonl")
    out: dict[str, dict[str, float]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[row["id"]] = {
            "embed": row.get("embed_cost_usd") or 0.0,
            "rerank": row.get("rerank_cost_usd") or 0.0,
        }
    return out


def analytic_cost(row: dict, retrieval: dict[str, dict[str, float]]) -> float | None:
    """What one live request for this row on this system would cost.

    None propagates rather than becoming zero, for the reason `config.price_of`
    gives: a route containing an unpriced component has an unknown total, and a
    number that is silently short is worse than an admitted gap.
    """
    generation = row.get("cost_usd")
    if generation is None:
        return None
    parts = retrieval.get(row["id"])
    if parts is None:
        return None
    spec = SYSTEMS.get(row["system"], {})
    total = generation + parts["embed"]
    if spec.get("reranks"):
        total += parts["rerank"]
    return total


def load_artifact(path: Path | None = None) -> list[dict]:
    path = path or ARTIFACT
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist. Run --refresh --system <name> with the "
            f"containers up and an API key."
        )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _p(values: list[float], q: float) -> float | None:
    """The q-th percentile by nearest rank. None on an empty list.

    Nearest rank rather than interpolation, for the reason `ask_eval._p` gives:
    an interpolated p95 invents a value no request ever took, and the point of
    the column is to report what was observed.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * len(ordered) + 0.5) - 1))
    return ordered[index]


def scoreboard(artifact: list[dict],
               retrieval: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Every published number, recomputed from the artifact alone.

    Pure -- no database, no API key, no network. That is what lets the tests and
    `docs/metrics/benchmark.md` quote the same figures and what makes `--eval`
    reproducible on a laptop with the containers down.

    Accuracy comes from `mode == "replay"` rows and latency/cost from
    `mode == "live"` rows. Mixing them would put a $0.00 replayed vector row into
    the cost column. See reporting rule 1 in the module docstring.
    """
    retrieval = retrieval_costs() if retrieval is None else retrieval
    # TAGGED ROWS ARE NOT THE BENCHMARK. A repeat sweep (`--run-tag`) or a
    # stratum-local re-run appends rows that are identical in `system` and
    # `mode` to the published ones, so grouping on those two alone would pool a
    # repetition into the arm it was measuring and double its denominator. The
    # published table is the untagged rows and nothing else; the repeats are
    # read by their own reporters.
    artifact = [r for r in artifact if not r.get("run_tag")]
    replay = [r for r in artifact if r.get("mode") == "replay"]
    live = [r for r in artifact if r.get("mode") == "live"]
    systems = [s for s in SYSTEM_ORDER if any(r["system"] == s for r in artifact)]

    def scorable(rows: list[dict], system: str) -> list[dict]:
        """Rows this system actually scored: no error, not truncated, not a canary."""
        return [
            r for r in rows
            if r["system"] == system
            and not r.get("error")
            and r.get("finish_reason") != "MAX_TOKENS"
            and r.get("bucket") != EXPECTED_FAIL_BUCKET
            and r.get("verdict")
        ]

    # The comparable denominator: rows EVERY system scored. See reporting rule 2.
    scored_ids = {s: {r["id"] for r in scorable(replay, s)} for s in systems}
    common_ids = set.intersection(*scored_ids.values()) if scored_ids else set()

    board: dict[str, Any] = {
        "systems": systems,
        "labels": {s: SYSTEMS[s]["label"] for s in systems},
        "rows": len(artifact),
        "replay_rows": len(replay),
        "live_rows": len(live),
        "ingestion_cost_usd": INGESTION_COST_USD,
        "common": {
            "ids": sorted(common_ids),
            "n": len(common_ids),
            "excluded": sorted({r["id"] for r in replay} - common_ids),
        },
        "strata": list(STRATUM_ORDER),
        "refusal_strata": list(REFUSAL_STRATA),
    }

    # THE ROUTER'S COST, decomposed. Only computable when both hybrid arms ran,
    # and computed over the rows BOTH scored so the difference is not partly a
    # difference in denominator.
    if DEPLOYED_SYSTEM in systems and ORACLE_SYSTEM in systems:
        deployed = {r["id"]: r for r in scorable(replay, DEPLOYED_SYSTEM)}
        oracle = {r["id"]: r for r in scorable(replay, ORACLE_SYSTEM)}
        shared = sorted(set(deployed) & set(oracle))
        lost = [
            rid for rid in shared
            if oracle[rid]["verdict"] in PASSING and deployed[rid]["verdict"] not in PASSING
        ]
        gained = [
            rid for rid in shared
            if deployed[rid]["verdict"] in PASSING and oracle[rid]["verdict"] not in PASSING
        ]
        board["router_cost"] = {
            "n": len(shared),
            "deployed_pass": sum(1 for r in shared if deployed[r]["verdict"] in PASSING),
            "oracle_pass": sum(1 for r in shared if oracle[r]["verdict"] in PASSING),
            "lost_to_routing": lost,
            "gained_despite_routing": gained,
            # Rows the router sent somewhere other than the gold label. This is
            # the population the cost can possibly come from; `lost_to_routing`
            # is the part of it that actually changed an answer.
            "misrouted": sorted(
                rid for rid in shared
                if deployed[rid].get("route") != oracle[rid].get("route")
            ),
        }

    for system in systems:
        rows = scorable(replay, system)
        by_id = {r["id"]: r for r in rows}

        per_stratum: dict[str, dict[str, Any]] = {}
        for stratum in STRATUM_ORDER:
            cell = [r for r in rows if r["stratum"] == stratum]
            if not cell:
                continue
            per_stratum[stratum] = {
                "n": len(cell),
                "pass": sum(1 for r in cell if r["verdict"] in PASSING),
                "partial": sum(1 for r in cell if r["verdict"] == "partially_correct"),
                "wrong": sum(1 for r in cell if r["verdict"] == "wrong"),
                "ids_wrong": sorted(r["id"] for r in cell if r["verdict"] == "wrong"),
            }

        live_rows = [
            r for r in live
            if r["system"] == system and not r.get("error") and r.get("latency_ms")
        ]
        latencies = [r["latency_ms"] for r in live_rows if r.get("attempts", 1) == 1]
        # Sampled, kept only as a cross-check on the derived figure below.
        costs = [r["cost_usd"] for r in live_rows if r.get("cost_usd") is not None]
        derived = [c for c in (analytic_cost(r, retrieval) for r in rows) if c is not None]

        board[system] = {
            "scored": len(rows),
            "errors": sorted(r["id"] for r in replay
                             if r["system"] == system and r.get("error")),
            "truncated": sorted(r["id"] for r in replay
                                if r["system"] == system
                                and r.get("finish_reason") == "MAX_TOKENS"),
            "ungraded": sorted(r["id"] for r in replay
                               if r["system"] == system
                               and not r.get("error") and not r.get("verdict")),
            "per_stratum": per_stratum,
            "pass_total": sum(1 for r in rows if r["verdict"] in PASSING),
            # The comparable pair: same rows for every system.
            "common_pass": sum(1 for r in rows
                               if r["id"] in common_ids and r["verdict"] in PASSING),
            "common_n": sum(1 for r in rows if r["id"] in common_ids),
            # Gold-chunk retention, carried beside accuracy rather than instead
            # of it. Every pre-Phase-5 number in this repo is this quantity, and
            # keeping it visible is what makes the two comparable.
            "gold_total": sum(len(r["gold"]) for r in rows),
            "gold_cited": sum(len(r.get("gold_cited", [])) for r in rows),
            # LIVE ONLY. A replayed row never paid the embed+rerank round trip.
            "latency_p50": statistics.median(latencies) if latencies else None,
            "latency_p95": _p(latencies, 0.95),
            "latency_max": max(latencies, default=None) if latencies else None,
            # THE PUBLISHED $/query. Derived over every scored row.
            "cost_mean": statistics.mean(derived) if derived else None,
            "cost_total": sum(derived) if derived else None,
            "cost_n": len(derived),
            "cost_is_derived": True,
            # The live observations, retained only so the two can be compared.
            "cost_mean_sampled": statistics.mean(costs) if costs else None,
            "live_n": len(live_rows),
            "latency_n": len(latencies),
            "unpriced": len(rows) - len(derived),
            # The three refusal modes, individually. NEVER averaged.
            "refusal": {
                stratum: {
                    "n": per_stratum.get(stratum, {}).get("n", 0),
                    "pass": per_stratum.get(stratum, {}).get("pass", 0),
                    "cited_when_it_should_not": sorted(
                        r["id"] for r in rows
                        if r["stratum"] == stratum
                        and stratum in ("out-of-scope", "unanswerable")
                        and r.get("cited_chunk_ids")
                    ),
                    "uncited_when_it_should_cite": sorted(
                        r["id"] for r in rows
                        if r["stratum"] == stratum
                        and stratum == "hard-negative"
                        and not r.get("cited_chunk_ids")
                    ),
                }
                for stratum in REFUSAL_STRATA
            },
            "expected_fail": {
                r["id"]: r.get("verdict")
                for r in replay
                if r["system"] == system and r.get("bucket") == EXPECTED_FAIL_BUCKET
            },
            "_by_id": by_id,
        }
    return board


def markdown_table(board: dict[str, Any]) -> str:
    """The README table, generated -- never transcribed.

    Step 5's recurrence was a published figure that had been computed once by
    hand and copied into the repo, so a test ended up comparing a constant to
    itself (`failure-notes.md`, "Verified once by hand, never encoded"). The
    README table is emitted from `scoreboard()` here so the number in the
    README and the number in the artifact cannot drift apart.

    Cells are `n/N`, not bare percentages: at 5 rows a refusal stratum has no
    meaningful percentage and rounding one to "80%" hides the denominator.

    THE HEADLINE COLUMN IS `common`, NOT `pass_total`.

    `scoreboard()` has computed both since the arm was added, and this function
    published the wrong one. Every per-system denominator is that system's own:
    `scorable()` drops each arm's errored and MAX_TOKENS rows, those are
    different rows in each arm, and the drop is not random -- 16 of the 20 rows
    dropped across the four arms were `wrong` or `partially_correct`, and they
    cluster in `aggregation`, whose long answers are the ones that truncate. So
    the per-system column silently deletes each arm's own hardest failures and
    then invites a reader to compare the results across arms.

    Both are now printed. `Overall` is the comparable figure over the rows every
    arm scored; `Own` is retained beside it, labelled, because dropping it would
    hide that the arms disagree about which rows are scorable at all -- and that
    disagreement is itself a finding. The per-stratum cells remain per-system
    denominators and are marked with a dagger for the same reason.
    """
    headline = ("single-hop", "two-hop", "three-hop", "cross-regulation", "aggregation")
    common_n = board["common"]["n"]
    lines = [
        f"| System | Overall (n={common_n}) | Own^ | Single-hop^ | Two-hop^ | "
        f"Three-hop^ | Cross-reg^ | Aggregation^ | Refusal*^ | p95 latency | $/query |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for system in board["systems"]:
        cell = board[system]
        cells = []
        for stratum in headline:
            got = cell["per_stratum"].get(stratum)
            cells.append(f"{got['pass']}/{got['n']}" if got else "-")
        refusal_pass = sum(v["pass"] for v in cell["refusal"].values())
        refusal_n = sum(v["n"] for v in cell["refusal"].values())
        p95 = (
            f"{cell['latency_p95']/1000:.1f} s (n={cell['latency_n']})"
            if cell["latency_p95"] else "-"
        )
        cost = f"${cell['cost_mean']:.4f}" if cell["cost_mean"] is not None else "-"
        if system == ORACLE_SYSTEM:
            # A ceiling that uses gold route labels is not a deployable system, so
            # publishing its latency or per-query cost would be a claim about
            # something nobody can run. Accuracy only.
            p95, cost = "-", "-"
        label = board["labels"][system]
        if system == DEPLOYED_SYSTEM:
            label = f"**{label}**"
        elif system == ORACLE_SYSTEM:
            # Marked as a ceiling in the table itself. It uses gold route labels
            # that a live request does not have, and an unmarked row would read
            # as a deployable system.
            label = f"{label} [ceiling]"
        common = f"**{cell['common_pass']}/{common_n}**"
        own = f"{cell['pass_total']}/{cell['scored']}"
        lines.append(
            f"| {label} | {common} | {own} | " + " | ".join(cells)
            + f" | {refusal_pass}/{refusal_n} | {p95} | {cost} |"
        )

    if ORACLE_SYSTEM in board["systems"]:
        cost = board.get("router_cost")
        gap = (
            f" The gap to it is the router's cost: {cost['oracle_pass'] - cost['deployed_pass']} "
            f"answers over {cost['n']} rows."
            if cost else ""
        )
        lines += [
            "",
            f"[ceiling] Not a deployable system: it replays the eval set's "
            f"hand-verified route labels, which a live request does not have."
            f"{gap} The adopted rules router scores 70/99 on this set.",
        ]

    excluded = board["common"]["excluded"]
    lines += [
        "",
        "^**Not comparable across systems.** These columns use each system's own "
        "denominator. `scorable()` drops that system's errored, MAX_TOKENS and "
        "canary rows, and they are *different rows in each system*, so the "
        "denominators differ ("
        + ", ".join(
            f"{board['labels'][s]} {board[s]['scored']}" for s in board["systems"]
        )
        + f"). The drop is quality-correlated -- truncation hits the longest "
        f"answers, which are concentrated in `aggregation` -- so each of these "
        f"cells is biased upward by an unknown amount. **Overall** is the only "
        f"column that compares: it is the {board['common']['n']} rows every "
        f"system scored"
        + (f", excluding {', '.join(excluded)}." if excluded else "."),
        "",
        "\\*Refusal is three behaviours with three different correct outputs, and is "
        "never averaged into one number. Broken out:",
        "",
        "| System | Out-of-scope (cite nothing) | Unanswerable (cite nothing) | Hard-negative (must cite) |",
        "|---|---|---|---|",
    ]
    for system in board["systems"]:
        cell = board[system]["refusal"]
        lines.append(
            f"| {board['labels'][system]} | "
            + " | ".join(
                f"{cell[s]['pass']}/{cell[s]['n']}" for s in REFUSAL_STRATA
            )
            + " |"
        )
    return "\n".join(lines)


def _report(board: dict[str, Any]) -> int:
    if not board["systems"]:
        print("\nNo system has been swept yet. Run --refresh --system <name>.")
        return 0

    print(f"\n{board['replay_rows']} replayed rows, {board['live_rows']} live rows.")
    print("Accuracy from the replayed pass; latency and cost from the live pass.\n")
    print(markdown_table(board))

    common = board["common"]
    print(f"\nCOMMON DENOMINATOR -- the {common['n']} rows every system scored.")
    print("-" * 78)
    for system in board["systems"]:
        cell = board[system]
        print(f"  {board['labels'][system]:<24} {cell['common_pass']:>3} of {common['n']:<4}"
              f"   (own denominator: {cell['pass_total']} of {cell['scored']})")
    print(
        "\n  The per-system column is NOT comparable across systems: each drops its\n"
        "  own errored and MAX_TOKENS rows and they are different rows. COMMON is."
    )
    if common["excluded"]:
        print(f"  Excluded from COMMON: {', '.join(common['excluded'])}")

    print("\nGOLD-CHUNK RETENTION, beside accuracy rather than instead of it")
    print("-" * 78)
    for system in board["systems"]:
        cell = board[system]
        total = cell["gold_total"] or 1
        print(f"  {board['labels'][system]:<24} {cell['gold_cited']:>3} of {cell['gold_total']:<4} "
              f"{cell['gold_cited']/total:>6.1%}")
    print(
        "\n  This is the quantity every pre-Phase-5 number in this repo reports.\n"
        "  A system whose retention rises and whose accuracy does not has found\n"
        "  the passages and failed to use them -- which is the finding, not a bug."
    )

    cost = board.get("router_cost")
    if cost:
        print("\nTHE ROUTER'S COST -- what the adopted router gives up vs the gold route")
        print("-" * 78)
        print(f"  over the {cost['n']} rows both hybrid arms scored:")
        print(f"    hybrid (adopted router)  {cost['deployed_pass']:>3} passing")
        print(f"    hybrid (gold route)      {cost['oracle_pass']:>3} passing")
        print(f"    difference               {cost['oracle_pass'] - cost['deployed_pass']:>+3}")
        print(f"  misrouted rows: {len(cost['misrouted'])}"
              f"  -- of which {len(cost['lost_to_routing'])} changed the answer")
        if cost["lost_to_routing"]:
            print(f"    lost to routing:  {', '.join(cost['lost_to_routing'])}")
        if cost["gained_despite_routing"]:
            print(f"    passed anyway on the wrong route: "
                  f"{', '.join(cost['gained_despite_routing'])}")
        print(
            "\n  A misrouted row that still passes means the vector path happened to\n"
            "  carry the answer -- the route was wrong and it did not matter. Only\n"
            "  `lost to routing` is the router costing the system an answer."
        )

    print("\nREFUSAL -- three modes, three correct behaviours, never averaged")
    print("-" * 78)
    for system in board["systems"]:
        print(f"  {board['labels'][system]}")
        for stratum in REFUSAL_STRATA:
            cell = board[system]["refusal"][stratum]
            want = "must cite" if stratum == "hard-negative" else "cite nothing"
            print(f"    {stratum:<15} {cell['pass']:>2} of {cell['n']:<3} ({want})")
            if cell["cited_when_it_should_not"]:
                print(f"        cited anyway: {', '.join(cell['cited_when_it_should_not'])}")
            if cell["uncited_when_it_should_cite"]:
                print(f"        cited nothing: {', '.join(cell['uncited_when_it_should_cite'])}")

    for system in board["systems"]:
        cell = board[system]
        if cell["expected_fail"]:
            print(f"\n  {system} expected_fail bucket (neither pass nor failure): "
                  f"{cell['expected_fail']}")
        if cell["errors"]:
            print(f"  {system} errored: {', '.join(cell['errors'])}")
        if cell["truncated"]:
            print(f"  {system} MAX_TOKENS (excluded): {', '.join(cell['truncated'])}")
        if cell["ungraded"]:
            print(f"  {system} ungraded: {', '.join(cell['ungraded'])}")

    print(f"\nOne-time ingestion cost: ${board['ingestion_cost_usd']:.2f} "
          f"(docs/metrics/extraction-cost-and-findings.md)")
    print(
        "\nNo per-stratum p95 is published and that was pre-registered: the strata\n"
        "carry 5-20 rows and a p95 over 5 is a maximum with a better name."
    )
    return 0


def run() -> None:
    """Execute all three systems and print the benchmark table."""
    raise SystemExit(main())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval", action="store_true", help="the table, from the artifact")
    parser.add_argument(
        "--refresh", action="store_true",
        help=f"sweep one system live and append to {ARTIFACT.name} (needs containers and a key)",
    )
    parser.add_argument("--system", choices=sorted(SYSTEMS), help="which system to sweep")
    parser.add_argument(
        "--live", action="store_true",
        help="pay the whole request instead of replaying passages; this is the pass "
             "the latency and cost columns come from",
    )
    parser.add_argument(
        "--sample", type=int, default=0,
        help="live pass only: measure latency on a deterministic stratified subset "
             "of N rows instead of all 100. $/query is derived and needs no live row.",
    )
    parser.add_argument(
        "--run-tag", default="",
        help="name this sweep as a REPETITION rather than the benchmark. Tagged rows "
             "are appended beside the published ones, are keyed separately, and are "
             "ignored by scoreboard(). Use for repeat runs (E1) and stratum re-runs.",
    )
    parser.add_argument(
        "--stratum", action="append", choices=sorted(STRATUM_ORDER), default=None,
        help="restrict the sweep to this stratum (repeatable). Requires --run-tag: a "
             "partial sweep is not the benchmark and must not overwrite it.",
    )
    parser.add_argument("--markdown", action="store_true", help="print only the README table")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not (args.eval or args.refresh):
        parser.error("pass --eval or --refresh")

    if args.refresh:
        if not args.system:
            parser.error("--refresh needs --system (one system per invocation; see the docstring)")
        if args.stratum and not args.run_tag:
            parser.error(
                "--stratum requires --run-tag: a stratum-local sweep covers a subset of "
                "the rows and would otherwise replace the full published pass for this "
                "system with a partial one"
            )
        mode = "live" if args.live else "replay"
        questions = load_questions()
        if args.live and args.sample:
            questions = live_sample(questions, args.sample)
        strata = tuple(args.stratum) if args.stratum else None
        if strata:
            questions = [q for q in questions if q["stratum"] in strata]
        print(
            f"sweeping {args.system!r} over {len(questions)} rows ({mode})"
            + (f" tag={args.run_tag!r}" if args.run_tag else "")
            + (f" strata={list(strata)}" if strata else ""),
            file=sys.stderr,
        )
        fresh = sweep(
            questions, args.system, live=args.live,
            run_tag=args.run_tag, only_strata=strata,
        )
        # The replace key includes `run_tag`, so a tagged sweep replaces only its
        # own previous rows and never the published untagged pass. Without this a
        # repeat run would delete the very rows it is being compared against.
        existing = [
            row for row in (load_artifact() if ARTIFACT.exists() else [])
            if not (
                row.get("system") == args.system
                and row.get("mode") == mode
                and (row.get("run_tag") or "") == args.run_tag
            )
        ]
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in [*existing, *fresh]) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(fresh)} rows for {args.system!r} ({mode}) to {ARTIFACT}", file=sys.stderr)

    board = scoreboard(load_artifact())
    if args.json:
        # `_by_id` is a working index, not a published number.
        printable = {
            k: ({kk: vv for kk, vv in v.items() if kk != "_by_id"}
                if isinstance(v, dict) and "_by_id" in v else v)
            for k, v in board.items()
        }
        print(json.dumps(printable, indent=2, ensure_ascii=False, default=str))
        return 0
    if args.markdown:
        print(markdown_table(board))
        return 0
    return _report(board)


if __name__ == "__main__":
    raise SystemExit(main())
