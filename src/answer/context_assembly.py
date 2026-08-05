"""Merge the two retrieval paths into the `documents` Command A is given.

This is the boundary where typed `ContextDoc`s become untyped documents for
Cohere's `documents` parameter, and where the graph path's output is finally
capped at something a prompt can hold.

THE BUDGET IS THE POINT OF THIS MODULE.

`docs/metrics/query-path.md` §Open names it "the largest open item on this path":
the graph path renders **2,886 statements over 9 questions** -- 1 on `th-003`,
642 on `3h-001` -- and nothing ranks them, because there is nothing to rank them
by. `ContextDoc.score` is `None` on every graph document by design, and the two
source scales are not comparable (`retriever.py:26-30`). So five arms are built
and measured against the same oracle, the way ADR-0012 and ADR-0013 did it, and
ADR-0014 records the adoption together with every loser's numbers:

  uncapped    the ceiling. Not a production arm -- it exists so the others have
              something to lose against.
  first       the constant. `docs[:n]` in path order. A budget that cannot beat
              this has not earned its network hop, its cost, or its code.
  roundrobin  interleave across the template calls in the plan. The one arm the
              constant can lose to for a structural reason:
              `obligations_for_role('provider')` renders 210 statements, so a
              first-N cap over the concatenation can spend the whole budget
              before the second call is reached.
  anchor      keep the statements that name an entity the question linked to.
              Free, deterministic, no API call -- the same shape as the rules
              arm that beat Command R7B twice.
  rerank      Rerank 3.5 over the statements as documents. The only arm that
              costs money, and adopting it makes route `graph` non-free for the
              first time.

THE PLAN'S DEDUPE INSTRUCTION IS HONOURED AS AN ORDERING RULE, NOT A DELETION
RULE, AND THE REASON IS RECORDED RATHER THAN ARGUED.

`plan-phase-3-router-and-query-path.md:544-547` says "dedupe by chunk_id across
graph + vector paths". That was written when the graph-side stub was
`path_to_prose(paths) -> list[Chunk]` (`:99-101`), where one statement *was* one
chunk and `chunk_id` *was* a key. Step 0 broke that deliberately and ADR-0011
recorded it. Two consequences, both measured:

  1. `chunk_id` is not a key on the graph side. Two different statements
     routinely share `chunks[0]` -- 124 chunks assert the same classification, so
     everything rendered from that leg collapses to one id. Deduping graph
     documents on it would delete real statements.
  2. A GRAPH and a PASSAGE document sharing a `chunk_id` are not duplicates. They
     carry different text: one is a rendered relationship, the other is the
     statute. Dropping the passage discards the only legislative prose in the
     prompt, which `query-path.md:650-654` already flags as the open question for
     route `graph`.

So: dedupe **within** source, report the cross-path overlap, collapse nothing.
Same species of correction as Step 4's "recall@5-after vs recall@10-before" -- a
plan instruction invalidated by an earlier step of the same plan, written up in
ADR-0014 rather than silently ignored.

ORDERING IS A DECISION, NOT A SIDE EFFECT OF `graph_docs + passage_docs`.

Graph statements first in budget order, then passages in rerank order. The two
scores are not comparable, so ordering is the only cross-source priority signal
available and it has to be chosen on purpose. Graph goes first because a
statement is a claim about structure that the passage text below it then
evidences; the reverse order buries the structural answer under statute prose on
exactly the `both` questions that need it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from src.schemas import ContextDoc

if TYPE_CHECKING:
    from src.query.entity_linker import LinkedEntity
    from src.query.reranker import RerankResult

__all__ = [
    "TEXT_KEY",
    "SOURCE_KEY",
    "LABEL_KEY",
    "DOC_ID_PREFIX",
    "DEFAULT_BUDGET_N",
    "BUDGETS",
    "ADOPTED_BUDGET",
    "AssemblyError",
    "AssemblyResult",
    "approx_tokens",
    "budget_uncapped",
    "budget_first",
    "budget_roundrobin",
    "budget_anchor",
    "budget_rerank",
    "assemble",
    "assemble_detailed",
]

# The `data` keys, which the plan left open ("labeled [GRAPH] or [PASSAGE] in its
# metadata"). Cohere passes both the key name and the value to the model, so
# these are prompt text and are named for the reader of the prompt rather than
# for the reader of this file.
#
# `[GRAPH]`/`[PASSAGE]` with brackets, in `roadmap.md:222` and in this module's
# previous docstring, is prose notation and not data. The value stored is the
# unbracketed literal `ContextDoc.source` already pins (`schemas.py:258`), so the
# string in the prompt and the string in the type are the same string.
TEXT_KEY, SOURCE_KEY, LABEL_KEY = "text", "source", "citation"

# Ids are `d0..dN` in output order. Short on purpose: the model echoes them back
# in `sources[].id`, and a long id is tokens spent on a key.
DOC_ID_PREFIX = "d"

# The graph-statement budget, pre-registered before any arm was written.
#
# Chosen from a token budget rather than assumed. A rendered statement is short
# and its length is dominated by the citation tail -- "X applies to provider.
# (AIA Art. 26(1), AIA Art. 26(10), AIA Art. 26(11), +9 more)" -- so 50
# statements is roughly 1.5-2k tokens of prompt, against Command A's 256k
# context. The constraint is not the context window; it is that 642 near-
# identical statements is not evidence, and a model asked to read 320 of them
# per question will cite whichever it saw first. `answer_path.scoreboard()`
# reports the measured mean/max so this number can be checked rather than
# believed.
DEFAULT_BUDGET_N = 50

BUDGETS = ("uncapped", "first", "roundrobin", "anchor", "rerank")

# Set by ADR-0014 from the measurement in docs/metrics/answer-path.md, not by
# preference -- the same contract `router.ADOPTED` and `template_selector.ADOPTED`
# carry. Change it there and here together, or the ADR stops describing the code
# it claims to describe.
#
# THE CONSTANT WON, AND NOT ON RETENTION. Measured 2026-08-05 at N=50 over the
# 10 routed rows (35 gold): `uncapped` 25, `anchor` 10, `first` 8, `roundrobin`
# 4. On retention alone `first` is third. It is adopted because it is **the only
# arm that produced a scored answer on all 23 rows**: `anchor` and `uncapped`
# truncate on `th-001` and `rerank` on `ag-001`, all three by leaking `<co>`
# citation markup into the answer text until `max_tokens`. An arm that reaches
# more gold on the rows where it does not collapse has not reached more gold.
#
# `anchor` is +2 over `first`, which is exactly ADR-0004's declared resolution
# for this eval set, so it is not a measured difference even before the
# truncation. `roundrobin` is -4, which is.
ADOPTED_BUDGET = "first"


class AssemblyError(RuntimeError):
    """Documents could not be assembled.

    Deliberately not `SystemExit`, for the reason `RouterError` gives at
    src/query/router.py:101 -- this runs inside a FastAPI worker at Step 7.
    """


@dataclass
class AssemblyResult:
    """The documents, plus everything the sweep has to record about them.

    `graph_available` against `graph_sent` is the whole budget question in two
    integers, and it has to survive to the artifact: an arm that scored badly
    because it dropped 592 statements and an arm that scored badly on 50 of 50
    are different findings.

    `overlap_chunk_ids` is reported and never acted on -- see the module
    docstring on why a GRAPH/PASSAGE pair sharing a chunk id is not a duplicate.
    Recorded because "how often do the two paths land on the same chunk" is a
    real question about the `both` route that nothing else measures.
    """

    documents: list[dict] = field(default_factory=list)
    by_id: dict[str, ContextDoc] = field(default_factory=dict)
    graph_sent: int = 0
    passage_sent: int = 0
    graph_available: int = 0
    overlap_chunk_ids: list[str] = field(default_factory=list)
    text_duplicates: int = 0
    budget: str = ADOPTED_BUDGET
    rerank: RerankResult | None = None

    @property
    def chunk_ids(self) -> set[str]:
        """Every chunk id a citation from this prompt may name.

        The union of the documents' `provenance`, not of their `chunk_id`: a
        graph document fans out over what it rendered, and `validate()` is given
        this set. `chunk_id` is `provenance[0]` on a graph document, so it is
        included either way.
        """
        found: set[str] = set()
        for doc in self.by_id.values():
            found.add(doc.chunk_id)
            found.update(doc.provenance)
        return found


def approx_tokens(text: str) -> int:
    """A token count with no API call, and it is an approximation on purpose.

    ~4 characters per token, the standard English heuristic. The corpus chunker
    stores whitespace word counts under the name `token_count`
    (`chunker.py:73`), which undercounts BPE by roughly a third; this is used for
    the budget arithmetic only, where a systematic 10-20% error changes nothing
    about which arm wins. Anything that is charged for reads
    `usage.billed_units`, never this.
    """
    return math.ceil(len(text) / 4)


# --------------------------------------------------------------------------
# The arms
#
# Every one takes `(docs, n, **kwargs)` and returns at most `n` documents, so
# `assemble_detailed` can dispatch on a name without special-casing. The keyword
# each needs is declared rather than pulled from a context object: an arm that
# silently degrades when its input is missing is an arm that scores as itself
# while measuring something else.
# --------------------------------------------------------------------------

def budget_uncapped(docs: list[ContextDoc], n: int = 0, **_: Any) -> list[ContextDoc]:
    """Everything. The ceiling, not a production arm.

    `n` is accepted and ignored so the dispatch table stays uniform. This is what
    the other four lose against, and it is the arm that answers "is the budget
    costing us gold, or is 642 statements just 642 ways to say the same thing?".
    """
    return list(docs)


def budget_first(docs: list[ContextDoc], n: int, **_: Any) -> list[ContextDoc]:
    """The first `n` in path order. The constant.

    Its order is `graph_search`'s: template calls in plan order, statements in
    render order within a call. That is not a ranking -- which is exactly what
    makes it the right control. A budget arm that cannot beat "take the first
    fifty" has not earned its place.
    """
    return list(docs[:n])


def budget_roundrobin(
    docs: list[ContextDoc], n: int, *, calls: list[int] | None = None, **_: Any
) -> list[ContextDoc]:
    """Interleave across the template calls in the plan.

    `calls` is parallel to `docs` and holds each statement's call index
    (`GraphResult.doc_calls`). One pass per round, each call contributing its
    next unspent statement, until `n` is reached.

    **Identical to `budget_first` on a one-call plan**, which is 2 of the 10
    routed rows (`ag-002`, `th-003`). Stated here before any measurement so the
    tie on those rows reads as arithmetic rather than as a result -- the same
    treatment `test_reranker.py` gives `cap@k`.

    A missing or mis-shaped `calls` degrades to `budget_first` rather than
    raising, because on a single-call plan that *is* the correct answer -- but it
    is recorded by `assemble_detailed`, which refuses to run this arm on a
    multi-call plan with no grouping.
    """
    if not calls or len(calls) != len(docs):
        return budget_first(docs, n)

    buckets: dict[int, list[ContextDoc]] = {}
    for call_index, doc in zip(calls, docs, strict=True):
        buckets.setdefault(call_index, []).append(doc)

    out: list[ContextDoc] = []
    cursors = {key: 0 for key in buckets}
    # `sorted(buckets)` and not insertion order: the round has to visit the calls
    # in plan order every time, or the interleave depends on which call happened
    # to render first and stops being reproducible across a regeneration.
    while len(out) < n:
        progressed = False
        for key in sorted(buckets):
            if len(out) >= n:
                break
            cursor = cursors[key]
            if cursor < len(buckets[key]):
                out.append(buckets[key][cursor])
                cursors[key] = cursor + 1
                progressed = True
        if not progressed:
            break
    return out


def budget_anchor(
    docs: list[ContextDoc], n: int, *, linked: Iterable[LinkedEntity] | None = None, **_: Any
) -> list[ContextDoc]:
    """Prefer statements that name an entity the question actually linked to.

    Free, deterministic, and no new call: `linked` is the `list[LinkedEntity]`
    the router already produced and `graph_search` already receives
    (`graph_path.py:129`). This is the same bet ADR-0012 and ADR-0013 both won --
    that the deterministic stage is the one that works -- made once more where it
    can be measured against a cross-encoder rather than assumed.

    Matching is on `display_name` because that is what the prose contains: the
    statements are built from `display_name` by decision (ADR-0009's Correction),
    so matching on `canonical_name` would miss `high-risk AI system` for
    `high risk ai system` on every row. Case-insensitive substring, because a
    statement says "high-risk AI system" where the question said "high-risk AI
    systems" and a word-boundary regex per entity per statement is 642 x 4 regex
    compilations to make a tie-break slightly sharper.

    A **stable partition**, not a sort: hits keep their relative order and so do
    misses. A comparison-sort here would be scoring statements against each other
    on a quantity that has no scale, which is the thing this whole module refuses
    to do to `ContextDoc.score`.
    """
    names = [
        (entity.display_name or entity.canonical_name).lower()
        for entity in (linked or [])
    ]
    names = [name for name in names if name]
    if not names:
        # Nothing to prefer. Falling back to the constant is honest; inventing an
        # order would make this arm score as `first` while claiming to be anchor.
        return budget_first(docs, n)

    hits = [doc for doc in docs if any(name in doc.text.lower() for name in names)]
    if len(hits) >= n:
        return hits[:n]
    seen = {id(doc) for doc in hits}
    rest = [doc for doc in docs if id(doc) not in seen]
    return [*hits, *rest[: n - len(hits)]]


def budget_rerank(
    docs: list[ContextDoc],
    n: int,
    *,
    question: str = "",
    client: Any | None = None,
    **_: Any,
) -> tuple[list[ContextDoc], RerankResult | None]:
    """Rerank 3.5 over the statements as documents. The only arm that spends.

    Reuses `reranker.rerank_detailed` unchanged -- its input is already
    `list[ContextDoc]` and its output already carries the billed search units, so
    the one arm with a price tag reports the same audited quantity the vector
    path does.

    Returns the `RerankResult` alongside the documents rather than swallowing it:
    billing is `ceil(len(docs)/100)` search units per row, ~39 over the routed
    set, and adopting this arm makes route `graph` non-free for the first time.
    A cost that does not reach the artifact is a cost that cannot be published.

    **The scores will be nearly tied and that is a prediction, not an excuse.**
    210 statements render as "<obligation> applies to provider." with only the
    obligation varying, so a cross-encoder sees 210 near-identical documents
    against one query. `reranker.py:211`'s `index` tiebreak then decides the
    boundary -- which is `budget_first`'s ordering wearing a $0.002 hat. The
    sweep records the top-N score spread so that can be measured instead of
    assumed.
    """
    from src.query.reranker import RerankError, rerank_detailed

    if not docs:
        return [], None
    if not question:
        raise AssemblyError("budget_rerank needs the question it is ranking against")
    try:
        result = rerank_detailed(question, docs, top_n=min(n, len(docs)), client=client)
    except RerankError as exc:
        raise AssemblyError(f"rerank budget failed: {exc}") from exc
    return result.docs, result


_ARMS = {
    "uncapped": budget_uncapped,
    "first": budget_first,
    "roundrobin": budget_roundrobin,
    "anchor": budget_anchor,
    "rerank": budget_rerank,
}


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _dedupe_graph(docs: list[ContextDoc]) -> tuple[list[ContextDoc], list[int], int]:
    """Graph statements, deduped on `text` and never on `chunk_id`.

    Returns the surviving indices too, so a caller holding a parallel `calls`
    list can keep it aligned. `graph_search` already dedupes on text within one
    request; this repeats it because `assemble` is also called with documents
    from somewhere else (a replayed sweep, a test) and an idempotent guard is
    cheaper than a precondition nobody checks.
    """
    out: list[ContextDoc] = []
    kept: list[int] = []
    seen: set[str] = set()
    duplicates = 0
    for index, doc in enumerate(docs):
        if doc.text in seen:
            duplicates += 1
            continue
        seen.add(doc.text)
        out.append(doc)
        kept.append(index)
    return out, kept, duplicates


def _dedupe_passages(docs: list[ContextDoc]) -> tuple[list[ContextDoc], int]:
    """Passages, deduped on `chunk_id` **and** on `text`, highest-ranked kept.

    The text key is not redundant. The corpus holds 12 duplicated `text` values
    across 24 rows (`reranker.py:208-210`) -- "The name, address and contact
    details of the provider;" appears under several chunk ids -- and they draw
    exactly equal rerank scores. Sending both wastes a slot, and which of the two
    the model cites would be arbitrary while Phase 5 grades the label. Input
    order is rerank order, so "first wins" is "highest-ranked wins".
    """
    out: list[ContextDoc] = []
    ids: set[str] = set()
    texts: set[str] = set()
    duplicates = 0
    for doc in docs:
        if doc.chunk_id in ids or doc.text in texts:
            duplicates += 1
            continue
        ids.add(doc.chunk_id)
        texts.add(doc.text)
        out.append(doc)
    return out, duplicates


def assemble_detailed(
    graph_docs: list[ContextDoc],
    passage_docs: list[ContextDoc],
    *,
    budget: str = ADOPTED_BUDGET,
    n: int = DEFAULT_BUDGET_N,
    question: str | None = None,
    linked: Iterable[LinkedEntity] | None = None,
    calls: list[int] | None = None,
    client: Any | None = None,
) -> AssemblyResult:
    """Apply the budget to the graph half, dedupe both halves, emit documents.

    The budget applies to graph statements only. Passages arrive already capped
    at the reranked top-5 and already ranked by something with a scale; the graph
    half is the one with 642 documents and no order.
    """
    if budget not in _ARMS:
        raise AssemblyError(f"{budget!r} is not a budget arm; have {sorted(_ARMS)}")
    if n < 1 and budget != "uncapped":
        raise AssemblyError(f"n must be at least 1, got {n}")

    graph, kept, graph_duplicates = _dedupe_graph(list(graph_docs))
    aligned_calls = [calls[i] for i in kept] if calls and len(calls) == len(graph_docs) else None

    if budget == "roundrobin" and aligned_calls is None and len(graph) > n:
        # Silently degrading to `first` here would score the round-robin arm as
        # the constant while the artifact says `roundrobin` -- an arm measuring
        # something other than itself, which is the defect ADR-0012's constants
        # exist to make visible.
        raise AssemblyError(
            "budget_roundrobin needs `calls` parallel to graph_docs; without the "
            "call grouping it is budget_first under another name"
        )

    rerank_result: RerankResult | None = None
    if budget == "rerank":
        graph, rerank_result = budget_rerank(graph, n, question=question or "", client=client)
    else:
        graph = _ARMS[budget](graph, n, calls=aligned_calls, linked=linked)

    passages, passage_duplicates = _dedupe_passages(list(passage_docs))

    # Ordering: graph first in budget order, then passages in rerank order. See
    # the module docstring -- this is a decision, not concatenation.
    ordered = [*graph, *passages]

    documents: list[dict] = []
    by_id: dict[str, ContextDoc] = {}
    for index, doc in enumerate(ordered):
        doc_id = f"{DOC_ID_PREFIX}{index}"
        by_id[doc_id] = doc
        documents.append(
            {
                "id": doc_id,
                "data": {
                    TEXT_KEY: doc.text,
                    SOURCE_KEY: doc.source,
                    LABEL_KEY: doc.citation_label,
                },
            }
        )

    graph_ids = {doc.chunk_id for doc in graph}
    overlap = sorted(graph_ids & {doc.chunk_id for doc in passages})

    return AssemblyResult(
        documents=documents,
        by_id=by_id,
        graph_sent=len(graph),
        passage_sent=len(passages),
        graph_available=len(graph_docs),
        overlap_chunk_ids=overlap,
        text_duplicates=graph_duplicates + passage_duplicates,
        budget=budget,
        rerank=rerank_result,
    )


def assemble(graph_docs: list[ContextDoc], passage_docs: list[ContextDoc]) -> list[dict]:
    """Merge and dedupe context documents, labeling their source path.

    Inputs are ContextDoc, not Chunk (ADR-0011) -- `graph_docs` in particular are
    path_to_prose's rendered statements, which are not corpus rows. The return
    type stays `list[dict]` on purpose: this is the boundary where a typed
    ContextDoc becomes an untyped document for Cohere's `documents` parameter,
    and that boundary should not be blurred by returning a Pydantic model on one
    side of a Cohere API call and a plain dict on the other.

    The declared signature is honoured, so nothing that already calls it has to
    change; everything the sweep and Step 7 need is on `assemble_detailed`.
    Note that this form cannot run the `roundrobin` or `rerank` arms -- neither
    has its input here -- which is a second reason the detailed form exists.
    """
    return assemble_detailed(graph_docs, passage_docs).documents
