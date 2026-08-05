"""Assembling the two retrieval paths into one `documents` list.

No container, no API key, no spend: every input here is a hand-built
`ContextDoc`, which is the whole reason `assemble_detailed` takes documents
rather than a question.

Two classes of defect live here.

The first is **deletion dressed as deduplication**. The phase plan says "dedupe
by chunk_id across graph + vector paths", which was correct when one statement
was one chunk and is wrong now: two graph statements routinely share `chunks[0]`
(124 chunks assert one classification, so everything rendered off that leg
collapses to one id), and a GRAPH/PASSAGE pair sharing an id carries two
different texts. Deduping on that key silently deletes real statements and the
only statute text in the prompt.

The second is **an arm that measures something other than itself**. The four
budget arms are compared against each other and against an uncapped ceiling, so
an arm that quietly degrades to `budget_first` when its input is missing would
score as the constant while the artifact records its own name -- the failure
ADR-0012's constants exist to make visible.
"""

from __future__ import annotations

import pytest

from src.answer.context_assembly import (
    ADOPTED_BUDGET,
    BUDGETS,
    DEFAULT_BUDGET_N,
    LABEL_KEY,
    SOURCE_KEY,
    TEXT_KEY,
    AssemblyError,
    approx_tokens,
    assemble,
    assemble_detailed,
    budget_anchor,
    budget_first,
    budget_roundrobin,
    budget_uncapped,
)
from src.schemas import ContextDoc

# Measured 2026-08-04 by `python -m src.query.graph_query --baseline` and quoted
# in docs/metrics/query-path.md. A change to either is a finding to be written
# down there, not a constant to be re-tuned here.
CLASSIFIED_CHUNKS = 124  # chunks asserting one classification -- why chunk_id is not a key
DUPLICATED_TEXTS = 12    # corpus texts duplicated across 24 rows (reranker.py:208-210)


def graph_doc(
    chunk_id: str, text: str, provenance: list[str] | None = None
) -> ContextDoc:
    return ContextDoc(
        chunk_id=chunk_id,
        text=text,
        citation_label=f"AIA Art. {chunk_id}",
        source="GRAPH",
        score=None,
        provenance=provenance if provenance is not None else [chunk_id],
    )


def passage(chunk_id: str, text: str = "statute text", score: float = 0.9) -> ContextDoc:
    return ContextDoc(
        chunk_id=chunk_id,
        text=text,
        citation_label=f"AIA Art. {chunk_id}",
        source="PASSAGE",
        score=score,
    )


class Linked:
    """The two fields `budget_anchor` reads off a LinkedEntity."""

    def __init__(self, display_name: str, canonical_name: str = ""):
        self.display_name = display_name
        self.canonical_name = canonical_name or display_name.lower()


# --------------------------------------------------------------------------
# The document contract
# --------------------------------------------------------------------------

def test_one_document_is_emitted_per_input_document():
    result = assemble_detailed([graph_doc("a", "A.")], [passage("b")])
    assert len(result.documents) == 2
    assert result.graph_sent == 1 and result.passage_sent == 1


def test_no_chunk_id_is_ever_invented():
    """Every id in the output traces to an input. An assembled document that
    names a chunk nothing retrieved is a citation that cannot be validated."""
    given = {"a", "b", "c"}
    result = assemble_detailed(
        [graph_doc("a", "A."), graph_doc("b", "B.")], [passage("c")]
    )
    assert {doc.chunk_id for doc in result.by_id.values()} <= given
    assert result.chunk_ids <= given


def test_every_document_id_is_unique_even_when_two_statements_share_a_chunk_id():
    """124 chunks assert one classification, so `chunks[0]` is shared by every
    statement rendered off that leg. Keying documents on it would collapse them."""
    docs = [graph_doc("shared", f"statement {i}.") for i in range(5)]
    result = assemble_detailed(docs, [])
    ids = [d["id"] for d in result.documents]
    assert len(ids) == len(set(ids)) == 5
    assert len(result.by_id) == 5


def test_a_graph_and_a_passage_doc_with_the_same_chunk_id_both_survive():
    """They are not duplicates. One is a rendered relationship, the other is the
    statute; dropping the passage discards the only legislative prose in the
    prompt (query-path.md:650-654)."""
    result = assemble_detailed(
        [graph_doc("aia-art9-para1", "X applies to Y. (AIA Art. 9(1))")],
        [passage("aia-art9-para1", "The risk management system shall...")],
    )
    assert len(result.documents) == 2
    assert result.overlap_chunk_ids == ["aia-art9-para1"]


def test_the_cross_path_overlap_is_reported_and_never_collapsed():
    result = assemble_detailed(
        [graph_doc("a", "A."), graph_doc("b", "B.")],
        [passage("a", "statute a"), passage("c", "statute c")],
    )
    assert result.overlap_chunk_ids == ["a"]
    assert result.graph_sent == 2 and result.passage_sent == 2


def test_passages_with_identical_text_are_deduped_and_the_higher_ranked_kept():
    """12 corpus texts are duplicated across 24 rows and draw equal rerank
    scores, so which one gets cited would be arbitrary while Phase 5 grades the
    label. Input order is rerank order, so first wins."""
    shared = "The name, address and contact details of the provider;"
    result = assemble_detailed([], [passage("first", shared), passage("second", shared)])
    assert result.passage_sent == 1
    assert next(iter(result.by_id.values())).chunk_id == "first"
    assert result.text_duplicates == 1


def test_passages_are_deduped_on_chunk_id_as_well_as_on_text():
    result = assemble_detailed([], [passage("a", "one"), passage("a", "two")])
    assert result.passage_sent == 1


def test_graph_statements_are_deduped_on_text_and_never_on_chunk_id():
    result = assemble_detailed(
        [graph_doc("shared", "same."), graph_doc("shared", "same."),
         graph_doc("shared", "different.")],
        [],
    )
    assert result.graph_sent == 2
    assert result.text_duplicates == 1


def test_documents_are_ordered_graph_then_passage():
    """The two scores are not comparable (retriever.py:26-30), so ordering is the
    only cross-source priority signal and it has to be a decision."""
    result = assemble_detailed([graph_doc("g", "G.")], [passage("p")])
    sources = [d["data"][SOURCE_KEY] for d in result.documents]
    assert sources == ["GRAPH", "PASSAGE"]


def test_the_order_is_stable_across_two_calls():
    """Ids are `d0..dN` in output order, so a regeneration against a rebuilt list
    has to produce the same map -- otherwise a surviving citation points at a
    different document, which is one of the four ways `validate()` can fire."""
    graph = [graph_doc(f"g{i}", f"statement {i}.") for i in range(4)]
    passages = [passage(f"p{i}") for i in range(3)]
    first = assemble_detailed(graph, passages)
    second = assemble_detailed(graph, passages)
    assert [d["id"] for d in first.documents] == [d["id"] for d in second.documents]
    assert first.documents == second.documents


def test_assembling_nothing_returns_an_empty_list_and_does_not_raise():
    assert assemble([], []) == []
    result = assemble_detailed([], [])
    assert result.documents == [] and result.by_id == {}


def test_a_document_is_exactly_id_and_data():
    """`Document` requires `data: dict` in cohere==7.0.8; a bare v1-style
    `{"text": ...}` does not satisfy it."""
    result = assemble_detailed([graph_doc("a", "A.")], [])
    doc = result.documents[0]
    assert set(doc) == {"id", "data"}
    assert set(doc["data"]) == {TEXT_KEY, SOURCE_KEY, LABEL_KEY}


def test_the_source_value_is_the_unbracketed_literal_context_doc_pins():
    """`[GRAPH]`/`[PASSAGE]` in roadmap.md:222 is prose notation, not data. The
    value has to be the same string `ContextDoc.source` declares."""
    result = assemble_detailed([graph_doc("a", "A.")], [passage("b")])
    values = {d["data"][SOURCE_KEY] for d in result.documents}
    assert values == {"GRAPH", "PASSAGE"}


def test_the_chunk_ids_a_citation_may_name_are_the_provenance_union():
    """A graph statement fans out over what it rendered, so `validate()` is given
    the union and not just `{doc.chunk_id}`."""
    result = assemble_detailed([graph_doc("a", "A.", ["a", "b", "c"])], [passage("p")])
    assert result.chunk_ids == {"a", "b", "c", "p"}


# --------------------------------------------------------------------------
# The arms
# --------------------------------------------------------------------------

@pytest.mark.parametrize("arm", ["first", "roundrobin", "anchor"])
def test_every_capped_arm_returns_at_most_n(arm: str):
    docs = [graph_doc(f"g{i}", f"statement {i}.") for i in range(40)]
    result = assemble_detailed(
        docs, [], budget=arm, n=7, calls=[i % 3 for i in range(40)],
        linked=[Linked("statement 1")],
    )
    assert result.graph_sent == 7


def test_the_uncapped_arm_is_the_ceiling_and_ignores_n():
    docs = [graph_doc(f"g{i}", f"statement {i}.") for i in range(40)]
    assert len(budget_uncapped(docs, 5)) == 40


@pytest.mark.parametrize("arm", ["first", "roundrobin"])
def test_the_order_preserving_arms_preserve_relative_order_within_a_call(arm: str):
    """`anchor` is deliberately exempt: it is a stable partition, so a miss can
    move after a hit. It keeps relative order *within* each partition, which is
    asserted separately."""
    docs = [graph_doc(f"g{i}", f"statement {i}.") for i in range(12)]
    calls = [i % 3 for i in range(12)]
    result = assemble_detailed(docs, [], budget=arm, n=6, calls=calls)
    kept = [doc.chunk_id for doc in result.by_id.values()]
    for call in range(3):
        original = [f"g{i}" for i in range(12) if calls[i] == call]
        got = [c for c in kept if c in original]
        assert got == [c for c in original if c in got]


def test_first_and_roundrobin_are_identical_on_a_one_call_plan():
    """2 of the 10 routed rows have one call (`ag-002`, `th-003`), so 8 of 10
    discriminate. Stated as arithmetic before the sweep so the tie on those rows
    does not read as agreement between the arms."""
    docs = [graph_doc(f"g{i}", f"statement {i}.") for i in range(20)]
    calls = [0] * 20
    assert budget_first(docs, 5) == budget_roundrobin(docs, 5, calls=calls)


def test_roundrobin_interleaves_across_calls_where_first_would_not():
    """`obligations_for_role('provider')` renders 210 statements, so a first-N cap
    over the concatenation can spend the whole budget before the second call in
    the plan is reached. This is the only structural reason an arm can beat the
    constant."""
    docs = [graph_doc(f"a{i}", f"a{i}.") for i in range(10)]
    docs += [graph_doc(f"b{i}", f"b{i}.") for i in range(10)]
    calls = [0] * 10 + [1] * 10
    assert [d.chunk_id for d in budget_first(docs, 4)] == ["a0", "a1", "a2", "a3"]
    assert [d.chunk_id for d in budget_roundrobin(docs, 4, calls=calls)] == [
        "a0", "b0", "a1", "b1"
    ]


def test_roundrobin_visits_calls_in_plan_order_not_render_order():
    """Otherwise the interleave depends on which call happened to render first
    and stops being reproducible across a regeneration."""
    docs = [graph_doc("b0", "b0."), graph_doc("a0", "a0.")]
    assert [d.chunk_id for d in budget_roundrobin(docs, 2, calls=[1, 0])] == ["a0", "b0"]


def test_roundrobin_refuses_a_multi_call_plan_with_no_grouping():
    """Degrading to `first` here would score the round-robin arm as the constant
    while the artifact records `roundrobin` -- an arm measuring something other
    than itself."""
    docs = [graph_doc(f"g{i}", f"statement {i}.") for i in range(20)]
    with pytest.raises(AssemblyError, match="budget_first under another name"):
        assemble_detailed(docs, [], budget="roundrobin", n=5, calls=None)


def test_anchor_prefers_statements_naming_a_linked_entity():
    docs = [
        graph_doc("g0", "keep records applies to importer."),
        graph_doc("g1", "conduct a FRIA applies to deployer."),
        graph_doc("g2", "register the system applies to importer."),
    ]
    kept = budget_anchor(docs, 1, linked=[Linked("deployer")])
    assert [d.chunk_id for d in kept] == ["g1"]


def test_anchor_matches_the_display_name_because_that_is_what_the_prose_holds():
    """Statements are built from `display_name` by decision (ADR-0009's
    Correction), so matching on `canonical_name` would miss `high-risk AI system`
    for `high risk ai system` on every row."""
    docs = [
        graph_doc("g0", "duty applies to provider."),
        graph_doc("g1", "high-risk AI system is classified as high-risk."),
    ]
    linked = [Linked(display_name="high-risk AI system", canonical_name="high risk ai system")]
    assert [d.chunk_id for d in budget_anchor(docs, 1, linked=linked)] == ["g1"]


def test_anchor_is_a_stable_partition_and_not_a_sort():
    """Hits keep their relative order and so do misses. A comparison sort would
    be scoring statements against each other on a quantity with no scale."""
    docs = [
        graph_doc("m0", "unrelated one."),
        graph_doc("h0", "deployer duty one."),
        graph_doc("m1", "unrelated two."),
        graph_doc("h1", "deployer duty two."),
    ]
    kept = budget_anchor(docs, 4, linked=[Linked("deployer")])
    assert [d.chunk_id for d in kept] == ["h0", "h1", "m0", "m1"]


def test_anchor_with_nothing_linked_falls_back_to_the_constant():
    """Inventing an order would make this arm score as `first` while claiming to
    be anchor."""
    docs = [graph_doc(f"g{i}", f"statement {i}.") for i in range(6)]
    assert budget_anchor(docs, 3, linked=[]) == budget_first(docs, 3)


def test_the_budget_applies_to_the_graph_half_only():
    """Passages arrive already capped at the reranked top-5 and already ranked by
    something with a scale. The graph half is the one with 642 documents."""
    graph = [graph_doc(f"g{i}", f"statement {i}.") for i in range(20)]
    passages = [passage(f"p{i}", f"text {i}") for i in range(5)]
    result = assemble_detailed(graph, passages, budget="first", n=3)
    assert result.graph_sent == 3
    assert result.passage_sent == 5


def test_graph_available_records_what_the_budget_dropped():
    """An arm that scored badly on 50 of 642 and an arm that scored badly on 50
    of 50 are different findings."""
    graph = [graph_doc(f"g{i}", f"statement {i}.") for i in range(642)]
    result = assemble_detailed(graph, [], budget="first", n=50)
    assert result.graph_available == 642 and result.graph_sent == 50


def test_an_unknown_budget_arm_raises_rather_than_silently_defaulting():
    with pytest.raises(AssemblyError, match="is not a budget arm"):
        assemble_detailed([], [], budget="cheapest")


def test_the_rerank_arm_needs_the_question_it_is_ranking_against():
    with pytest.raises(AssemblyError, match="needs the question"):
        assemble_detailed([graph_doc("a", "A.")], [], budget="rerank", question=None)


def test_the_rerank_arm_carries_the_billed_search_units_to_the_result():
    """Adopting it makes route `graph` non-free for the first time, and a cost
    that does not reach the artifact is a cost that cannot be published."""
    from tests.test_reranker import FakeRerankClient

    docs = [graph_doc("a", "A."), graph_doc("b", "B.")]
    result = assemble_detailed(
        docs, [], budget="rerank", n=2, question="q",
        client=FakeRerankClient([(1, 0.9), (0, 0.4)], search_units=1.0),
    )
    assert result.rerank is not None
    assert result.rerank.search_units == 1.0
    assert [d.chunk_id for d in result.by_id.values()] == ["b", "a"]


def test_the_rerank_arm_never_calls_the_api_with_nothing_to_rank():
    from tests.test_reranker import ExplodingClient

    result = assemble_detailed([], [passage("p")], budget="rerank", question="q",
                               client=ExplodingClient())
    assert result.rerank is None
    assert result.passage_sent == 1


# --------------------------------------------------------------------------
# The constants this module publishes
# --------------------------------------------------------------------------

def test_the_adopted_budget_is_one_of_the_measured_arms():
    """The same contract `router.ADOPTED` and `template_selector.ADOPTED` carry:
    it is set by an ADR from a measurement, so it cannot be a name no arm has."""
    assert ADOPTED_BUDGET in BUDGETS


def test_the_uncapped_ceiling_is_an_arm_and_not_a_flag():
    """It has to be swept like the others or it is not a ceiling anything was
    measured against."""
    assert "uncapped" in BUDGETS


def test_approx_tokens_is_monotonic_and_never_zero_for_real_text():
    assert approx_tokens("") == 0
    assert approx_tokens("a") == 1
    assert approx_tokens("x" * 400) == 100
    assert approx_tokens("x" * 401) > approx_tokens("x" * 400)


def test_the_default_budget_is_large_enough_to_matter_and_small_enough_to_bind():
    """At N=50 three of the ten routed rows cannot discriminate; at N=642 none
    can. The number is pre-registered, and this pins that it is a real cap."""
    assert 1 < DEFAULT_BUDGET_N < 642
