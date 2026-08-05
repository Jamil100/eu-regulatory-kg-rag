"""The answer path, end to end: question -> cited answer.

route (router) -> retrieve (graph_path / retriever+reranker) -> assemble
(context_assembly) -> generate (generate) -> validate (citation_validator).
Mirrors `src/query/graph_path.py`, which is the same shape one layer down.
Step 7's `app.py` calls `answer()` and does nothing else.

THE REGENERATE-ONCE LOOP AND THE COUNTERS LIVE HERE, NOT IN `validate()`.

The plan puts "regenerate once, then fail loudly" in the validator's docstring.
It belongs here for two reasons. `validate()` is a pure membership test and a
function that can spend $0.03 is not one. And the rejection *rate* cannot be a
process-global counter: that is wrong in a FastAPI worker with more than one
request in flight, and unauditable in every process. It is recomputed by
`scoreboard()` from the artifact, the way every other published number in this
repo is.

**REGENERATION MUST CHANGE SOMETHING.** At `temperature=0, seed=42` an identical
second request is a guaranteed second charge for a guaranteed identical failure.
So the retry appends a user turn naming the rejected ids and restating that only
the provided documents may be cited, and `regeneration_fixed` records whether it
worked. `query-path.md` already logs R7B as *not* reproducible at those settings,
so "identical" is a prior to be measured rather than a guarantee -- which is why
the second call is made at all instead of being short-circuited.

ONE ARM PER INVOCATION, AND THE RATE LIMIT IS WHY.

~160 chat calls against a trial key at 10-20/min is 10-20 minutes minimum, and
the tenacity policy tops out near 3 minutes of backoff before giving up and
scoring the arm under measurement a zero -- which is what happened to `th-004` in
Step 5 and to the first rerank sweep in Step 4. The artifact is appended and
keyed by `budget`, the way `selector-eval.jsonl` holds both selectors in one
file, and `attempts` is recorded per row so a rate-limited row is visible in the
artifact rather than inferred from a bad score.

`passages=` IS WHAT KEEPS THE SWEEP CHEAP.

It replays the reranked orderings out of `eval/rerank-eval.jsonl` and fetches
text by `chunk_id` from Postgres, so every arm sees identical passages and the
vector half of the sweep costs **$0.00**. Same reasoning as
`retrieve_detailed(vector=...)` at `reranker.py:280-285`: an injection point that
makes the arms comparable is worth more than the API call it saves.

Usage:
    python -m src.answer.answer_path --prereg                    # $0.00, needs containers
    python -m src.answer.answer_path --question "..." --json
    python -m src.answer.answer_path --eval                      # from the artifact, pure
    python -m src.answer.answer_path --eval --refresh --budget first
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.answer.citation_validator import (
    LABEL_RE,
    normalise_label,
    span_defects,
    uncited_labels,
    validate,
)
from src.answer.context_assembly import (
    ADOPTED_BUDGET,
    BUDGETS,
    DEFAULT_BUDGET_N,
    AssemblyError,
    approx_tokens,
    assemble_detailed,
    budget_anchor,
    budget_first,
    budget_roundrobin,
    budget_uncapped,
)
from src.answer.generate import GenerateError, build_messages, generate_detailed
from src.schemas import Citation, ContextDoc, Route

if TYPE_CHECKING:
    from neo4j import Driver
    from psycopg import Connection

    from src.query.entity_linker import LinkedEntity

__all__ = [
    "ARTIFACT",
    "PREREG_KEY",
    "PASSAGE_TOP_N",
    "AnswerPathError",
    "AnswerResult",
    "answer",
    "sweep",
    "preregister",
    "scoreboard",
]

ROOT = Path(__file__).resolve().parents[2]

# Beside the eval set it measures, and tracked, for the reason router.py:66
# gives: the tests and the metrics doc read their numbers out of it, and both
# must work with no API key and no spend.
ARTIFACT = ROOT / "eval" / "answer-eval.jsonl"

# The pre-registration shares the artifact rather than getting a file of its own,
# keyed the way every arm is. `selector-eval.jsonl` already holds two selectors
# in one file; this holds four arms plus the ceiling plus the oracles. One file
# means `scoreboard()` keeps the single-argument signature the plan declared and
# a reader cannot pair an arm with a stale set of oracles.
PREREG_KEY = "_prereg"

# The reranked slice the vector path contributes. Not a budget arm: passages
# arrive already ranked by something with a scale, and `POST[5] = 27 of 51` is
# the measured citable ceiling they carry (`tests/test_reranker.py:48`).
PASSAGE_TOP_N = 5

# The Ns the pre-registration reports `first` retention at. The budget question
# is a curve, not a point, and publishing one number would make N look like a
# tuned parameter rather than a pre-registered one.
PREREG_NS = (1, 2, 3, 5, 10, 25, 50, 100)


class AnswerPathError(RuntimeError):
    """The answer path could not produce an answer.

    Deliberately not `SystemExit`, for the reason `RouterError` gives at
    src/query/router.py:101 -- this runs inside a FastAPI worker at Step 7.
    """


@dataclass
class AnswerResult:
    """One answered question, with everything the artifact and Step 7 need."""

    answer: str = ""
    citations: list[Citation] = field(default_factory=list)
    route: Route | str = ""
    budget: str = ADOPTED_BUDGET
    documents_sent: int = 0
    graph_sent: int = 0
    passage_sent: int = 0
    graph_available: int = 0
    overlap_chunk_ids: list[str] = field(default_factory=list)

    # The three checks, reported against one denominator.
    rejected: list[str] = field(default_factory=list)
    span_mismatches: list[int] = field(default_factory=list)
    uncited: list[str] = field(default_factory=list)

    regenerated: bool = False
    regeneration_fixed: bool = False

    finish_reason: str = ""
    content_blocks: int = 0
    dropped: list[str] = field(default_factory=list)
    attempts: int = 1
    cost_usd: float | None = None

    route_ms: float = 0.0
    graph_ms: float = 0.0
    vector_ms: float = 0.0
    assemble_ms: float = 0.0
    generate_ms: float = 0.0

    @property
    def latency_ms(self) -> float:
        return (
            self.route_ms + self.graph_ms + self.vector_ms
            + self.assemble_ms + self.generate_ms
        )


# --------------------------------------------------------------------------
# What the model was shown
# --------------------------------------------------------------------------

def labels_shown(docs: list[ContextDoc]) -> set[str]:
    """Every provision label that appeared in the prompt.

    Not just `doc.citation_label`. A graph statement renders up to
    `MAX_PROVENANCE` labels **inside its text** -- "... (AIA Art. 26(1), AIA Art.
    26(10), AIA Art. 26(11), +9 more)" -- and all three are strings the model can
    read and repeat. `uncited_labels` has to be asked "did the model name a
    provision it was not shown", so the denominator is what was *shown*, and the
    `+9 more` tail is deliberately not in it: that is a count, not a label, and a
    model that turns the count into an article number is the exact failure the
    check exists to catch.
    """
    shown = {normalise_label(doc.citation_label) for doc in docs if doc.citation_label}
    for doc in docs:
        for match in LABEL_RE.finditer(doc.text):
            shown.add(normalise_label(match.group(0)))
    return shown


def _correction_turn(rejected: list[str], uncited: list[str]) -> str:
    """The user turn the regeneration appends. It has to name the defect.

    Re-sending the identical request at `temperature=0, seed=42` is a second
    charge for the same failure. This states what was rejected, by id and by
    label, and restates the grounding rule -- so the second call is a different
    request and the measurement is of a repair rather than of a coin flip.
    """
    parts = ["Your previous answer cited sources that are not in the documents provided."]
    if rejected:
        parts.append(f"These citations named chunks that were not supplied: {', '.join(sorted(set(rejected))[:10])}.")
    if uncited:
        parts.append(f"These provision labels appear in your answer but in no document you were given: {', '.join(uncited[:10])}.")
    parts.append(
        "Answer the question again using only the documents provided. Cite only "
        "those documents. If they do not contain the answer, say so and name what "
        "is missing rather than supplying a provision from memory."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------
# The request path
# --------------------------------------------------------------------------

def answer(
    question: str,
    *,
    route: Route | None = None,
    driver: Driver | None = None,
    conn: Connection | None = None,
    client: Any | None = None,
    budget: str = ADOPTED_BUDGET,
    n: int = DEFAULT_BUDGET_N,
    passages: list[ContextDoc] | None = None,
    linked: list[LinkedEntity] | None = None,
    regenerate: bool = True,
) -> AnswerResult:
    """Route, retrieve, assemble, generate, validate. One question, one answer.

    `route` is injectable so a sweep can replay the router's committed decisions
    rather than re-deriving them; `passages` is injectable so the vector half can
    be replayed out of `eval/rerank-eval.jsonl` at $0.00. Both are the same trade
    `retrieve_detailed(vector=...)` makes.

    `driver` and `conn` follow the house lifecycle -- caller-owned if passed,
    opened and closed here if not -- so Step 7 hands in pooled handles.
    """
    if budget not in BUDGETS:
        raise AnswerPathError(f"{budget!r} is not a budget arm; have {sorted(BUDGETS)}")

    from src.query.router import RouterError, route_by_rules

    started = time.perf_counter()
    if route is None:
        decision = route_by_rules(question)
        if decision.route is None:
            raise AnswerPathError(f"the router returned no route for {question!r}")
        route = decision.route
        linked = linked or decision.linked
    route_ms = (time.perf_counter() - started) * 1000

    graph_docs: list[ContextDoc] = []
    doc_calls: list[int] = []
    graph_ms = 0.0
    retrieval_cost = 0.0
    if route in ("graph", "both"):
        from src.query.graph_path import GraphPathError, graph_search

        started = time.perf_counter()
        try:
            graph_result = graph_search(question, driver=driver, conn=conn, linked=linked)
        except GraphPathError as exc:
            raise AnswerPathError(f"graph retrieval failed: {exc}") from exc
        graph_ms = (time.perf_counter() - started) * 1000
        graph_docs = graph_result.docs
        doc_calls = graph_result.doc_calls
        retrieval_cost += graph_result.cost_usd or 0.0

    passage_docs: list[ContextDoc] = list(passages or [])
    vector_ms = 0.0
    if route in ("vector", "both") and passages is None:
        from src.query.reranker import CANDIDATES, RerankError, rerank_detailed
        from src.query.retriever import DIM, RetrieverError, retrieve_detailed

        started = time.perf_counter()
        try:
            retrieved = retrieve_detailed(
                question, CANDIDATES, dim=DIM, conn=conn, client=client
            )
            reranked = rerank_detailed(
                question, retrieved.docs, top_n=PASSAGE_TOP_N, client=client
            )
        except (RetrieverError, RerankError) as exc:
            raise AnswerPathError(f"vector retrieval failed: {exc}") from exc
        vector_ms = (time.perf_counter() - started) * 1000
        passage_docs = reranked.docs
        retrieval_cost += (retrieved.cost_usd or 0.0) + (reranked.cost_usd or 0.0)

    started = time.perf_counter()
    try:
        assembly = assemble_detailed(
            graph_docs,
            passage_docs,
            budget=budget,
            n=n,
            question=question,
            linked=linked,
            calls=doc_calls,
            client=client,
        )
    except AssemblyError as exc:
        raise AnswerPathError(str(exc)) from exc
    assemble_ms = (time.perf_counter() - started) * 1000
    retrieval_cost += (assembly.rerank.cost_usd or 0.0) if assembly.rerank else 0.0

    if not assembly.documents:
        raise AnswerPathError(
            f"route {route!r} produced no documents for {question!r}; there is "
            f"nothing to ground an answer in"
        )

    # `citation_label` is SELECTed, never recomputed (retriever.py:170). The
    # fan-out needs a label for `provenance[1]` and `provenance[2]`, which no
    # `ContextDoc` carries, so it comes from the same table the rest do.
    labels: dict[str, str] = {}
    wanted = sorted({c for doc in assembly.by_id.values() for c in (doc.provenance or [])})
    if wanted:
        from src.query.graph_path import label_map

        labels = label_map(wanted, conn)
    labels.update({doc.chunk_id: doc.citation_label for doc in assembly.by_id.values()})

    shown = labels_shown(list(assembly.by_id.values()))
    retrieved_ids = assembly.chunk_ids

    started = time.perf_counter()
    try:
        generated = generate_detailed(
            question, assembly.documents, client=client,
            by_id=assembly.by_id, labels=labels,
        )
    except GenerateError as exc:
        raise AnswerPathError(str(exc)) from exc

    ok = validate(generated.citations, retrieved_ids)
    rejected = sorted({c.chunk_id for c in generated.citations if c.chunk_id not in retrieved_ids})
    uncited = uncited_labels(generated.answer, shown)

    regenerated = False
    regeneration_fixed = False
    if regenerate and (not ok or uncited):
        # Different request, not the same one twice. See the module docstring.
        regenerated = True
        messages = [
            *build_messages(question),
            {"role": "assistant", "content": generated.answer},
            {"role": "user", "content": _correction_turn(rejected, uncited)},
        ]
        try:
            retry = generate_detailed(
                question, assembly.documents, client=client,
                by_id=assembly.by_id, labels=labels, messages=messages,
            )
        except GenerateError:
            # The first answer stands. A failed repair is not a reason to lose a
            # correct-but-imperfectly-cited answer, and the cost of the attempt is
            # already spent.
            retry = None
        if retry is not None:
            retry_ok = validate(retry.citations, retrieved_ids)
            retry_uncited = uncited_labels(retry.answer, shown)
            regeneration_fixed = retry_ok and not retry_uncited
            if regeneration_fixed or (retry_ok and len(retry_uncited) < len(uncited)):
                generated = _merge_cost(generated, retry)
                ok, rejected, uncited = retry_ok, [], retry_uncited
                rejected = sorted(
                    {c.chunk_id for c in retry.citations if c.chunk_id not in retrieved_ids}
                )
            else:
                # Keep the first answer, keep the second's cost. Silently
                # discarding a charge is how a cost table stops matching a bill.
                generated = _merge_cost(generated, retry, keep_answer=True)
    generate_ms = (time.perf_counter() - started) * 1000

    return AnswerResult(
        answer=generated.answer,
        citations=generated.citations,
        route=route,
        budget=budget,
        documents_sent=len(assembly.documents),
        graph_sent=assembly.graph_sent,
        passage_sent=assembly.passage_sent,
        graph_available=assembly.graph_available,
        overlap_chunk_ids=assembly.overlap_chunk_ids,
        rejected=rejected,
        span_mismatches=span_defects(generated.answer, generated.citations),
        uncited=uncited,
        regenerated=regenerated,
        regeneration_fixed=regeneration_fixed,
        finish_reason=generated.finish_reason,
        content_blocks=generated.content_blocks,
        dropped=generated.dropped,
        attempts=generated.attempts,
        cost_usd=(
            None
            if generated.cost_usd is None
            else generated.cost_usd + retrieval_cost
        ),
        route_ms=route_ms,
        graph_ms=graph_ms,
        vector_ms=vector_ms,
        assemble_ms=assemble_ms,
        generate_ms=generate_ms,
    )


def _merge_cost(first: Any, second: Any, keep_answer: bool = False) -> Any:
    """Carry both calls' cost and tokens onto whichever answer is kept.

    A regeneration is a second charge whether or not its answer is used. Dropping
    it would make the published cost per question short by exactly the rows that
    needed repairing -- the rows a cost table most needs to be right about.
    """
    kept = first if keep_answer else second
    return type(kept)(
        answer=kept.answer,
        citations=kept.citations,
        finish_reason=kept.finish_reason,
        content_blocks=kept.content_blocks,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cost_usd=(
            None
            if first.cost_usd is None or second.cost_usd is None
            else first.cost_usd + second.cost_usd
        ),
        latency_ms=first.latency_ms + second.latency_ms,
        attempts=max(first.attempts, second.attempts),
        dropped=[*first.dropped, *second.dropped],
    )


# --------------------------------------------------------------------------
# Pre-registration -- computed BEFORE any arm, $0.00, no API key
# --------------------------------------------------------------------------

def preregister(
    rows: list[dict],
    driver: Driver | None = None,
    conn: Connection | None = None,
) -> list[dict]:
    """The three oracles and the distributions, per routed row. Needs no key.

    **These are computed here, by code in this repo, and not by a script in a
    temp directory.** Step 5 logged that exact recurrence against itself -- the
    ceiling and oracle were computed outside the repo and transcribed into
    `src/` as literals, so `test_rules_reaches_the_oracle` compared a constant to
    itself (`failure-notes.md`, "Verified once by hand, never encoded"). The
    published values live in `tests/test_answer_path.py` as assertions, never as
    sources.

    The three oracles, in descending order of what they permit:

      `oracle_provenance`  |gold ∩ every chunk any returned row asserts|. This is
                           ADR-0013's headline, 24 of 32, and it is the number
                           **no citation can ever reach**: the union runs to 217
                           distinct chunks on `th-001`.
      `oracle_shown`       |gold ∩ ⋃ doc.provenance| over uncapped graph docs --
                           what `MAX_PROVENANCE=3` rendering actually put in
                           front of the model.
      `oracle_primary`     |gold ∩ {doc.chunk_id}| -- the ceiling on what
                           `validate()` and `AskResponse.citations` can carry if
                           the fan-out is ignored. **This is the number Step 6
                           builds against**, and if it is far below 24 that is
                           this step's finding rather than its failure.
    """
    from src.query.entity_linker import build_index, link_detailed
    from src.query.graph_path import graph_search

    owned_driver = driver is None
    if driver is None:
        from src.ingest.graph_writer import connect

        driver = connect()

    index = build_index()
    out: list[dict] = []
    try:
        for row in rows:
            if row["route"] not in ("graph", "both"):
                continue
            entities = link_detailed(row["question"], index)
            result = graph_search(
                row["question"], driver=driver, conn=conn, linked=entities, index=index
            )
            docs = result.docs
            gold = set(row["source_chunk_ids"])

            # The full union, straight off the executed rows -- ADR-0013's set.
            from src.query.graph_query import provenance_of, run_template

            union: list[str] = []
            for call in result.plan:
                try:
                    got = run_template(call.template, call.params, driver)
                except Exception:  # noqa: BLE001 -- an empty call is a fact, not a stop
                    continue
                for got_row in got:
                    union.extend(provenance_of(got_row))
            union_set = set(union)

            shown_set = {c for doc in docs for c in (doc.provenance or [])}
            primary_set = {doc.chunk_id for doc in docs}

            tokens = [approx_tokens(doc.text) for doc in docs] or [0]
            retention = {
                str(k): sorted(gold & {d.chunk_id for d in budget_first(docs, k)})
                for k in PREREG_NS
            }
            retention_shown = {
                str(k): sorted(gold & {c for d in budget_first(docs, k) for c in (d.provenance or [])})
                for k in PREREG_NS
            }

            # EVERY FREE ARM'S RETENTION, AT EVERY PRE-REGISTERED N.
            #
            # The adoption criterion is gold retention against `oracle_primary`,
            # and retention is a pure function of which statements survive the
            # budget -- so four of the five arms can be decided for **$0.00**,
            # before a single token is generated. Only `rerank` needs a call, and
            # only the citation/refusal half of the step needs generation.
            #
            # This is a departure from the plan's spend table and it is a
            # reduction, not a shortcut: the plan's own criterion is retention,
            # and measuring it without paying for generation measures it on more
            # rows, at every N, rather than at one. What generation is still
            # required for is the three rejection rates and the four refusal
            # rows, none of which any budget arm can move.
            arm_retention = {
                arm: {
                    str(k): sorted(gold & {d.chunk_id for d in fn(docs, k)})
                    for k in PREREG_NS
                }
                for arm, fn in (
                    ("uncapped", lambda d, k: budget_uncapped(d, k)),
                    ("first", budget_first),
                    ("roundrobin",
                     lambda d, k: budget_roundrobin(d, k, calls=result.doc_calls)),
                    ("anchor", lambda d, k: budget_anchor(d, k, linked=entities)),
                )
            }

            out.append({
                "budget": PREREG_KEY,
                "id": row["id"],
                "stratum": row["stratum"],
                "route": row["route"],
                "gold": sorted(gold),
                "calls": len(result.plan),
                "statements": len(docs),
                "doc_calls": result.doc_calls,
                "distinct_provenance": len(union_set),
                "oracle_provenance": sorted(gold & union_set),
                "oracle_shown": sorted(gold & shown_set),
                "oracle_primary": sorted(gold & primary_set),
                "tokens_mean": round(statistics.mean(tokens), 1),
                "tokens_max": max(tokens),
                "tokens_total": sum(tokens),
                "first_retention_primary": retention,
                "first_retention_shown": retention_shown,
                "arm_retention": arm_retention,
                "graph_ms": round(result.latency_ms, 2),
            })
    finally:
        if owned_driver:
            driver.close()
    return out


# --------------------------------------------------------------------------
# The sweep -- impure, needs a key, one arm per invocation
# --------------------------------------------------------------------------

def replayed_passages(conn: Connection | None = None) -> dict[str, list[ContextDoc]]:
    """The reranked top-5 per question, replayed from `eval/rerank-eval.jsonl`.

    Zero API calls: the ordering is committed and the text comes out of Postgres
    by `chunk_id`. Every arm therefore sees identical passages, so a difference
    between two arms is a difference in the graph budget and cannot be a
    different vector draw. Same reasoning as `retrieve_detailed(vector=...)`.
    """
    from src.query.reranker import load_artifact

    rows = load_artifact()
    wanted = sorted({cid for row in rows for cid in row["reranked"][:PASSAGE_TOP_N]})

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    try:
        fetched = conn.execute(
            "SELECT chunk_id, text, citation_label FROM chunks WHERE chunk_id = ANY(%s)",
            (wanted,),
        ).fetchall()
    finally:
        if owned:
            conn.close()
    by_chunk = {cid: (text, label) for cid, text, label in fetched}

    out: dict[str, list[ContextDoc]] = {}
    for row in rows:
        docs: list[ContextDoc] = []
        for rank, chunk_id in enumerate(row["reranked"][:PASSAGE_TOP_N]):
            if chunk_id not in by_chunk:
                continue
            text, label = by_chunk[chunk_id]
            docs.append(
                ContextDoc(
                    chunk_id=chunk_id,
                    text=text,
                    citation_label=label,
                    source="PASSAGE",
                    score=row["rerank_scores"][rank] if rank < len(row["rerank_scores"]) else None,
                )
            )
        out[row["id"]] = docs
    return out


def sweep(
    rows: list[dict],
    budget: str,
    n: int = DEFAULT_BUDGET_N,
    driver: Driver | None = None,
    conn: Connection | None = None,
    client: Any | None = None,
) -> list[dict]:
    """One arm over every eval row. The only part that spends.

    Routes are replayed from the eval set's gold labels rather than re-derived,
    so a router change cannot silently re-measure this step, and passages are
    replayed from `rerank-eval.jsonl`. What this pays for is generation, and
    generation only -- plus the rerank ranking if the arm is `rerank`.
    """
    if budget not in BUDGETS:
        raise AnswerPathError(f"{budget!r} is not a budget arm; have {sorted(BUDGETS)}")

    owned_driver = driver is None
    if driver is None:
        from src.ingest.graph_writer import connect

        driver = connect()
    owned_conn = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect as pg_connect

        conn = pg_connect()

    passages = replayed_passages(conn)
    out: list[dict] = []
    try:
        for row in rows:
            started = time.perf_counter()
            error = None
            try:
                result = answer(
                    row["question"],
                    route=row["route"],
                    driver=driver,
                    conn=conn,
                    client=client,
                    budget=budget,
                    n=n,
                    passages=passages.get(row["id"]) if row["route"] != "graph" else None,
                )
            except (AnswerPathError, GenerateError) as exc:
                # The arm under measurement must not take the sweep down with it;
                # the failure is recorded as this row's result. Same call
                # `template_selector.select_by_model` makes at :491.
                out.append({
                    "budget": budget,
                    "id": row["id"],
                    "stratum": row["stratum"],
                    "route": row["route"],
                    "gold": row["source_chunk_ids"],
                    "must_cite": row.get("must_cite"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                })
                continue

            cited = sorted({c.chunk_id for c in result.citations})
            out.append({
                "budget": budget,
                "n": n,
                "id": row["id"],
                "stratum": row["stratum"],
                "route": row["route"],
                "gold": row["source_chunk_ids"],
                "must_cite": row.get("must_cite"),
                "answer": result.answer,
                "citations": [c.model_dump() for c in result.citations],
                "cited_chunk_ids": cited,
                "cited_labels": sorted({c.citation_label for c in result.citations if c.citation_label}),
                "gold_cited": sorted(set(row["source_chunk_ids"]) & set(cited)),
                "documents_sent": result.documents_sent,
                "graph_sent": result.graph_sent,
                "passage_sent": result.passage_sent,
                "graph_available": result.graph_available,
                "overlap_chunk_ids": result.overlap_chunk_ids,
                "rejected": result.rejected,
                "span_mismatches": result.span_mismatches,
                "uncited": result.uncited,
                "regenerated": result.regenerated,
                "regeneration_fixed": result.regeneration_fixed,
                "finish_reason": result.finish_reason,
                "content_blocks": result.content_blocks,
                "dropped": result.dropped,
                "attempts": result.attempts,
                "cost_usd": result.cost_usd,
                "latency_ms": {
                    "route": round(result.route_ms, 2),
                    "graph": round(result.graph_ms, 2),
                    "vector": round(result.vector_ms, 2),
                    "assemble": round(result.assemble_ms, 2),
                    "generate": round(result.generate_ms, 2),
                },
                "error": error,
            })
    finally:
        if owned_driver:
            driver.close()
        if owned_conn:
            conn.close()
    return out


# --------------------------------------------------------------------------
# Measurement -- PURE. No DB, no key, no spend.
# --------------------------------------------------------------------------

def load_artifact(path: Path | None = None) -> list[dict]:
    path = path or ARTIFACT
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist. Run --prereg (free, needs containers), then "
            f"--eval --refresh --budget <arm> with an API key."
        )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_questions() -> list[dict]:
    from src.query.router import QUESTIONS

    return [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def scoreboard(artifact: list[dict]) -> dict[str, Any]:
    """Every published number, recomputed from the artifact alone.

    Pure -- no database, no API key, no network. That is what lets the tests and
    `docs/metrics/answer-path.md` quote the same figures, and what makes
    `--eval` reproducible on a laptop with the containers down.

    THE REJECTION RATE'S DENOMINATOR EXCLUDES `MAX_TOKENS` ROWS. A truncated
    answer can lose the closing half of its last citation, so its span and label
    rates measure the token limit rather than the model -- the same treatment
    `reranker.py:400` gives `attempts != 1`. The excluded rows are counted and
    named, never silently dropped.
    """
    prereg = [row for row in artifact if row.get("budget") == PREREG_KEY]
    arms = sorted({row["budget"] for row in artifact if row.get("budget") != PREREG_KEY})

    # The three oracles, summed over the pre-registered rows.
    oracles = {
        name: sum(len(row.get(name, [])) for row in prereg)
        for name in ("oracle_provenance", "oracle_shown", "oracle_primary")
    }
    prereg_gold = sum(len(row["gold"]) for row in prereg)

    # Rows on which no arm can differ: every statement fits inside the budget.
    # Publishing the discriminating denominator beside the total is what stops a
    # tie on those rows reading as agreement between the arms.
    def non_discriminating(n: int) -> list[str]:
        return sorted(row["id"] for row in prereg if row["statements"] <= n)

    ns_seen = sorted({row.get("n", DEFAULT_BUDGET_N) for row in artifact
                      if row.get("budget") != PREREG_KEY} or {DEFAULT_BUDGET_N})
    inert = {str(n): non_discriminating(n) for n in ns_seen}
    inert_gold = {
        str(n): sum(len(row["gold"]) for row in prereg if row["statements"] <= n)
        for n in ns_seen
    }

    # The `first` retention curve, from the pre-registration, at every N.
    curve = {
        str(k): {
            "primary": sum(len(row["first_retention_primary"].get(str(k), [])) for row in prereg),
            "shown": sum(len(row["first_retention_shown"].get(str(k), [])) for row in prereg),
        }
        for k in PREREG_NS
    }

    # The free half of the adoption: retention per arm per N, summed over the
    # pre-registered rows. `rerank` is absent because it is the one arm that
    # needs a call to rank; it is appended by its own sweep.
    free_arms = sorted({a for row in prereg for a in row.get("arm_retention", {})})
    arm_curve = {
        arm: {
            str(k): sum(
                len(row["arm_retention"][arm].get(str(k), [])) for row in prereg
            )
            for k in PREREG_NS
        }
        for arm in free_arms
    }
    arm_curve_rows = {
        arm: {
            str(k): sorted(
                row["id"] for row in prereg if row["arm_retention"][arm].get(str(k))
            )
            for k in PREREG_NS
        }
        for arm in free_arms
    }

    # THE ARMS DO NOT SHARE A DENOMINATOR UNLESS ONE IS IMPOSED, AND THIS FILE
    # WOULD OTHERWISE HAVE PUBLISHED THREE.
    #
    # Each arm excludes its own `MAX_TOKENS` rows, and they are not the same rows
    # -- `anchor` and `uncapped` lose `th-001`, `rerank` loses `ag-001`. So each
    # arm's `gold_total` is a different number (51 / 47 / 40) and "28 of 51"
    # against "28 of 47" reads as a tie between two things that were never
    # measured on the same set. `ag-001` alone carries 11 of the 51 gold chunks.
    # This is the same defect `template_selector._report` shipped once as
    # "Ceiling 10 of 9" and the same one `test_reranker` pins with `cap@k`: a
    # ratio whose numerator and denominator come from different populations.
    #
    # `common` is the rows every arm scored. It is the comparable number and it
    # is smaller and weaker than the per-arm one, which is the honest trade.
    scored_by_arm = {
        arm: {
            row["id"]
            for row in artifact
            if row.get("budget") == arm
            and not row.get("error")
            and row.get("finish_reason") != "MAX_TOKENS"
        }
        for arm in arms
    }
    common_ids = set.intersection(*scored_by_arm.values()) if scored_by_arm else set()
    gold_by_id = {
        row["id"]: row["gold"] for row in artifact if row.get("budget") != PREREG_KEY
    }
    board_common = {
        "ids": sorted(common_ids),
        "n": len(common_ids),
        "gold": sum(len(gold_by_id[i]) for i in common_ids),
        "excluded": sorted(
            {row["id"] for row in artifact if row.get("budget") != PREREG_KEY}
            - common_ids
        ),
    }

    board: dict[str, Any] = {
        "arms": arms,
        "free_arms": free_arms,
        "arm_retention": arm_curve,
        "arm_retention_rows": arm_curve_rows,
        "prereg_rows": len(prereg),
        "prereg_gold": prereg_gold,
        "oracles": oracles,
        "statements_total": sum(row["statements"] for row in prereg),
        "statements_per_row": {row["id"]: row["statements"] for row in prereg},
        "provenance_per_row": {row["id"]: row["distinct_provenance"] for row in prereg},
        "calls_per_row": {row["id"]: row["calls"] for row in prereg},
        "tokens_per_row": {row["id"]: row["tokens_total"] for row in prereg},
        "one_call_rows": sorted(row["id"] for row in prereg if row["calls"] <= 1),
        "tokens_mean": (
            round(statistics.mean([row["tokens_mean"] for row in prereg]), 1) if prereg else 0.0
        ),
        "tokens_max": max([row["tokens_max"] for row in prereg], default=0),
        "tokens_uncapped_total": sum(row["tokens_total"] for row in prereg),
        "non_discriminating": inert,
        "non_discriminating_gold": inert_gold,
        "first_retention": curve,
        "common": board_common,
    }

    for arm in arms:
        entries = [row for row in artifact if row.get("budget") == arm]
        ok = [row for row in entries if not row.get("error")]
        truncated = [row["id"] for row in ok if row.get("finish_reason") == "MAX_TOKENS"]
        clean = [row for row in ok if row.get("finish_reason") != "MAX_TOKENS"]
        must_cite = [row for row in clean if row.get("must_cite")]
        gold_total = sum(len(row["gold"]) for row in clean)
        gold_cited = sum(len(row.get("gold_cited", [])) for row in clean)

        latencies = sorted(
            sum(row["latency_ms"].values()) for row in clean if row.get("attempts", 1) == 1
        )
        costs = [row["cost_usd"] for row in clean if row.get("cost_usd") is not None]

        board[arm] = {
            "n_rows": len(entries),
            "errors": [row["id"] for row in entries if row.get("error")],
            "truncated": truncated,
            "scored": len(clean),
            "gold_total": gold_total,
            "gold_cited": gold_cited,
            # The comparable pair: same rows for every arm.
            "common_gold_cited": sum(
                len(row.get("gold_cited", [])) for row in clean if row["id"] in common_ids
            ),
            "common_scored": sum(1 for row in clean if row["id"] in common_ids),
            "rows_with_a_gold_citation": sum(1 for row in must_cite if row.get("gold_cited")),
            "must_cite_rows": len(must_cite),
            # The three checks, one denominator: `scored`.
            "rejected_rows": sorted(row["id"] for row in clean if row.get("rejected")),
            "span_defect_rows": sorted(row["id"] for row in clean if row.get("span_mismatches")),
            "uncited_rows": sorted(row["id"] for row in clean if row.get("uncited")),
            "uncited_labels": sorted({
                label for row in clean for label in row.get("uncited", [])
            }),
            "regenerated": sorted(row["id"] for row in clean if row.get("regenerated")),
            "regeneration_fixed": sorted(
                row["id"] for row in clean if row.get("regeneration_fixed")
            ),
            "dropped_citations": sum(len(row.get("dropped", [])) for row in clean),
            "multi_block_rows": sorted(
                row["id"] for row in clean if (row.get("content_blocks") or 0) > 1
            ),
            "graph_sent": sum(row.get("graph_sent", 0) for row in clean),
            "graph_available": sum(row.get("graph_available", 0) for row in clean),
            "overlap_rows": sorted(row["id"] for row in clean if row.get("overlap_chunk_ids")),
            "retried": sorted(row["id"] for row in clean if row.get("attempts", 1) > 1),
            "cost_usd": sum(costs) if costs else None,
            "latency_p50": statistics.median(latencies) if latencies else 0.0,
            "latency_max": max(latencies, default=0.0),
            # The four refusal rows, individually. NEVER averaged -- `oos-002`
            # says any citation is wrong while `hn-001`/`hn-002` say an uncited
            # correct answer is only partial, and one "refusal rate" over the
            # four would report a system that got all four right and a system
            # that got two right in two opposite ways as the same number.
            # plan-phase-3:622-624 already flags averaging them as a defect.
            "refusal": {
                row["id"]: {
                    "must_cite": row.get("must_cite"),
                    "stratum": row["stratum"],
                    "citations": len(row.get("citations", [])),
                    "gold_cited": row.get("gold_cited", []),
                    "uncited": row.get("uncited", []),
                    "answer": row.get("answer", ""),
                }
                for row in clean
                if row["stratum"] in ("out-of-scope", "unanswerable", "hard-negative")
            },
        }

        # THE INVARIANT. No arm can cite a gold chunk the uncapped graph path
        # never produced, and the vector half's ceiling is a union with it and
        # not a sum. Structural, so it holds regardless of which arm is adopted.
        board[arm]["exceeds_oracle"] = [
            row["id"]
            for row in clean
            if row["route"] == "graph"
            and len(row.get("gold_cited", [])) > _prereg_primary(prereg, row["id"])
        ]
    return board


def _prereg_primary(prereg: list[dict], row_id: str) -> int:
    for row in prereg:
        if row["id"] == row_id:
            return len(row["oracle_primary"])
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _report_prereg(board: dict[str, Any]) -> int:
    print(f"\nPRE-REGISTRATION over {board['prereg_rows']} routed rows, "
          f"{board['prereg_gold']} gold chunks. $0.00, no API key.\n")

    print("THE THREE ORACLES -- and the gap between them is the finding")
    print("-" * 74)
    total = board["prereg_gold"] or 1
    for name, label in (
        ("oracle_provenance", "every chunk any returned row asserts (ADR-0013's set)"),
        ("oracle_shown", "chunks rendered into a statement's own text"),
        ("oracle_primary", "chunk_id alone -- what a Citation can carry"),
    ):
        hit = board["oracles"][name]
        print(f"  {name:20} {hit:>3} of {total:<3} {hit / total:>6.1%}   {label}")
    equal = len(set(board["oracles"].values())) == 1
    print(
        "\n  Each is a subset of the one above it, so the inequality is structural.\n"
        "  `oracle_provenance` is what ADR-0013's headline quotes; `oracle_primary`\n"
        "  is the ceiling on what `validate()` and `AskResponse.citations` carry."
    )
    if equal:
        print(
            "\n  THEY ARE EQUAL, AND THE STEP PLAN PREDICTED THEY WOULD NOT BE.\n"
            "  `path_to_prose` really does keep only `chunks[0]` and render the rest as\n"
            "  label text -- but with 389-642 statements per row, every gold chunk in\n"
            "  the union is also the lexicographic minimum of SOME statement's own\n"
            "  provenance. The ContextDoc boundary never cost a gold chunk. See\n"
            "  ADR-0014; this is a premise about the code that was true and an\n"
            "  inference from it that was false."
        )

    print("\nSTATEMENTS AND PROVENANCE PER ROUTED ROW")
    print("-" * 74)
    print(f"  {'id':10} {'calls':>6} {'statements':>11} {'distinct prov':>14} "
          f"{'~tokens':>9}")
    per_row = board["statements_per_row"]
    for rid in sorted(per_row, key=lambda r: -per_row[r]):
        print(f"  {rid:10} {board['calls_per_row'][rid]:>6} {per_row[rid]:>11} "
              f"{board['provenance_per_row'][rid]:>14} "
              f"{board['tokens_per_row'][rid]:>9}")
    print(f"\n  total {board['statements_total']} statements, "
          f"~{board['tokens_uncapped_total']} tokens uncapped; "
          f"mean {board['tokens_mean']}, max {board['tokens_max']} tokens per statement")

    print("\nROWS ON WHICH NO ARM CAN DIFFER (every statement fits the budget)")
    print("-" * 74)
    for n, ids in board["non_discriminating"].items():
        gold = board["non_discriminating_gold"][n]
        print(f"  N={n:<4} {len(ids)} of {board['prereg_rows']} rows carrying "
              f"{gold} of {board['prereg_gold']} gold: {', '.join(ids) or '-'}")
    print(f"\n  One-call rows, where `first` and `roundrobin` are identical by\n"
          f"  arithmetic: {', '.join(board['one_call_rows']) or '-'}")

    print("\n`first-N` GOLD RETENTION CURVE (pre-registered, before any arm ran)")
    print("-" * 74)
    print(f"  {'N':>5} {'primary':>12} {'shown':>12}")
    for n, cell in board["first_retention"].items():
        print(f"  {n:>5} {cell['primary']:>7} of {total:<3} {cell['shown']:>7} of {total:<3}")

    if board["free_arms"]:
        print("\nGOLD RETENTION BY ARM -- the adoption criterion, at $0.00")
        print("-" * 74)
        header = "  ".join(f"{a:>11}" for a in board["free_arms"])
        print(f"  {'N':>5}  {header}")
        for n in board["first_retention"]:
            cells = "  ".join(
                f"{board['arm_retention'][a][n]:>4} of {total:<4}"
                for a in board["free_arms"]
            )
            marker = "  <- adopted N" if int(n) == DEFAULT_BUDGET_N else ""
            print(f"  {n:>5}  {cells}{marker}")
        print(
            "\n  Retention is a pure function of which statements survive the budget,\n"
            "  so four of the five arms are decided here with no generation and no\n"
            "  spend. `rerank` is the exception -- it needs a call to rank -- and the\n"
            "  three rejection rates and four refusal rows still need generation, which\n"
            "  no budget arm can move."
        )
    return 0


def _report(board: dict[str, Any]) -> int:
    if board["prereg_rows"]:
        _report_prereg(board)
    if not board["arms"]:
        print("\nNo arm has been swept yet. Run --eval --refresh --budget <arm>.")
        return 0

    print("\n\nARMS")
    print("-" * 96)
    common = board["common"]
    print(f"  {'arm':11} {'scored':>9} {'gold cited':>13} {'COMMON':>14} {'rej':>4} "
          f"{'span':>5} {'unc':>4} {'regen':>6} {'cost':>10} {'p50 ms':>8}")
    for arm in board["arms"]:
        a = board[arm]
        cost = f"${a['cost_usd']:.4f}" if a["cost_usd"] is not None else "unpriced"
        print(
            f"  {arm:11} {a['scored']:>3} of {a['n_rows']:<3} "
            f"{a['gold_cited']:>6} of {a['gold_total']:<4} "
            f"{a['common_gold_cited']:>7} of {common['gold']:<4} "
            f"{len(a['rejected_rows']):>4} {len(a['span_defect_rows']):>5} "
            f"{len(a['uncited_rows']):>4} {len(a['regenerated']):>6} "
            f"{cost:>10} {a['latency_p50']:>8.0f}"
        )
    print(
        f"\n  `gold cited` uses each arm's OWN denominator and is NOT comparable across\n"
        f"  arms: every arm drops its own MAX_TOKENS rows, and they are different rows.\n"
        f"  COMMON is the {common['n']} rows every arm scored, {common['gold']} gold "
        f"chunks. Excluded: {', '.join(common['excluded']) or '-'}."
    )

    print("\nTHE THREE CHECKS, ONE DENOMINATOR (rows scored, MAX_TOKENS excluded)")
    print("-" * 90)
    for arm in board["arms"]:
        a = board[arm]
        d = a["scored"] or 1
        print(f"  {arm}")
        print(f"    citation-validation rejection  {len(a['rejected_rows']):>2} of {d:<2} "
              f"{len(a['rejected_rows']) / d:>6.1%}   {', '.join(a['rejected_rows']) or '-'}")
        print(f"    span defect                    {len(a['span_defect_rows']):>2} of {d:<2} "
              f"{len(a['span_defect_rows']) / d:>6.1%}   {', '.join(a['span_defect_rows']) or '-'}")
        print(f"    uncited label in prose         {len(a['uncited_rows']):>2} of {d:<2} "
              f"{len(a['uncited_rows']) / d:>6.1%}   {', '.join(a['uncited_rows']) or '-'}")
        if a["truncated"]:
            print(f"    excluded, finish_reason=MAX_TOKENS: {', '.join(a['truncated'])}")
        if a["errors"]:
            print(f"    errored: {', '.join(a['errors'])}")
        if a["exceeds_oracle"]:
            print(f"    *** EXCEEDS oracle_primary on {', '.join(a['exceeds_oracle'])} "
                  f"-- the invariant broke, do not publish")

    print("\nREFUSAL -- n=4, SPLIT BY `must_cite`, NEVER AVERAGED")
    print("-" * 90)
    for arm in board["arms"]:
        rows = board[arm]["refusal"]
        if not rows:
            continue
        print(f"  {arm}")
        for rid, cell in sorted(rows.items()):
            want = "cite a retrieved chunk" if cell["must_cite"] else "decline, zero citations"
            print(f"    {rid:9} {cell['stratum']:<14} must_cite={str(cell['must_cite']):<5} "
                  f"citations={cell['citations']:<3} gold_cited={len(cell['gold_cited'])}  "
                  f"want: {want}")
    print(
        "\n  `oos-002` says ANY citation is wrong; `hn-001`/`hn-002` say an uncited\n"
        "  correct answer is only partial. One prompt must produce both from the\n"
        "  same route on adjacent questions. Reporting one 'refusal rate' over the\n"
        "  four would make those two failures look like one success."
    )
    return 0


def _print_answer(result: AnswerResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            "answer": result.answer,
            "route": result.route,
            "budget": result.budget,
            "documents_sent": result.documents_sent,
            "graph_sent": result.graph_sent,
            "passage_sent": result.passage_sent,
            "graph_available": result.graph_available,
            "citations": [c.model_dump() for c in result.citations],
            "rejected": result.rejected,
            "span_mismatches": result.span_mismatches,
            "uncited": result.uncited,
            "regenerated": result.regenerated,
            "regeneration_fixed": result.regeneration_fixed,
            "finish_reason": result.finish_reason,
            "content_blocks": result.content_blocks,
            "dropped": result.dropped,
            "cost_usd": result.cost_usd,
            "latency_ms": {
                "route": round(result.route_ms, 2),
                "graph": round(result.graph_ms, 2),
                "vector": round(result.vector_ms, 2),
                "assemble": round(result.assemble_ms, 2),
                "generate": round(result.generate_ms, 2),
                "total": round(result.latency_ms, 2),
            },
        }, indent=2, ensure_ascii=False))
        return

    cost = f"${result.cost_usd:.6f}" if result.cost_usd is not None else "unpriced"
    print(f"\nroute {result.route}  budget {result.budget}  "
          f"{result.graph_sent} graph + {result.passage_sent} passage documents "
          f"(of {result.graph_available} graph available)  {cost}  "
          f"{result.latency_ms:.0f} ms\n")
    print(result.answer)
    print(f"\n{len(result.citations)} citation(s):")
    seen: set[tuple[int, int]] = set()
    for citation in result.citations:
        span = (citation.start, citation.end)
        # ASCII only. Windows consoles default to cp1252 and a box-drawing or
        # arrow glyph raises `UnicodeEncodeError` *after* the answer has been
        # printed and the money spent -- which is how this was found.
        quoted = f'"{citation.text[:60]}"' if span not in seen else " " * 8 + "(same span)"
        seen.add(span)
        print(f"  {citation.citation_label or citation.chunk_id:<26} "
              f"[{citation.source}] {quoted}")
    if result.rejected:
        print(f"\nREJECTED (not in the retrieved set): {', '.join(result.rejected)}")
    if result.span_mismatches:
        print(f"SPAN DEFECTS at citation index: {result.span_mismatches}")
    if result.uncited:
        print(f"LABELS IN PROSE THAT NO DOCUMENT CARRIED: {', '.join(result.uncited)}")
    if result.dropped:
        print(f"dropped: {'; '.join(result.dropped[:5])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", help="answer one question end to end")
    parser.add_argument("--eval", action="store_true", help="the table, from the artifact")
    parser.add_argument(
        "--prereg", action="store_true",
        help="compute the three oracles and the distributions live; $0.00, needs containers",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help=f"sweep one arm live and append to {ARTIFACT.name} (needs an API key)",
    )
    parser.add_argument("--budget", choices=sorted(BUDGETS), default=ADOPTED_BUDGET)
    parser.add_argument("-n", type=int, default=DEFAULT_BUDGET_N, help="the budget size")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not (args.question or args.eval or args.prereg):
        parser.error("pass --question, --eval or --prereg")

    if args.question:
        try:
            result = answer(args.question, budget=args.budget, n=args.n)
        except AnswerPathError as exc:
            print(f"answer path failed: {exc}")
            return 1
        _print_answer(result, args.json)
        return 0

    if args.prereg:
        print("computing the pre-registration over the routed rows ($0.00)", file=sys.stderr)
        rows = preregister(load_questions())
        existing = [
            row for row in (load_artifact() if ARTIFACT.exists() else [])
            if row.get("budget") != PREREG_KEY
        ]
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in [*rows, *existing]) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(rows)} pre-registration rows to {ARTIFACT}", file=sys.stderr)
        return _report(scoreboard(load_artifact()))

    if args.refresh:
        print(f"sweeping arm {args.budget!r} at n={args.n} over the eval set",
              file=sys.stderr)
        fresh = sweep(load_questions(), args.budget, args.n)
        existing = [
            row for row in (load_artifact() if ARTIFACT.exists() else [])
            if not (row.get("budget") == args.budget and row.get("n", DEFAULT_BUDGET_N) == args.n)
        ]
        ARTIFACT.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in [*existing, *fresh]) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(fresh)} rows for {args.budget!r} to {ARTIFACT}", file=sys.stderr)

    return _report(scoreboard(load_artifact()))


if __name__ == "__main__":
    raise SystemExit(main())
