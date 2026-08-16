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

# Measured by `python -m src.query.template_selector --eval --refresh`.
# A regression below these is a defect, not a re-tuning.
#
# RE-MEASURED 2026-08-15 ON THE 100-ROW EVAL SET. The 23-row values are kept
# beside the new ones because the comparison is the point. The scored set grew
# from 9 routed rows to 51, so every absolute here had to move; what did not have
# to move is ADR-0013's conclusion, and it did not.
#
#   2026-08-04, 9 scored rows:  RULES 24/32  R7B 14/32  ORACLE 24  CONSTANT 8
RULES_GOLD_HITS = 61
GOLD_TOTAL = 148
SCORED_ROWS = 51
BEST_CONSTANT = 40  # always-obligations_for_system, by edge-intersection

# The pre-registration, published in ADR-0013 and docs/metrics/query-path.md.
#
# These live HERE, in the test, and not in `src/` -- which is the whole point of
# the follow-up that added them. For one commit they were hand-typed constants in
# `template_selector.py` that nothing recomputed, so `test_rules_reaches_the_oracle`
# was asserting `24 == 24` with the right-hand side typed by me. `scoreboard()` now
# computes all three from the artifact, and these are the assertions against it --
# the same arrangement `tests/test_reranker.py` has with CAPS and ORACLE.
ORACLE = 62  # best single (template, anchor) per row, chosen with the gold visible
CEILING_EDGE = 47  # a template traverses a declared edge, of 51 scored
CEILING_ANCHOR = 51  # the linker can fill that template, of 51 scored

# R7B's figure is a SINGLE SAMPLE and is asserted as a bound, not an equality.
# Two sweeps of the same 23 questions at `temperature=0, seed=42` returned 16 and
# then 14 gold hits, with the plan itself differing on several rows -- Cohere's
# `seed` is best-effort, not a guarantee. The rules arm has
# `test_the_rules_arm_still_reproduces_the_artifact` asserting byte-exact
# reproduction; the model arm cannot have that test, which is itself part of what
# ADR-0013 weighs. The committed artifact is the record of the run the ADR quotes.
R7B_GOLD_HITS_CEILING = 37
DOCS_ANNEX_CAVEATED = 27  # 6 at 23 rows; the deferred annex `section` defect is
                          # flagged in real output on 27 statements at 100 rows

# Measured before either arm existed. `always-obligations_for_system` scoring 40 of
# 47 against a ceiling of 47 is why gold yield is the headline and
# edge-intersection is reported only beside its constants. The constant is even
# closer to the ceiling at 100 rows (85%) than it was at 23 (89% of 9), so the
# argument for not using the edge metric as the headline is unchanged.
CEILING = 47


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


# Scored rows on which the rules arm executes a plan that returns nothing.
SILENT_ROWS = ["3h-009", "ag-006", "xr-009"]


def test_the_graph_path_answers_all_but_three_rows_it_is_given(board) -> None:
    """Reach, counted in statements rather than rows.

    At 9 scored rows this asserted the graph answered every one. At 51 it answers
    48, and the three it does not are named rather than absorbed into a rate:

      `3h-009`, `xr-009`  S6-bridge selects a template whose traversal returns no
                          rows -- the bridge the rule assumes is not in the graph
      `ag-006`            S0-none: no rule fires at all, so no plan is built

    `ag-006` is the interesting one. It is "What are the AI Act's administrative
    fine tiers?", a row deliberately labelled `graph` because the four Art. 99
    paragraphs are lexically near-identical and defeat embedding retrieval. The
    selector reaching nothing for it means that row is currently answered by
    neither path well -- the same shape as `xr-007` above.
    """
    assert board["rules"]["rows_answerable"] == SCORED_ROWS - len(SILENT_ROWS)
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


def test_the_ceilings_and_oracle_are_what_the_docs_claim(board) -> None:
    """The pre-registration, recomputed rather than recalled.

    Named and shaped after `test_reranker.py`'s
    `test_the_ceilings_and_oracle_are_what_query_path_md_claims`, which this file
    should have copied the first time. Every figure here is now computed by
    `scoreboard()` from the artifact; these literals are the published claim in
    ADR-0013 and query-path.md, and this test is what ties the two together.
    """
    assert board["oracle"] == ORACLE
    assert board["ceiling_edge"] == CEILING_EDGE
    assert board["ceiling_anchor"] == CEILING_ANCHOR
    assert board["gold_total"] == GOLD_TOTAL


def test_no_arm_exceeds_the_oracle(board) -> None:
    """A structural invariant, not a measurement.

    No selector can reach a gold chunk that no fillable template reaches, so this
    cannot be satisfied by editing a number -- which is exactly what a hand-typed
    oracle invited. If an arm ever exceeds it, the oracle computation is wrong,
    not the arm.
    """
    for arm in ts.ARMS:
        assert board[arm]["gold_hit"] <= board["oracle"], arm


# The single row where the oracle beats the rules on the 100-row set.
ORACLE_GAP_ROWS = ["xr-007"]


def test_rules_falls_one_row_short_of_the_oracle(board) -> None:
    """AT 23 ROWS THE RULES EQUALLED THE ORACLE EXACTLY (24 == 24). AT 100 THEY DO
    NOT, AND THE ONE ROW THEY LOSE IS NAMED.

    The old form of this test asserted equality and concluded that "selection is
    NOT the binding constraint on this eval set -- what the templates can reach at
    all is". That conclusion survives, barely: 61 of a 62 oracle is still a
    selector that is almost never the thing standing between the graph and the
    gold, and the reach ceiling (`CEILING_ANCHOR = 51` rows) is still the real
    limit. But equality was a property of 9 rows, not a law, and it is gone.

    The row is `xr-007` ("Does the AI Act override the GDPR?"). The rules pick
    `cross_regulation` via S3-cross-instrument and fill it with the wrong anchor;
    the oracle picks the same template anchored on `GDPR` and gets 2 gold chunks.

    **`xr-007` is the hardest row in the set and this is the second time it has
    surfaced.** It is also one of the three rows whose gold is entirely absent
    from the vector path's 50-candidate pool. So neither path reaches it by
    default: the vector arm cannot retrieve it and the graph arm anchors it
    wrongly. A row that defeats both halves independently is worth more than its
    one point, and it is the row to look at first if the hybrid underperforms.
    """
    gap = board["oracle"] - board["rules"]["gold_hit"]
    assert gap == len(ORACLE_GAP_ROWS), (
        f"the rules-to-oracle gap moved to {gap} -- that is a finding for "
        f"ADR-0013, not a constant to retune"
    )
    assert board["rules"]["gold_hit"] == RULES_GOLD_HITS
    assert board["oracle"] == ORACLE


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


def test_the_artifact_rebuilds_from_its_own_plans(loaded, indexed, rows, artifact) -> None:
    """The graph half of the artifact is reproducible; the model half is not.

    R7B cannot have `test_the_rules_arm_still_reproduces_the_artifact` -- two
    sweeps at `temperature=0, seed=42` gave different plans. But everything
    downstream of a plan is deterministic given a loaded graph, and that is what
    `--rebuild` exploits to recompute graph-side fields without re-asking (or
    re-paying) the model. This test is the guarantee that it does.
    """
    recorded = {(e["id"], e["selector"]): e for e in artifact}
    rebuilt = ts.sweep(rows, driver=loaded, conn=indexed, plans=recorded)

    graph_fields = [
        "gold_hits", "provenance", "rows_returned", "empty_calls",
        "docs_rendered", "docs_derived", "docs_annex_caveated",
        "oracle_hits", "oracle_choice", "edge_reachable", "anchor_fillable",
    ]
    model_fields = ["plan", "rule", "raw", "cost_usd", "latency_ms", "attempts"]

    assert len(rebuilt) == len(artifact)
    for entry in rebuilt:
        before = recorded[(entry["id"], entry["selector"])]
        for field in graph_fields:
            assert entry[field] == before[field], f'{entry["id"]}/{entry["selector"]}.{field}'
        for field in model_fields:
            assert entry[field] == before[field], (
                f'{entry["id"]}/{entry["selector"]}.{field} moved -- a rebuild must '
                f"replay the model, never re-ask it"
            )
