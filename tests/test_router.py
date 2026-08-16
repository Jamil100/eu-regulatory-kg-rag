"""The router, and the measurement that chose it.

These run with no API key, no containers, and no spend. The R7B numbers come from
`eval/router-eval.jsonl`, the committed sweep artifact; the rules arm is
recomputed live and asserted to still reproduce what the artifact recorded, which
is what turns the artifact from a screenshot into a regression anchor.

The one thing here that is not about correctness is
`test_few_shot_examples_are_not_in_the_eval_set`. It exists because the leakage
it prevents would not fail anything -- it would just quietly raise R7B's score
and make ADR-0012 wrong.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from src.query import decision_log, router
from src.schemas import ROUTES


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return router.load_questions()


@pytest.fixture(scope="module")
def artifact() -> list[dict]:
    return router.load_artifact()


@pytest.fixture(scope="module")
def board(rows, artifact) -> dict:
    return router.scoreboard(rows, artifact)


# --------------------------------------------------------------------------
# The rules, one test per rule
# --------------------------------------------------------------------------

def test_r2_fires_when_no_link_is_a_template_anchor():
    """ag-003 links `GDPR` and `infringement` -- both real nodes, neither a
    parameter any template declares. This is the rule that actually carries the
    refusal-and-penalty rows, and it is the one R1 was assumed to be."""
    result = router.route_by_rules(
        "Which categories of infringement fall under the GDPR's highest fine tier?"
    )
    assert result.route == "vector"
    assert result.rule == "R2-no-anchor"


def test_r3_routes_a_role_enumeration_to_the_graph():
    result = router.route_by_rules(
        "List the main obligations the AI Act places on deployers of high-risk AI systems."
    )
    assert result.route == "graph"
    assert result.rule == "R3-enumerate-role"


def test_r4_fires_on_a_conjoined_second_question():
    result = router.route_by_rules(
        "What must a provider have established to demonstrate that a high-risk AI "
        "system complies with the requirements of Section 2, and who does that "
        "obligation fall on?"
    )
    assert result.route == "both"
    assert result.rule == "R4-second-ask"


def test_a_pronoun_tail_is_not_a_second_hop():
    """"...and is it a one-time or ongoing process?" asks a further property of the
    same subject, answered by the same passage. Without this distinction sh-001 and
    sh-002 both read as two-hop questions and cost two rows."""
    result = router.route_by_rules(
        "How does the AI Act define the risk management system for a high-risk AI "
        "system, and is it a one-time or ongoing process?"
    )
    assert result.route == "vector"
    assert result.rule == "R5-default"


# R1 fires on exactly one row of the 100-row set. See the test below.
R1_ROWS = ["oos-005"]


def test_r1_is_load_bearing_on_exactly_one_row(rows):
    """R1 STOPPED BEING INERT AT THE 100-ROW EXPANSION, WHICH IS WHAT THIS TEST
    WAS BUILT TO ANNOUNCE.

    It used to assert `fired == []`: Step 2 measured the link rate at 23 of 23,
    so "links to zero nodes" fired on nothing, and the old docstring said the
    assertion existed so that "a linker change which starts dropping questions
    shows up as a failure here -- at which point R1 becomes load-bearing and this
    test is the thing that says so."

    It did, and this is it saying so. The row is `oos-005` ("What labelling does
    China's deep synthesis regulation require?"), and it is the one case where
    reaching no node is the CORRECT outcome rather than a linker regression: the
    question is about a regulation the corpus does not contain. R1 sends it to
    `vector`, which is its gold route, so R1 is now a rule that earns a correct
    answer rather than a guard that never runs.

    The other four out-of-scope rows do link, because they name concepts the EU
    corpus also uses -- so this is emphatically not "out-of-scope rows link
    nothing", and refusal cannot be delegated to retrieval coming back empty.
    """
    fired = [r["id"] for r in rows
             if router.route_by_rules(r["question"]).rule == "R1-no-links"]
    assert fired == R1_ROWS, f"R1's firing set moved: {fired} -- update ADR-0012"
    by_id = {r["id"]: r for r in rows}
    for rid in fired:
        assert by_id[rid]["stratum"] == "out-of-scope", (
            f"{rid} links no node but is not out-of-scope -- that is a linker "
            f"regression, not a correct refusal"
        )
        assert by_id[rid]["route"] == "vector"


def test_r1_still_works_when_it_does_fire():
    """It is a real guard on the request path even though no eval row reaches it."""
    result = router.route_by_rules("zzzz qqqq wwww")
    assert result.route == "vector"
    assert result.rule == "R1-no-links"


def test_every_rule_returns_a_known_route(rows):
    for row in rows:
        result = router.route_by_rules(row["question"])
        assert result.route in ROUTES, f"{row['id']}: {result.route!r}"
        assert result.rule, f"{row['id']}: no rule recorded"


# --------------------------------------------------------------------------
# Parsing the model's answer
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["graph", " graph ", "graph.", "GRAPH", '"vector"'])
def test_parse_route_accepts_formatting(raw):
    assert router.parse_route(raw) in ROUTES


@pytest.mark.parametrize("raw", [
    "",
    "hybrid",
    "I would use the graph for this one",
    "this is not a graph question",
    "graph or vector",
])
def test_parse_route_refuses_to_guess(raw):
    """A substring search would read "this is not a graph question" as `graph`.

    An unparseable answer is None, is recorded raw, and is counted as a misroute.
    Coercing it to `both` would make a router that emits garbage indistinguishable
    from one that hedges, and those are different facts.
    """
    assert router.parse_route(raw) is None


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------

def test_the_artifact_covers_every_question_for_both_arms(rows, artifact):
    for arm in ("rules", "r7b"):
        got = {e["id"] for e in artifact if e["router"] == arm}
        assert got == {r["id"] for r in rows}, f"{arm} is missing rows"


def test_the_artifact_gold_matches_the_eval_set(rows, artifact):
    """The sweep copies each row's gold label. If the eval set is relabelled and the
    sweep is not re-run, every number in ADR-0012 silently becomes wrong."""
    gold = {r["id"]: r["route"] for r in rows}
    stale = [e["id"] for e in artifact if e["gold"] != gold[e["id"]]]
    assert not stale, (
        f"artifact gold is stale for {sorted(set(stale))} -- re-run --eval --refresh"
    )


def test_the_rules_arm_still_reproduces_the_artifact(rows, artifact):
    """The rules are deterministic and free, so the artifact's rules rows are a
    regression anchor rather than a record: any drift is a defect, here, now."""
    recorded = {e["id"]: (e["route"], e["rule"]) for e in artifact if e["router"] == "rules"}
    for row in rows:
        result = router.route_by_rules(row["question"])
        assert (result.route, result.rule) == recorded[row["id"]], (
            f"{row['id']}: rules now say {result.route}/{result.rule}, "
            f"artifact says {recorded[row['id']]}"
        )


# Re-measured 2026-08-15 on the 100-row eval set. ADR-0012 published 21/22 and
# 10/22 on the 23-row set; both are kept here because the DROP is the finding.
#
# THE 95% WAS OVERFITTING AND THIS IS THE EVIDENCE. The rules router reaches
# `both` through exactly one regex (`R4-second-ask`), tuned while looking at 23
# questions. On 77 unseen ones it generalises poorly: 24 rows whose gold is `both`
# are routed `vector` because they phrase the second hop in a way the regex does
# not match ("Which GDPR fine tier applies?", "when must X and when must it Y").
#
# The adoption still stands -- rules at 71% beats the majority-class constant at
# 48% -- but the headline number does not, and `eval/run_benchmark.py` grew a
# `hybrid-oracle` arm so the benchmark can separate the router's error from the
# hybrid's capability instead of confounding them.
RULES_CORRECT, RULES_SCORED = 70, 99
R7B_CORRECT, R7B_SCORED = 44, 99


def test_rules_accuracy_is_what_adr_0012_claims(board):
    assert board["rules"]["correct"] == RULES_CORRECT
    assert board["rules"]["scored"] == RULES_SCORED


def test_r7b_accuracy_is_what_adr_0012_claims(board):
    assert board["r7b"]["correct"] == R7B_CORRECT
    assert board["r7b"]["scored"] == R7B_SCORED


def test_the_rules_router_still_beats_the_majority_class_constant(board):
    """The adoption criterion from ADR-0012, re-checked on the bigger set. It is
    the claim that survived the expansion; the 95% headline is the one that did
    not, and the gap between rules and the best constant narrowed from 8 rows to
    22 percentage points on four times the data."""
    constants = {k: v["correct"] for k, v in board.items()
                 if isinstance(v, dict) and k.startswith("always")}
    assert constants, "the constant arms vanished from the scoreboard"
    assert board["rules"]["correct"] > max(constants.values())


def test_r7b_never_emits_the_third_class(artifact):
    """The finding ADR-0012 turns on, pinned so it cannot quietly stop being true.

    Command R7B returned `both` for 0 of 23 questions, under two different system
    prompts, with two of six few-shot examples demonstrating it. Its errors are not
    spread over a confusion matrix -- one whole class is missing, which is why it
    scores below the majority-class constant.
    """
    emitted = collections.Counter(e["route"] for e in artifact if e["router"] == "r7b")
    assert emitted["both"] == 0, (
        f"R7B now emits `both` ({emitted['both']}x) -- re-run the sweep and revisit "
        f"ADR-0012, which adopted the rules partly on this"
    )


def test_the_adopted_router_beat_both_constants(board):
    """The pre-registered mitigation: a router that cannot beat a constant has not
    earned a place in the request path."""
    adopted = board["rules" if router.ADOPTED == "rules" else "r7b"]
    for constant in ("always-vector", "always-both"):
        assert adopted["accuracy"] > board[constant]["accuracy"], (
            f"{router.ADOPTED} does not beat {constant}"
        )


def test_r7b_did_not_beat_the_majority_class_constant(board):
    """Recorded because it is the uncomfortable half of the result, and deleting it
    would leave ADR-0012 looking like a clean win for rules over a fair opponent."""
    assert board["r7b"]["accuracy"] < board["always-vector"]["accuracy"]


def test_the_hard_gate_holds_for_the_adopted_router(board):
    assert board["rules"]["gate_ok"]


def test_r7b_trips_the_hard_gate(rows, artifact):
    """xr-004 goes to `graph` under R7B. The gate was fixed before the run, so this
    is a pre-registered disqualification rather than a fact found afterwards and
    promoted into a reason."""
    preds = router.predictions(rows, artifact)["r7b"]
    no_bridge = [r["id"] for r in rows if r.get("graph_traversable") is False]
    assert any(preds[qid] == "graph" for qid in no_bridge)


def test_few_shot_examples_are_not_in_the_eval_set(rows):
    """Leakage guard.

    Reusing an eval question as a few-shot example would raise R7B's score without
    failing anything, which is the exact shape of defect this repo keeps finding.
    Compared on normalised text so that a reworded copy is still caught by the
    obvious cases, and on exact identity so an outright paste always is.
    """
    def norm(text: str) -> str:
        return " ".join(text.lower().split()).strip("?.")

    eval_questions = {norm(r["question"]) for r in rows}
    for example, _ in router.FEW_SHOT:
        assert norm(example) not in eval_questions, (
            f"few-shot example is an eval question: {example!r}"
        )


def test_few_shot_examples_are_labelled_with_real_routes():
    for example, answer in router.FEW_SHOT:
        assert answer in ROUTES, f"{example!r} is labelled {answer!r}"


def test_all_three_classes_are_demonstrated_to_the_model():
    """R7B never emitting `both` is a finding about R7B only if `both` was actually
    shown to it."""
    shown = collections.Counter(answer for _, answer in router.FEW_SHOT)
    assert set(shown) == set(ROUTES), f"few-shot covers only {sorted(shown)}"


# --------------------------------------------------------------------------
# The decision log
# --------------------------------------------------------------------------

def test_the_log_appends_and_never_destroys_the_previous_run(tmp_path: Path):
    """`docs/failure-notes.md` §3: `failures.jsonl` was rebuilt by every run, so the
    only evidence of what went wrong was gone before anyone read it. The roadmap
    says Phase 5 needs these decisions and cannot reconstruct them later, so the
    same defect here would be found at the point it is unrecoverable.
    """
    path = tmp_path / "decisions.jsonl"
    first = decision_log.new_run_id()
    decision_log.append(
        decision_log.Decision(run_id=first, question="q1", router="rules", route="vector"),
        path,
    )
    decision_log.append(
        decision_log.Decision(run_id="second-run", question="q2", router="r7b", route="graph"),
        path,
    )

    written = decision_log.read(path)
    assert len(written) == 2
    assert written[0]["run_id"] == first, "the first run's row did not survive the second"
    assert [r["question"] for r in written] == ["q1", "q2"]


def test_outcome_is_present_and_null_from_the_first_row(tmp_path: Path):
    """Phase 5 fills `outcome` in. A key that appears only in later rows is a schema
    change every reader then has to handle."""
    path = tmp_path / "decisions.jsonl"
    decision_log.append(
        decision_log.Decision(run_id="r", question="q", router="rules", route="vector"), path
    )
    row = decision_log.read(path)[0]
    assert "outcome" in row
    assert row["outcome"] is None


def test_a_logged_decision_is_json_and_carries_its_reason(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    result = router.route_by_rules("List the tasks of the market surveillance authority.")
    decision_log.append(
        decision_log.Decision(
            run_id="r", question="q", router="rules", route=result.route, rule=result.rule,
            linked=[e.canonical_name for e in result.linked or []],
        ),
        path,
    )
    raw = path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 1
    row = json.loads(raw[0])
    assert row["rule"] == result.rule
    assert row["route"] == result.route


def test_route_does_not_write_to_the_real_log_when_asked_not_to():
    """`/ask` (Step 7) needs to be able to route without logging -- in a test, or in
    a dry run -- without reaching for the module-level path."""
    assert router.route("What is a serious incident?", log=False) in ROUTES
