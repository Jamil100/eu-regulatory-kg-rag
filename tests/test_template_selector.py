"""Template selection: the two arms, their contract, and the measurement.

Almost all of this runs without a database. The tables the selector dispatches on
are derived from the Cypher and from `ALLOWED_ENDPOINTS`, so the drift tests are
pure; the artifact carries what the graph returned, so the measurement tests are
pure too. Only the end-to-end handshake and the cross-store checks need
containers.
"""

from __future__ import annotations

import json
import re

import pytest

from src.query import router, template_selector as ts
from src.query.cypher_templates import TEMPLATES, TEMPLATE_PARAMS
from src.query.graph_query import run_template

# Measured 2026-08-04 by `python -m src.query.template_selector --eval --refresh`.
# A regression below these is a defect, not a re-tuning.
RULES_GOLD_HITS = 24
GOLD_TOTAL = 32
SCORED_ROWS = 9
BEST_CONSTANT = 8  # always-obligations_for_system, by edge-intersection

# R7B's figure is a SINGLE SAMPLE and is asserted as a bound, not an equality.
# Two sweeps of the same 23 questions at `temperature=0, seed=42` returned 16 and
# then 14 gold hits, with the plan itself differing on several rows -- Cohere's
# `seed` is best-effort, not a guarantee. The rules arm has
# `test_the_rules_arm_still_reproduces_the_artifact` asserting byte-exact
# reproduction; the model arm cannot have that test, which is itself part of what
# ADR-0013 weighs. The committed artifact is the record of the run the ADR quotes.
R7B_GOLD_HITS_CEILING = 20
DOCS_ANNEX_CAVEATED = 6

# Measured before either arm existed. `always-obligations_for_system` scoring 8 of
# 9 against a ceiling of 9 of 9 is why gold yield is the headline and
# edge-intersection is reported only beside its constants.
CEILING = 9


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return ts.load_questions()


@pytest.fixture(scope="module")
def artifact() -> list[dict]:
    return ts.load_artifact()


@pytest.fixture(scope="module")
def board(rows, artifact) -> dict:
    return ts.scoreboard(rows, artifact)


# --------------------------------------------------------------------------
# The dispatch tables must not drift from the Cypher they describe
# --------------------------------------------------------------------------

_EDGE_RE = re.compile(r"\[\s*\w*\s*:\s*([A-Z_]+)")
_PINNED_RE = re.compile(r"\(\s*\w+\s*:\s*(\w+)\s*\{\s*canonical_name\s*:\s*\$(\w+)\s*\}")


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_template_edges_match_the_cypher(name: str) -> None:
    """`TEMPLATE_EDGES` is the only bridge from a template to `ontology_edges`.

    If a template gains a leg and this table does not, the edge measurement
    silently describes a query that no longer exists.
    """
    from src.schemas import RELATION_TYPES

    found = frozenset(t for t in _EDGE_RE.findall(TEMPLATES[name]) if t in RELATION_TYPES)
    assert ts.TEMPLATE_EDGES[name] == found, f"{name}: declared {ts.TEMPLATE_EDGES[name]}, Cypher has {found}"


def test_path_between_is_the_only_untyped_template() -> None:
    """It is also the only cover for REFERENCES, LISTED_IN, SETS_PENALTY,
    EXEMPT_FROM, PERMITS and GRANTS -- 6 of the ontology's 13 relation types."""
    untyped = {n for n, e in ts.TEMPLATE_EDGES.items() if not e}
    assert untyped == {"path_between"}


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_pinned_anchor_labels_match_the_cypher(name: str) -> None:
    """A template that pins a label accepts exactly that label.

    `obligations_for_role` filters on `:ActorRole`; declaring anything wider here
    would let the selector fill it with a type that returns zero rows.
    """
    for label, param in _PINNED_RE.findall(TEMPLATES[name]):
        if label == "Entity":  # open head, constrained by ALLOWED_ENDPOINTS instead
            continue
        assert ts.TEMPLATE_ANCHORS[name][param] == frozenset({label})


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_anchors_cover_exactly_the_declared_parameters(name: str) -> None:
    assert set(ts.TEMPLATE_ANCHORS[name]) == set(TEMPLATE_PARAMS[name])


def test_the_anchor_type_disagreement_is_exactly_as_recorded() -> None:
    """Six types fill a declared parameter but are not router anchors.

    `router.ANCHOR_TYPES` says "none is a parameter any template declares" of
    Regulation, DefinedTerm, Authority and Penalty. That is true of the three
    typed templates and false of `definition_of`, whose head is `(t:Entity)`.

    The router is deliberately NOT changed: ADR-0012's adopted 21 of 22 was
    measured with ANCHOR_TYPES as it stands, and editing it re-measures Step 3
    silently. This test pins the difference so it cannot drift unnoticed.
    """
    assert ts.SELECTABLE_TYPES - router.ANCHOR_TYPES == ts.ANCHOR_TYPE_DISAGREEMENT
    assert not router.ANCHOR_TYPES - ts.SELECTABLE_TYPES, (
        "an ANCHOR_TYPE that fills no declared parameter would be a router that "
        "routes to a graph path with nothing to run"
    )


# --------------------------------------------------------------------------
# ADR-0002 -- nothing reaches a driver unvalidated
# --------------------------------------------------------------------------

def test_every_rules_call_over_the_eval_set_is_valid(rows) -> None:
    """Asserted over all 23 questions, not spot-checked.

    `TemplateCall.__post_init__` calls `validate()`, so this passing means no
    question can produce a call that would reach `session.run` unchecked.
    """
    index = ts.build_index()
    for row in rows:
        for call in ts.select_by_rules(row["question"], index).plan:
            assert call.template in TEMPLATES
            assert set(call.params) == set(TEMPLATE_PARAMS[call.template])


def test_a_call_with_an_unknown_template_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="is not a template"):
        ts.TemplateCall(template="obligations_for_everything", params={}, rule="test")


def test_an_injection_as_a_template_name_is_rejected() -> None:
    """ADR-0002's whole point, at the one boundary a model can reach through."""
    with pytest.raises(ValueError, match="is not a template"):
        ts.TemplateCall(
            template="'; MATCH (n) DETACH DELETE n //", params={"role": "x"}, rule="test"
        )


def test_an_undeclared_extra_parameter_is_rejected() -> None:
    """R7B produced exactly this twice on the eval set -- `obligations_for_role
    role=importer system_type=high-risk AI system`. An extra parameter is silently
    ignored by the driver, so the query would have run with an unbound `$role`."""
    with pytest.raises(ValueError, match="unexpected="):
        ts.TemplateCall(
            template="obligations_for_role",
            params={"role": "deployer", "system_type": "x"},
            rule="test",
        )


# --------------------------------------------------------------------------
# Parsing the model's answer
# --------------------------------------------------------------------------

def test_a_multi_word_parameter_value_survives_parsing() -> None:
    calls, rejected = ts.parse_plan("obligations_for_system system_type=remote biometric system")
    assert not rejected
    assert calls[0].params == {"system_type": "remote biometric system"}


def test_none_is_an_empty_plan_and_not_an_error() -> None:
    calls, rejected = ts.parse_plan("none")
    assert calls == [] and rejected == []


def test_an_invalid_line_is_rejected_and_never_repaired() -> None:
    """A selector that emits garbage is a different fact than one that picks a
    default template. Coercing the first into the second deletes the difference."""
    calls, rejected = ts.parse_plan("obligations_for_role role=deployer article=26")
    assert calls == []
    assert len(rejected) == 1 and "unexpected=" in rejected[0]


def test_prose_after_a_valid_line_does_not_take_the_valid_line_with_it() -> None:
    """R7B did this on `3h-001`: one good plan line, then it started answering."""
    calls, rejected = ts.parse_plan(
        "path_between entity_a=provider entity_b=notified body\n"
        "\n"
        "The answer is:\n"
        "The national competent authority designated by the Member State."
    )
    assert [c.template for c in calls] == ["path_between"]
    assert rejected


def test_a_plan_is_capped(rows) -> None:
    long_answer = "\n".join(f"definition_of term=thing{i}" for i in range(10))
    calls, _ = ts.parse_plan(long_answer)
    assert len(calls) <= ts.MAX_CALLS


def test_no_few_shot_example_is_an_eval_question(rows) -> None:
    """The leakage guard, asserted against the live eval set rather than a comment."""
    questions = {r["question"].strip().lower() for r in rows}
    for example, _ in ts.FEW_SHOT:
        assert example.strip().lower() not in questions


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

def test_a_definitional_question_reaches_definition_of() -> None:
    result = ts.select_by_rules("What does 'provider' mean in the AI Act?")
    assert "definition_of" in [c.template for c in result.plan]


def test_a_question_with_no_linked_entity_produces_no_plan() -> None:
    """The graph path cannot answer what it cannot anchor. An empty plan is the
    honest output; a default template would be a query returning rows about
    something the question never asked."""
    result = ts.select_by_rules("What is the capital of France?")
    assert result.plan == []
    assert result.rule == "S0-none"


def test_the_role_anchor_is_a_canonical_name_usable_as_a_parameter() -> None:
    result = ts.select_by_rules(
        "List every obligation the AI Act places on deployers of high-risk AI systems."
    )
    params = {c.template: c.params for c in result.plan}
    assert params["obligations_for_role"] == {"role": "deployer"}


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------

def test_the_artifact_covers_every_question_for_both_arms(rows, artifact) -> None:
    for arm in ts.ARMS:
        assert {e["id"] for e in artifact if e["selector"] == arm} == {r["id"] for r in rows}


def test_the_artifact_gold_matches_the_eval_set(rows, artifact) -> None:
    """If the eval set is relabelled and the sweep is not re-run, every number in
    ADR-0013 silently becomes wrong."""
    gold = {r["id"]: sorted(r["source_chunk_ids"]) for r in rows}
    for entry in artifact:
        assert entry["gold"] == gold[entry["id"]], entry["id"]


def test_the_rules_arm_still_reproduces_the_artifact(artifact) -> None:
    """The rules are deterministic and free, so any drift is a defect here, now."""
    index = ts.build_index()
    by_id = {e["id"]: e for e in artifact if e["selector"] == "rules"}
    for row in ts.load_questions():
        expected = [(c["template"], c["params"]) for c in by_id[row["id"]]["plan"]]
        got = [(c.template, c.params) for c in ts.select_by_rules(row["question"], index).plan]
        assert got == expected, row["id"]


def test_oos_002_never_reaches_a_selector(rows) -> None:
    """The trap the eval set documents on itself. `oos-002` declares
    `ontology_edges: ["IMPOSES"]` with zero gold chunks, and its own
    `route_reason` says a selector deriving from declared edges "would send it to
    graph". It routes `vector`, so it must not be in the scored denominator."""
    row = next(r for r in rows if r["id"] == "oos-002")
    assert row["route"] == "vector"
    assert "oos-002" not in ts.scoreboard(rows, ts.load_artifact())["_scored_ids"]


def test_the_scored_set_is_the_routed_set_minus_expected_fail(rows, board) -> None:
    assert len(board["_scored_ids"]) == SCORED_ROWS
    assert "3h-002" not in board["_scored_ids"], "carries expected_fail (ADR-0007)"


def test_rules_gold_yield_is_what_adr_0013_claims(board) -> None:
    assert board["rules"]["gold_hit"] == RULES_GOLD_HITS
    assert board["rules"]["gold_total"] == GOLD_TOTAL


def test_r7b_loses_to_the_rules_by_a_margin_no_resample_closes(board) -> None:
    """Asserted as a bound because the model arm is not reproducible.

    Two sweeps at `temperature=0, seed=42` gave 16 then 14 gold hits. The gap to
    the rules' 24 is wider than that spread, which is the claim ADR-0013 rests
    on -- not the exact figure of either sample.
    """
    assert board["r7b"]["gold_hit"] <= R7B_GOLD_HITS_CEILING
    assert board["rules"]["gold_hit"] > board["r7b"]["gold_hit"]


def test_the_graph_path_answers_every_row_it_is_given(board) -> None:
    """Reach, counted in statements rather than rows."""
    assert board["rules"]["rows_answerable"] == SCORED_ROWS
    assert board["rules"]["docs_rendered"] > 0


def test_the_annex_caveat_fires_on_the_eval_set(board) -> None:
    """The deferred defect is flagged in real output, not just in a unit test."""
    assert board["rules"]["docs_annex_caveated"] == DOCS_ANNEX_CAVEATED


def test_the_derived_flag_is_inert_on_this_eval_set(board) -> None:
    """It works -- `test_a_live_derived_bridge_is_identifiable_in_the_output`
    proves that against the live graph -- but no selected plan on these 23
    questions traverses one of ADR-0010's 22 bridges. Reported as inert rather
    than counted as a feature that fired, the same treatment ADR-0012 gave the
    router's R1. When a question finally reaches one, this test fails and the
    number has to be written down instead of absorbed.
    """
    assert board["rules"]["docs_derived"] == 0, (
        "a derived bridge is now reachable from the eval set -- update ADR-0013 "
        "and docs/metrics/query-path.md rather than editing this number"
    )


def test_rules_reaches_the_oracle(board) -> None:
    """The deterministic arm matches the best single call per row, chosen with the
    gold visible. Selection is therefore NOT the binding constraint on this eval
    set -- what the templates can reach at all is. Same shape as Step 4's finding
    that ranking rather than retrieval bound the vector path."""
    assert board["rules"]["gold_hit"] == ts.ORACLE_GOLD_HITS
    assert ts.ORACLE_GOLD_TOTAL == GOLD_TOTAL


def test_the_edge_metric_is_beaten_by_a_constant(board) -> None:
    """Reported, not adopted. A metric whose ceiling is 9 of 9 and whose best
    constant is 8 of 9 has about one row of discriminating power -- measured
    before either arm was written, which is the only reason it is a caveat in the
    docs rather than a headline in them."""
    assert max(board["_constants"].values()) == BEST_CONSTANT
    assert board["rules"]["edge_hit"] <= CEILING


def test_r7b_produced_parameter_values_that_match_no_node(board) -> None:
    """The finding of ADR-0013, and it is not about template choice.

    `validate()` checks parameter NAMES; the graph matches parameter VALUES. R7B
    picked reasonable templates and filled them with display-form English
    (`high-risk AI system`, `gdpr`) where the graph keys are `high risk ai
    system` and `GDPR`, so the calls validated and returned nothing.
    """
    assert board["r7b"]["empty_calls"] > board["rules"]["empty_calls"]
    assert board["r7b"]["empty_calls"] >= 4


def test_rules_cost_nothing(board) -> None:
    assert board["rules"]["cost_usd"] == 0.0


# --------------------------------------------------------------------------
# Against a live database
# --------------------------------------------------------------------------

def test_a_question_selects_a_template_that_returns_the_graph_load_anchor(loaded) -> None:
    """The Step 2 -> Step 5 handshake, end to end from a real question.

    `ag-001`'s own wording links to `deployer`, selects `obligations_for_role`,
    and returns exactly the 60 rows `docs/metrics/graph-load.md` anchors on --
    reached from the question rather than from a hand-typed parameter.
    """
    question = next(
        r["question"] for r in ts.load_questions() if r["id"] == "ag-001"
    )
    plan = ts.select_by_rules(question).plan
    call = next(c for c in plan if c.template == "obligations_for_role")
    assert call.params == {"role": "deployer"}
    assert len(run_template(call.template, call.params, loaded)) == 60


def test_every_selected_call_executes(loaded, rows) -> None:
    """A validated call must at least run. Returning zero rows is a fact about
    the question; raising is a broken query."""
    index = ts.build_index()
    for row in rows:
        for call in ts.select_by_rules(row["question"], index).plan:
            run_template(call.template, call.params, loaded)
