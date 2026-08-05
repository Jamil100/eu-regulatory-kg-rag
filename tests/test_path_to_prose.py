"""Rendering graph rows into citable statements.

Everything here except the last three tests runs with no database: the renderers
are pure functions over the plain dicts `graph_query.row_to_dict` produces, and
`labels` is injected rather than looked up, so a hand-built row is a complete
test input.
"""

from __future__ import annotations

import pytest

from src.answer.path_to_prose import ANNEX_CAVEAT, ProseError, path_to_prose
from src.query.graph_path import label_map
from src.query.graph_query import provenance_of, run_template

# Measured live 2026-08-04 by `python -m src.query.graph_query --baseline`.
CLASSIFIED_CHUNKS = 124  # chunks asserting `high risk ai system -> high risk`
SYSTEM_ROWS = 169
DERIVED_EDGES = 22  # ADR-0010


def node(canonical: str, display: str | None = None, labels: list[str] | None = None) -> dict:
    return {
        "labels": labels or ["Entity"],
        "canonical_name": canonical,
        "display_name": display or canonical,
    }


# --------------------------------------------------------------------------
# Pure -- the rendering contract
# --------------------------------------------------------------------------

def test_prose_uses_display_name_and_never_the_key() -> None:
    """ADR-0009's Correction added `display_name` for exactly this. Built from
    `canonical_name` the graph path would cite `high risk` and `aia art. 1(1)`
    instead of `high-risk` and `AIA Art. 1(1)`."""
    rows = [
        {
            "t": node("high risk ai system", "high-risk AI system"),
            "a": node("aia art. 1(1)", "AIA Art. 1(1)", ["Article"]),
            "defined_chunks": ["c1"],
        }
    ]
    docs = path_to_prose(rows, "definition_of", labels={"c1": "AIA Art. 1(1)"})
    assert docs[0].text.startswith("high-risk AI system is defined in AIA Art. 1(1).")
    assert "aia art. 1(1)" not in docs[0].text
    assert "high risk ai system" not in docs[0].text


def test_a_node_without_a_display_name_falls_back_to_the_key() -> None:
    rows = [{"t": {"labels": ["Entity"], "canonical_name": "provider"}, "a": node("aia art. 3"),
             "defined_chunks": ["c1"]}]
    docs = path_to_prose(rows, "definition_of", labels={"c1": "AIA Art. 3(3)"})
    assert "provider is defined in" in docs[0].text


def test_the_hot_fact_collapses_to_one_statement() -> None:
    """169 rows all asserting the same classification must render once.

    Rendering per row would emit the fact 169 times with 124 citations behind
    each copy -- the 24,428-row multiplication arriving through the prose instead
    of the row count, which graph-load.md:225 names as this step's risk.
    """
    chunks = [f"c{i}" for i in range(CLASSIFIED_CHUNKS)]
    rows = [
        {
            "s": node("high risk ai system", "high-risk AI system", ["SystemType"]),
            "rc": node("high risk", "high-risk", ["RiskCategory"]),
            "o": node(f"duty {i}", labels=["Obligation"]),
            "a": node(f"aia art. {i}", f"AIA Art. {i}", ["Article"]),
            "classified_chunks": chunks,
            "applies_chunks": [f"a{i}"],
            "imposes_chunks": [f"i{i}"],
        }
        for i in range(SYSTEM_ROWS)
    ]
    labels = {c: f"L{c}" for c in chunks}
    labels |= {f"a{i}": f"La{i}" for i in range(SYSTEM_ROWS)}
    labels |= {f"i{i}": f"Li{i}" for i in range(SYSTEM_ROWS)}

    docs = path_to_prose(rows, "obligations_for_system", labels=labels)
    classification = [d for d in docs if "is classified as" in d.text]
    assert len(classification) == 1
    assert len(docs) == 1 + 2 * SYSTEM_ROWS


def test_provenance_is_capped_and_says_how_many_it_capped() -> None:
    """A silent truncation would be a lie about the evidence; a 124-entry
    citation list is not evidence either."""
    chunks = [f"c{i:03d}" for i in range(CLASSIFIED_CHUNKS)]
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": chunks}]
    labels = {c: f"L{c}" for c in chunks}
    docs = path_to_prose(rows, "definition_of", labels=labels, max_provenance=3)
    assert f"+{CLASSIFIED_CHUNKS - 3} more" in docs[0].text
    named = [c for c in chunks if f"L{c}" in docs[0].text]
    assert named == chunks[:3], "the three named must be the first three, deterministically"


def test_an_empty_optional_leg_renders_nothing() -> None:
    """Only 4 of 216 enforced obligations carry PENALIZED_UNDER, so an empty
    `penalty_chunks` is the common case. It must produce no statement rather than
    a statement citing nothing."""
    rows = [
        {
            "o": node("keep records", labels=["Obligation"]),
            "auth": node("market surveillance authority", labels=["Authority"]),
            "p": None,
            "enforced_chunks": ["c1"],
            "penalty_chunks": [],
        }
    ]
    docs = path_to_prose(rows, "enforcement_chain", labels={"c1": "AIA Art. 74(1)"})
    assert len(docs) == 1
    assert "penalised" not in docs[0].text


def test_a_null_map_provenance_entry_is_dropped() -> None:
    """`[{chunk: null}]` is the shape rule 1 of the template library exists to
    prevent -- a fake citation that passes every `if provenance:` check."""
    rows = [
        {
            "a": node("aia art. 2(7)", "AIA Art. 2(7)", ["Article"]),
            "b": node("GDPR", "GDPR", ["Regulation"]),
            "provenance": [{"chunk": None, "derived": None, "outbound": True}],
        }
    ]
    assert path_to_prose(rows, "cross_regulation", labels={}) == []


def test_direction_comes_from_outbound_and_is_not_guessed() -> None:
    """`cross_regulation` matches undirected, so without `outbound` the prose
    would have to guess which way the citation ran."""
    a = node("aia art. 2(7)", "AIA Art. 2(7)", ["Article"])
    b = node("GDPR", "GDPR", ["Regulation"])
    out = path_to_prose(
        [{"a": a, "b": b, "provenance": [{"chunk": "c1", "derived": False, "outbound": True}]}],
        "cross_regulation", labels={"c1": "AIA Art. 2(7)"},
    )
    back = path_to_prose(
        [{"a": a, "b": b, "provenance": [{"chunk": "c1", "derived": False, "outbound": False}]}],
        "cross_regulation", labels={"c1": "AIA Art. 2(7)"},
    )
    assert out[0].text.startswith("AIA Art. 2(7) interacts with GDPR.")
    assert back[0].text.startswith("GDPR interacts with AIA Art. 2(7).")


def test_a_derived_edge_is_flagged_on_the_document() -> None:
    """ADR-0010 tagged the 22 bridges so a consumer could tell an inferred edge
    from an asserted one. This is that consumer."""
    rows = [
        {
            "a": node("aia annex viii", "AIA Annex VIII", ["Annex"]),
            "b": node("gdpr art. 35", "GDPR Art. 35", ["Article"]),
            "provenance": [{"chunk": "c1", "derived": True, "outbound": True}],
        }
    ]
    docs = path_to_prose(rows, "cross_regulation", labels={"c1": "AIA Annex VIII(C)(5)"})
    assert docs[0].derived is True


def test_an_asserted_edge_is_not_flagged_derived() -> None:
    rows = [
        {
            "a": node("aia art. 2(7)", "AIA Art. 2(7)", ["Article"]),
            "b": node("GDPR", "GDPR", ["Regulation"]),
            "provenance": [{"chunk": "c1", "derived": False, "outbound": True}],
        }
    ]
    docs = path_to_prose(rows, "cross_regulation", labels={"c1": "AIA Art. 2(7)"})
    assert docs[0].derived is False


@pytest.mark.parametrize("annex", ["AIA Annex VIII", "AIA Annex XI"])
def test_an_ambiguous_annex_statement_carries_the_caveat(annex: str) -> None:
    """The deferred defect is flagged, not fixed. Annex VIII's three "point 1"
    entries are registration duties attaching to different actors, and the
    extractor was never given `section` (failure-notes.md:1055-1058, still OPEN)."""
    rows = [{"t": node("status of the ai system"), "a": node(annex.lower(), annex, ["Annex"]),
             "defined_chunks": ["c1"]}]
    docs = path_to_prose(rows, "definition_of", labels={"c1": f"{annex}(A)(7)"})
    assert ANNEX_CAVEAT in docs[0].text


def test_an_unambiguous_annex_carries_no_caveat() -> None:
    """Annex IV and Annex VII are not sectioned; caveating them would train a
    reader to ignore the caveat."""
    rows = [{"t": node("computational resources"), "a": node("aia annex iv", "AIA Annex IV", ["Annex"]),
             "defined_chunks": ["c1"]}]
    docs = path_to_prose(rows, "definition_of", labels={"c1": "AIA Annex IV(2)"})
    assert ANNEX_CAVEAT not in docs[0].text


def test_a_chunk_with_no_label_raises_rather_than_shipping_uncitable() -> None:
    """ADR-0011 makes `citation_label` required on GRAPH documents and names this
    function as what keeps that true by construction. Step 6 validates citations
    against a retrieved set, so an unlabelled statement is a rejection deferred to
    where the cause is no longer visible."""
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": ["missing"]}]
    with pytest.raises(ProseError, match="no citation_label"):
        path_to_prose(rows, "definition_of", labels={})


def test_a_graph_document_carries_no_similarity_score() -> None:
    """`ContextDoc.score` is cosine similarity on the vector path and Cohere's
    relevance score after rerank. A graph statement has neither, and nothing may
    sort across sources on that field (retriever.py:26-30)."""
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": ["c1"]}]
    docs = path_to_prose(rows, "definition_of", labels={"c1": "L"})
    assert docs[0].score is None
    assert docs[0].source == "GRAPH"


def test_provenance_equals_the_chunks_the_statement_actually_named() -> None:
    """ADR-0014's widening. Before it, this list was computed here, rendered into
    the text as labels, and then dropped -- `chunk_id` kept one of it and nothing
    else survived the boundary, so ADR-0013's 24-of-32 was measured against a set
    `Citation` could never carry."""
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": ["c1", "c2", "c3"]}]
    labels = {"c1": "L1", "c2": "L2", "c3": "L3"}
    docs = path_to_prose(rows, "definition_of", labels=labels)
    assert docs[0].provenance == ["c1", "c2", "c3"]
    for chunk in docs[0].provenance:
        assert labels[chunk] in docs[0].text


def test_provenance_never_exceeds_max_provenance() -> None:
    """Capped at what was *shown*, never at the full 124. Citing 121 chunks a
    reader was never shown is not evidence."""
    chunks = [f"c{i:03d}" for i in range(CLASSIFIED_CHUNKS)]
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": chunks}]
    labels = {c: f"L{c}" for c in chunks}
    docs = path_to_prose(rows, "definition_of", labels=labels, max_provenance=3)
    assert docs[0].provenance == chunks[:3]
    assert f"+{CLASSIFIED_CHUNKS - 3} more" in docs[0].text


def test_the_first_provenance_entry_is_the_documents_own_chunk_id() -> None:
    """`chunk_id` is `provenance[0]`, so a consumer that ignores the fan-out and
    reads `chunk_id` alone gets a chunk that is genuinely in the list."""
    chunks = [f"c{i}" for i in range(5)]
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": chunks}]
    docs = path_to_prose(rows, "definition_of", labels={c: f"L{c}" for c in chunks})
    assert docs[0].provenance[0] == docs[0].chunk_id


def test_a_single_chunk_statement_has_a_one_element_provenance() -> None:
    rows = [{"t": node("x"), "a": node("y"), "defined_chunks": ["only"]}]
    docs = path_to_prose(rows, "definition_of", labels={"only": "L"})
    assert docs[0].provenance == ["only"]


def test_a_merged_statement_carries_the_merged_provenance() -> None:
    """Two rows rendering the same statement union their chunks, and the shown
    list has to come from the union rather than from whichever row was first."""
    rows = [
        {"t": node("x"), "a": node("y"), "defined_chunks": ["c2"]},
        {"t": node("x"), "a": node("y"), "defined_chunks": ["c1"]},
    ]
    docs = path_to_prose(rows, "definition_of", labels={"c1": "L1", "c2": "L2"})
    assert len(docs) == 1
    assert docs[0].provenance == ["c2", "c1"]


def test_a_passage_sourced_document_carries_no_provenance() -> None:
    """`provenance == []` is how a consumer tells the two sources apart without
    reading `source`. A corpus chunk asserts itself; a one-element list saying so
    would invite a caller to treat the two symmetrically."""
    from src.query.retriever import ContextDoc as _  # noqa: F401 -- same model

    from src.schemas import ContextDoc

    doc = ContextDoc(
        chunk_id="aia-art9-para1", text="statute", citation_label="AIA Art. 9(1)",
        source="PASSAGE", score=0.9,
    )
    assert doc.provenance == []


def test_an_unknown_template_has_no_renderer() -> None:
    with pytest.raises(ProseError, match="has no renderer"):
        path_to_prose([], "obligations_for_everything", labels={})


def test_path_between_renders_one_statement_per_hop() -> None:
    rows = [
        {
            "p": {
                "nodes": [node("deployer", "deployer"), node("gdpr art. 35", "GDPR Art. 35"),
                          node("GDPR", "GDPR")],
                "types": ["APPLIES_TO", "INTERACTS_WITH"],
                "hops": 2,
            },
            "chunks": ["c1", "c2"],
            "types": ["APPLIES_TO", "INTERACTS_WITH"],
            "derived_flags": [False, True],
        }
    ]
    docs = path_to_prose(rows, "path_between", labels={"c1": "L1", "c2": "L2"})
    assert len(docs) == 2
    assert docs[0].derived is False and docs[1].derived is True
    assert "applies to" in docs[0].text


# --------------------------------------------------------------------------
# Against a live database
# --------------------------------------------------------------------------

def test_the_live_hot_fact_renders_once(loaded, indexed) -> None:
    """The pure test builds 169 synthetic rows; this one uses the real 169."""
    rows = run_template("obligations_for_system", {"system_type": "high risk ai system"}, loaded)
    assert len(rows) == SYSTEM_ROWS
    labels = label_map([c for r in rows for c in provenance_of(r)], indexed)
    docs = path_to_prose(rows, "obligations_for_system", labels=labels)
    classification = [d for d in docs if "is classified as" in d.text]
    assert len(classification) == 1
    assert "+" in classification[0].text and "more" in classification[0].text


def test_every_provenance_chunk_resolves_to_a_citation_label(loaded, indexed) -> None:
    """Zero dangling references across the two stores, over every template the
    selector can actually emit. A miss here is a statement that cannot be cited."""
    from src.query import template_selector as ts

    index = ts.build_index()
    wanted: set[str] = set()
    for row in ts.load_questions():
        for call in ts.select_by_rules(row["question"], index).plan:
            for r in run_template(call.template, call.params, loaded):
                wanted.update(provenance_of(r))
    labels = label_map(wanted, indexed)
    assert wanted, "the selector emitted no provenance at all"
    assert not wanted - set(labels), f"{len(wanted - set(labels))} chunk ids have no citation_label"


def test_a_live_derived_bridge_is_identifiable_in_the_output(loaded, indexed) -> None:
    """ADR-0010's flag, surviving all the way into a rendered statement."""
    with loaded.session() as session:
        anchor = session.run(
            "MATCH (a:Entity)-[r:INTERACTS_WITH]->(:Entity) WHERE r.derived "
            "RETURN a.canonical_name AS name ORDER BY name LIMIT 1"
        ).single()["name"]
    rows = run_template("cross_regulation", {"article": anchor}, loaded)
    labels = label_map([c for r in rows for c in provenance_of(r)], indexed)
    docs = path_to_prose(rows, "cross_regulation", labels=labels)
    assert any(d.derived for d in docs), f"no derived statement from {anchor!r}"
    assert any(not d.derived for d in docs), "asserted and derived must be distinguishable"
