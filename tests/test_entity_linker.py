"""What a question links to, and the traps that make a linked key useless.

Almost all of this runs without a database: the linker is a pure derivation over
the same resolver the graph was built from, so `graph` (the pure fixture) is
enough to assert that every key it emits is a real node.

The regressions here are the specific ways a linker breaks *while still returning
results* -- a lowercased instrument, a truncated span, an unbalanced paren. Each
one produces a plausible-looking list and zero rows from the template that
consumes it, which is the failure mode this project keeps rediscovering.
"""

from __future__ import annotations

import json

import pytest

from src.ingest.entity_resolution import normalize, resolve_corpus
from src.query.entity_linker import (
    QUESTIONS,
    _surfaces,
    build_index,
    link,
    link_detailed,
)

# Measured 2026-08-03 by `python -m src.query.entity_linker --eval`. A regression
# below this is a defect, not a re-tuning: every question reaching at least one
# node is what makes the graph route available at all.
ROWS_LINKING = 23


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------
# The keys have to be usable as Cypher parameters
# --------------------------------------------------------------------------

def test_every_linked_key_is_a_real_node(rows, graph):
    """The whole contract. A key that names no node returns rows from nothing."""
    nodes = {node["canonical_name"] for node in graph["nodes"]}
    for row in rows:
        unknown = [name for name in link(row["question"]) if name not in nodes]
        assert not unknown, f"{row['id']} linked non-existent nodes: {unknown}"


def test_no_linked_key_has_an_unbalanced_paren(rows):
    """ADR-0009's Correction, from the query side.

    A blind `.strip("()")` in the resolver left 1,026 of 3,366 node keys reading
    `aia art. 1(1`. `normalize()` is bracket-aware and the linker must not undo
    that by trimming spans itself.
    """
    for row in rows:
        for name in link(row["question"]):
            assert name.count("(") == name.count(")"), f"{row['id']}: {name!r}"
            assert name.count("[") == name.count("]"), f"{row['id']}: {name!r}"


def test_gdpr_links_as_uppercase():
    """`ABBREVIATIONS` maps `gdpr` -> `GDPR`, and the graph MERGEd on the latter.

    A linker that lowercased unconditionally would lose every instrument node
    silently -- `tests/test_graph_writer.py:273` passes `"GDPR"` as a live
    template parameter, so the case is load-bearing, not cosmetic.
    """
    assert "GDPR" in link("Under the GDPR, is processing biometric data permitted?")
    assert "gdpr" not in link("Under the GDPR, is processing biometric data permitted?")


def test_the_ai_act_links_under_both_of_its_names():
    for question in ("What does the AI Act require?", "What does the AIA require?"):
        assert "AI Act" in link(question), question


# --------------------------------------------------------------------------
# Span handling -- the two defects found on first contact with real questions
# --------------------------------------------------------------------------

def test_a_trailing_question_mark_does_not_truncate_a_span():
    """`_trim` peels " .,;:" and has never had to handle "?" -- no node ends in one.

    Before the linker stripped it, `"...require a notified body?"` failed to match
    `notified body`, fell back to the bare token `notified`, and linked the
    obligation `notify use of real time remote biometric identification system`.
    It returned a result, which is why it needs a test rather than a comment.
    """
    linked = link("Does it require a notified body?")
    assert "notified body" in linked
    assert not any(name.startswith("notify use of") for name in linked)


def test_a_possessive_still_reaches_the_instrument():
    """`normalize()` deletes apostrophes, turning `GDPR's` into `gdprs`.

    No plural fold covers that -- `gdprs` is not an attested surface -- so
    `ag-003` linked no instrument at all until the possessive was stripped first.
    """
    assert "GDPR" in link("Which infringements fall under the GDPR's highest fine tier?")


def test_the_possessive_form_is_only_ever_a_fallback():
    """Tried after the plain form, so it cannot cost a match that already worked.

    The ordering is the guarantee, not an accident of the corpus. 122 surfaces
    end in `s` and would land on a *different* node if the trailing letter came
    off, and `normalize()` collapses the corpus's own possessives the same way
    (`"controller's representative"` is stored as `controllers representative`).
    Stripping first would silently reroute those.
    """
    for span in ("the GDPR's", "deployer's", "data subjects'"):
        assert _surfaces(span)[0] == normalize(span), span


def test_a_collapsed_possessive_still_links_by_its_plain_form():
    """The corpus keeps possessives with the apostrophe deleted, not stripped."""
    assert "workers representative" in link("what may a workers' representative do")
    assert "controllers representative" in link("the controller's representative")


# --------------------------------------------------------------------------
# Resolution rules
# --------------------------------------------------------------------------

def test_plural_folds_to_the_attested_singular():
    """`ag-001` asks about "deployers"; every template takes `deployer`."""
    assert link("obligations on deployers of high-risk AI systems") == [
        "deployer",
        "high risk ai system",
    ]


def test_the_plural_fold_is_the_resolvers_rule_not_a_new_one():
    """Widened to alias surfaces, but still `_plural_map`: both forms attested.

    The breakages a grammatical rule would cause stay unmerged because their
    singulars never occur in the corpus.
    """
    merges = build_index().plural_merges
    assert merges["deployers"] == "deployer"
    for unattested in ("premises", "analysis", "business", "bias", "practices"):
        assert unattested not in merges


def test_no_fold_shadows_a_node_that_owns_its_name():
    """A plural that is itself a node must keep its own identity."""
    index = build_index()
    shadowed = [p for p in index.plural_merges if p in index.canonical]
    assert not shadowed, f"fold would swallow real nodes: {shadowed}"


def test_longest_match_wins_and_does_not_also_emit_its_parts():
    linked = link("a high-risk AI system")
    assert linked == ["high risk ai system"]


def test_a_node_is_linked_once_however_often_it_is_named():
    """`ag-002` names the deployer twice; one template call is enough."""
    linked = link(
        "Which of a deployer's obligations are modified when the deployer "
        "is a financial institution?"
    )
    assert linked.count("deployer") == 1


def test_linked_types_agree_with_the_resolver(rows):
    """Step 5 dispatches typed templates on this; a wrong label returns 0 rows."""
    types = resolve_corpus()["types"]
    for row in rows:
        for entity in link_detailed(row["question"]):
            assert entity.type == types[entity.canonical_name]


def test_display_name_is_carried_and_is_not_the_key():
    """ADR-0009's Correction: prose wants `high-risk`, keys want `high risk`."""
    entity = next(
        e for e in link_detailed("a high-risk AI system") if e.canonical_name
    )
    assert entity.display_name
    assert entity.display_name != entity.canonical_name


# --------------------------------------------------------------------------
# Behaviour under the eval set
# --------------------------------------------------------------------------

def test_every_question_links_at_least_one_node(rows):
    """`ag-001`'s canary in code: a question linking nothing has no graph route."""
    unlinked = [row["id"] for row in rows if not link(row["question"])]
    assert not unlinked, f"questions reaching no node: {unlinked}"
    assert sum(bool(link(row["question"])) for row in rows) == ROWS_LINKING


def test_link_is_deterministic_and_order_stable(rows):
    """Template parameters come from this; an order that moved between runs
    would make a graph answer irreproducible."""
    for row in rows:
        assert link(row["question"]) == link(row["question"])


def test_a_question_with_no_entities_links_nothing():
    for question in ("", "   ", "How much and by when?"):
        assert link(question) == []


def test_the_index_is_built_once():
    """`resolve_corpus()` re-parses 1.8 MB per call and `/ask` is a request path."""
    assert build_index() is build_index()
    assert build_index.cache_info().hits >= 1


# --------------------------------------------------------------------------
# The gold source, cross-checked against the database
# --------------------------------------------------------------------------

def test_pg_entity_ids_match_the_resolver(indexed):
    """`schema.sql:50-53` promises `entity_ids` holds the Neo4j MERGE key verbatim.

    The measurement defaults to the resolver so it runs without containers; this
    is what licenses that shortcut.
    """
    from src.query.entity_linker import compare_gold_sources

    agreement = compare_gold_sources()
    assert not agreement["disagreeing"], (
        f"{len(agreement['disagreeing'])} chunks disagree between the resolver "
        f"and pgvector: {agreement['disagreeing'][:5]}"
    )


def test_a_linked_role_actually_returns_rows(loaded):
    """End to end: what the linker emits is what the template consumes.

    `ag-001` is the row this exists for -- if `deployer` resolved to a name the
    graph does not hold, the template returns zero rows and the aggregation
    stratum silently answers from the vector path alone.
    """
    from src.query.graph_query import run_template

    linked = link("List the main obligations the AI Act places on deployers "
                  "of high-risk AI systems.")
    assert "deployer" in linked
    assert len(run_template("obligations_for_role", {"role": "deployer"}, loaded)) == 60
