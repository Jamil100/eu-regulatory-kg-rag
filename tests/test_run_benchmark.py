"""The benchmark harness computes every cell in the README table, so the rules it
was pre-registered on are asserted rather than trusted.

All pure: `scoreboard()` and `markdown_table()` take an artifact and return
numbers, with no database, no key and no network. That property is itself one of
the tests -- it is what lets `--eval` reproduce the published table on a laptop
with the containers down, which is the guarantee every other harness in this repo
makes.
"""

from __future__ import annotations

import inspect

import pytest

from eval.run_benchmark import (
    DEPLOYED_SYSTEM,
    EXPECTED_FAIL_BUCKET,
    ORACLE_SYSTEM,
    PASSING,
    REFUSAL_STRATA,
    SYSTEMS,
    SYSTEM_ORDER,
    bucket_of,
    load_questions,
    markdown_table,
    scoreboard,
    sweep,
)


def row(system="hybrid", mode="replay", rid="sh-001", stratum="single-hop",
        verdict="correct", **over) -> dict:
    out = {
        "system": system, "mode": mode, "id": rid, "stratum": stratum,
        "verdict": verdict, "gold": ["c1"], "gold_cited": ["c1"],
        "cited_chunk_ids": ["c1"], "finish_reason": "COMPLETE",
        "error": None, "bucket": None, "must_cite": True,
        "latency_ms": 1000.0, "cost_usd": 0.005, "attempts": 1,
    }
    out.update(over)
    return out


def three_systems(**over) -> list[dict]:
    return [row(system=s, **over) for s in ("vector", "rerank", "hybrid")]


# --------------------------------------------------------------------------
# Purity -- the property the whole reproduce-with-containers-down claim rests on
# --------------------------------------------------------------------------

def test_scoreboard_touches_no_database_and_no_network():
    """Asserted structurally rather than by hoping: neither `scoreboard` nor
    `markdown_table` may reference a connection, a client or the SDK."""
    for fn in (scoreboard, markdown_table):
        src = inspect.getsource(fn)
        for forbidden in ("connect(", "cohere", "client", "requests", "httpx"):
            assert forbidden not in src, f"{fn.__name__} references {forbidden!r}"


def test_scoreboard_runs_on_an_empty_artifact():
    board = scoreboard([])
    assert board["systems"] == []
    assert board["common"]["n"] == 0


# --------------------------------------------------------------------------
# Reporting rule 1 -- accuracy from replay, latency and cost from live
# --------------------------------------------------------------------------

def test_accuracy_comes_from_replayed_rows_only():
    """A live row must not move an accuracy cell; the two passes measure
    different things and mixing them was the defect the split exists to avoid."""
    artifact = [
        row(mode="replay", rid="sh-001", verdict="correct"),
        row(mode="live", rid="sh-001", verdict="wrong"),
    ]
    cell = scoreboard(artifact)["hybrid"]["per_stratum"]["single-hop"]
    assert (cell["pass"], cell["n"]) == (1, 1)


def test_cost_and_latency_come_from_live_rows_only():
    """A replayed row never paid the embed+rerank round trip, so counting its
    $0.00 would understate the cost column by exactly the vector half."""
    board = scoreboard([row(mode="replay", cost_usd=0.0, latency_ms=10.0)])
    assert board["hybrid"]["cost_mean"] is None
    assert board["hybrid"]["latency_p95"] is None

    board = scoreboard([
        row(mode="replay"),
        row(mode="live", cost_usd=0.02, latency_ms=4000.0),
    ])
    assert board["hybrid"]["cost_mean"] == pytest.approx(0.02)
    assert board["hybrid"]["latency_p95"] == 4000.0


def test_no_per_stratum_p95_is_published():
    """Pre-registered in the module docstring: the strata carry 5-20 rows and a
    p95 over 5 is a maximum with a better name. Structural, like
    `test_no_per_route_p95_is_published` in test_api.py -- so a later step cannot
    add one without deleting a test that says why not."""
    artifact = [
        row(mode="live", rid=f"sh-{i:03d}", latency_ms=100.0 * i)
        for i in range(1, 8)
    ]
    board = scoreboard(artifact)
    for stratum, cell in board["hybrid"]["per_stratum"].items():
        assert not any("p95" in k for k in cell), f"{stratum} publishes a p95"
    assert board["hybrid"]["latency_p95"] is not None, "the per-SYSTEM p95 is published"


def test_a_retried_row_is_excluded_from_the_latency_percentile():
    """A call that retried spent most of its wall clock asleep in backoff;
    averaging that in produces a number about the API key."""
    board = scoreboard([
        row(mode="live", rid="a", latency_ms=1000.0, attempts=1),
        row(mode="live", rid="b", latency_ms=90000.0, attempts=3),
    ])
    assert board["hybrid"]["latency_max"] == 1000.0


# --------------------------------------------------------------------------
# Reporting rule 2 -- the systems do not share a denominator unless one is imposed
# --------------------------------------------------------------------------

def test_common_is_the_intersection_of_what_every_system_scored():
    artifact = [
        *three_systems(rid="a"),
        row(system="vector", rid="b"), row(system="rerank", rid="b"),
        # hybrid never scored `b` -- it truncated.
        row(system="hybrid", rid="b", finish_reason="MAX_TOKENS"),
    ]
    board = scoreboard(artifact)
    assert board["common"]["ids"] == ["a"]
    assert board["common"]["excluded"] == ["b"]
    # Own denominators differ; that is exactly why COMMON exists.
    assert board["vector"]["scored"] == 2
    assert board["hybrid"]["scored"] == 1
    assert board["vector"]["common_n"] == board["hybrid"]["common_n"] == 1


def test_a_truncated_row_is_excluded_and_named():
    board = scoreboard([row(rid="x", finish_reason="MAX_TOKENS")])
    assert board["hybrid"]["truncated"] == ["x"]
    assert board["hybrid"]["scored"] == 0


def test_an_errored_row_is_excluded_and_named():
    board = scoreboard([row(rid="x", error="AnswerPathError: boom", verdict=None)])
    assert board["hybrid"]["errors"] == ["x"]
    assert board["hybrid"]["scored"] == 0


def test_an_ungraded_row_is_reported_rather_than_scored_wrong():
    """A grading failure is a fact about the grader; scoring it against the
    system would be the wrong attribution."""
    board = scoreboard([row(rid="x", verdict=None)])
    assert board["hybrid"]["ungraded"] == ["x"]
    assert board["hybrid"]["scored"] == 0


# --------------------------------------------------------------------------
# Reporting rule 3 -- the three refusal modes are never averaged
# --------------------------------------------------------------------------

def test_the_three_refusal_modes_are_reported_separately():
    artifact = [
        row(rid="oos-001", stratum="out-of-scope", verdict="correct_refusal",
            cited_chunk_ids=[], must_cite=False, gold=[], gold_cited=[]),
        row(rid="oos-007", stratum="unanswerable", verdict="wrong",
            cited_chunk_ids=[], must_cite=False, gold=[], gold_cited=[]),
        row(rid="hn-001", stratum="hard-negative", verdict="correct"),
    ]
    refusal = scoreboard(artifact)["hybrid"]["refusal"]
    assert set(refusal) == set(REFUSAL_STRATA)
    assert refusal["out-of-scope"]["pass"] == 1
    assert refusal["unanswerable"]["pass"] == 0
    assert refusal["hard-negative"]["pass"] == 1


def test_the_two_opposite_refusal_failures_are_distinguished():
    """`cited when it should not` and `cited nothing when it must` are opposite
    defects. One combined number would report a system that made both as
    consistent."""
    artifact = [
        row(rid="oos-001", stratum="out-of-scope", verdict="wrong",
            cited_chunk_ids=["c1"], must_cite=False, gold=[], gold_cited=[]),
        row(rid="hn-001", stratum="hard-negative", verdict="partially_correct",
            cited_chunk_ids=[], gold_cited=[]),
    ]
    refusal = scoreboard(artifact)["hybrid"]["refusal"]
    assert refusal["out-of-scope"]["cited_when_it_should_not"] == ["oos-001"]
    assert refusal["hard-negative"]["uncited_when_it_should_cite"] == ["hn-001"]


def test_the_markdown_table_breaks_refusal_out_instead_of_averaging_it():
    artifact = [
        *three_systems(rid="sh-001"),
        *[row(system=s, rid="oos-001", stratum="out-of-scope",
              verdict="correct_refusal", cited_chunk_ids=[], must_cite=False,
              gold=[], gold_cited=[]) for s in ("vector", "rerank", "hybrid")],
    ]
    table = markdown_table(scoreboard(artifact))
    assert "Out-of-scope (cite nothing)" in table
    assert "Unanswerable (cite nothing)" in table
    assert "Hard-negative (must cite)" in table
    assert "never averaged" in table


# --------------------------------------------------------------------------
# Reporting rule 4 -- expected_fail is its own bucket
# --------------------------------------------------------------------------

def test_an_expected_fail_row_is_neither_a_pass_nor_a_failure():
    """Counting it as a pass silences the canary; counting it as a system
    failure blames retrieval for an extraction gap."""
    board = scoreboard([
        row(rid="3h-002", verdict="wrong", bucket=EXPECTED_FAIL_BUCKET,
            stratum="three-hop"),
        row(rid="sh-001", verdict="correct"),
    ])
    assert board["hybrid"]["scored"] == 1
    assert board["hybrid"]["expected_fail"] == {"3h-002": "wrong"}
    assert "three-hop" not in board["hybrid"]["per_stratum"]


def test_bucket_of_needs_a_non_blank_reason():
    assert bucket_of({"expected_fail": {"reason": "extractor emits PERMITS"}}) == EXPECTED_FAIL_BUCKET
    assert bucket_of({"expected_fail": {"reason": "   "}}) is None
    assert bucket_of({}) is None


def test_the_eval_sets_expected_fail_rows_are_bucketed():
    """3h-002 is the live canary; if it stops being flagged this stops being a
    bucket and starts being a silent failure."""
    flagged = [r["id"] for r in load_questions() if bucket_of(r)]
    assert flagged, "no expected_fail row found; 3h-002 was the canary"


# --------------------------------------------------------------------------
# The table is generated, never transcribed
# --------------------------------------------------------------------------

def test_the_table_cells_are_fractions_not_bare_percentages():
    """At 5 rows a refusal stratum has no meaningful percentage, and rounding one
    to '80%' hides the denominator."""
    table = markdown_table(scoreboard(three_systems()))
    assert "1/1" in table
    assert "%" not in table


def test_the_table_renders_a_missing_cell_rather_than_inventing_one():
    board = scoreboard(three_systems(rid="sh-001", stratum="single-hop"))
    table = markdown_table(board)
    assert "| - |" in table, "an unmeasured stratum must render as a dash"


def test_every_system_in_the_table_is_one_the_roadmap_names_or_a_marked_ceiling():
    """The roadmap names three. `hybrid-oracle` is the fourth and it is a ceiling
    rather than a system, which is why it carries a marker in the table and why
    it is excluded here rather than quietly folded in."""
    assert set(SYSTEMS) - {ORACLE_SYSTEM} == {"vector", "rerank", "hybrid"}
    assert SYSTEMS["vector"]["field"] == "retrieved", "vector-only replays the pre-rerank draw"
    assert SYSTEMS["rerank"]["field"] == "reranked"
    assert SYSTEMS["hybrid"]["route"] is None, "the hybrid must use the adopted router"


def test_the_baselines_are_pinned_to_the_vector_route():
    """Letting the router send a baseline row to `graph` would make the
    three-way comparison a comparison of routers."""
    assert SYSTEMS["vector"]["route"] == "vector"
    assert SYSTEMS["rerank"]["route"] == "vector"


def test_the_live_vector_baseline_does_not_go_through_the_reranker():
    """`answer()` hardwires retrieve->rerank in its vector branch, so a live
    sweep that just called it would make the vector-only baseline identical to
    the rerank system in exactly the two columns live mode exists to produce.

    Asserted structurally, because the failure is silent: both systems would
    still emit plausible per-row costs and latencies, and the only symptom would
    be a suspiciously equal pair of numbers in the published table.
    """
    src = inspect.getsource(sweep)
    assert "live_passages" in src, "the live vector arm must inject its own passages"
    assert "retrieve_detailed" in src, "the live vector arm must do its own retrieval"
    # And it must be conditioned on the un-reranked ordering, not on the system name.
    assert 'spec["field"] != "retrieved"' in src


def test_the_replayed_orderings_are_the_two_the_artifact_commits():
    """`vector` and `rerank` differ only in which committed ordering they replay,
    so a typo in either field name would silently make them the same system."""
    from src.answer.answer_path import PASSAGE_FIELDS

    assert set(PASSAGE_FIELDS) == {"retrieved", "reranked"}
    assert PASSAGE_FIELDS["retrieved"] == "retrieved_scores"
    assert PASSAGE_FIELDS["reranked"] == "rerank_scores"
    assert {SYSTEMS[s]["field"] for s in ("vector", "rerank")} == set(PASSAGE_FIELDS)


def test_partially_correct_is_not_counted_as_a_pass():
    assert "partially_correct" not in PASSING
    board = scoreboard([row(verdict="partially_correct")])
    cell = board["hybrid"]["per_stratum"]["single-hop"]
    assert (cell["pass"], cell["partial"], cell["n"]) == (0, 1, 1)


def test_gold_retention_is_reported_beside_accuracy():
    """Every pre-Phase-5 number in this repo is retention; keeping it visible is
    what makes the two comparable."""
    board = scoreboard([row(gold=["c1", "c2"], gold_cited=["c1"])])
    assert board["hybrid"]["gold_total"] == 2
    assert board["hybrid"]["gold_cited"] == 1


# --------------------------------------------------------------------------
# The oracle arm -- added 2026-08-15 when the router was re-measured at 70/99
# --------------------------------------------------------------------------

def test_the_oracle_arm_replays_the_gold_route():
    """`route: "gold"` is the sentinel `sweep()` reads to replay the eval set's
    hand-verified label instead of asking the router."""
    assert SYSTEMS[ORACLE_SYSTEM]["route"] == "gold"
    assert SYSTEMS[DEPLOYED_SYSTEM]["route"] is None
    src = inspect.getsource(sweep)
    assert 'spec["route"] == "gold"' in src, "the gold sentinel must be honoured in sweep()"


def test_the_router_cost_is_computed_over_rows_both_hybrid_arms_scored():
    """Otherwise the difference between the two arms would be partly a difference
    in denominator rather than entirely the router's doing."""
    artifact = [
        row(system="hybrid", rid="a", verdict="wrong", route="vector"),
        row(system="hybrid-oracle", rid="a", verdict="correct", route="both"),
        row(system="hybrid", rid="b", verdict="correct", route="both"),
        row(system="hybrid-oracle", rid="b", verdict="correct", route="both"),
        # `c` is scored by the oracle only, so it must not enter the comparison.
        row(system="hybrid-oracle", rid="c", verdict="correct", route="both"),
    ]
    cost = scoreboard(artifact)["router_cost"]
    assert cost["n"] == 2
    assert cost["lost_to_routing"] == ["a"]
    assert cost["misrouted"] == ["a"]
    assert cost["oracle_pass"] - cost["deployed_pass"] == 1


def test_a_misrouted_row_that_still_passes_is_not_counted_as_a_router_cost():
    """The route was wrong and it did not matter -- the vector path happened to
    carry the answer. Counting it would overstate what the router costs."""
    artifact = [
        row(system="hybrid", rid="a", verdict="correct", route="vector"),
        row(system="hybrid-oracle", rid="a", verdict="correct", route="both"),
    ]
    cost = scoreboard(artifact)["router_cost"]
    assert cost["misrouted"] == ["a"]
    assert cost["lost_to_routing"] == []


def test_the_router_cost_is_absent_when_only_one_hybrid_arm_ran():
    assert "router_cost" not in scoreboard([row(system="hybrid")])


def test_the_oracle_arm_is_marked_as_a_ceiling_in_the_table():
    """An unmarked row would read as a deployable system; it uses gold route
    labels a live request does not have."""
    artifact = [
        *[row(system=s, rid="sh-001") for s in ("vector", "rerank", "hybrid")],
        row(system="hybrid-oracle", rid="sh-001"),
    ]
    table = markdown_table(scoreboard(artifact))
    assert "[ceiling]" in table
    assert "not a deployable system" in table.lower()


def test_the_table_is_ascii_only():
    """Windows consoles default to cp1252, and `_report` prints this table. A
    box-drawing or dagger glyph raises UnicodeEncodeError *after* the sweep has
    run and the money is spent -- which is how answer_path.py found the same bug.
    """
    artifact = [row(system=s, rid="sh-001") for s in SYSTEM_ORDER]
    table = markdown_table(scoreboard(artifact))
    assert table.isascii(), [c for c in table if not c.isascii()]
