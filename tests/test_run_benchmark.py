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


def test_latency_comes_from_live_rows_only():
    """A replayed row never paid the embed+rerank round trip, so its wall clock
    is not a latency observation.

    THE COST HALF OF THIS TEST WAS REMOVED BY THE 2026-08-16 AMENDMENT, AND THE
    REPLACEMENT IS STRICTLY BETTER. `$/query` used to be sampled from live rows
    for the same reason latency is. It is now DERIVED -- generation tokens plus
    the query embedding plus, where the system reranks, one search unit -- so it
    is computed over every scored row instead of over whichever subset the live
    pass could afford, and it cannot drift from the price table. See
    `test_cost_is_derived_over_every_scored_row_not_sampled` and ADR-0015.
    """
    board = scoreboard([row(mode="replay", cost_usd=0.0, latency_ms=10.0)], {})
    assert board["hybrid"]["latency_p95"] is None, "a replayed row is not a latency sample"

    board = scoreboard([
        row(mode="replay"),
        row(mode="live", cost_usd=0.02, latency_ms=4000.0),
    ], {})
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
    assert set(SYSTEMS) - {ORACLE_SYSTEM} == {
        "vector", "rerank", "hybrid",
        # Added 2026-08-16. Not a roadmap system: it is the `rerank` arm with a
        # wider candidate pool (vector top-50 unioned with BM25 and Postgres-FTS
        # top-50), measured to decide whether the pool change ships. It is named
        # here rather than excluded like the oracle because it IS deployable --
        # `answer_path.LEXICAL_DEPTH_LIVE` is the switch -- so it must not be
        # quietly exempt from the checks the other deployable arms get.
        "rerank-pool",
    }
    assert SYSTEMS["vector"]["field"] == "retrieved", "vector-only replays the pre-rerank draw"
    assert SYSTEMS["rerank"]["field"] == "reranked"
    assert SYSTEMS["rerank-pool"]["field"] == "pool_reranked"
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


def test_the_replayed_orderings_are_the_ones_the_artifact_commits():
    """The replay arms differ only in which committed ordering they read, so a
    typo in any field name would silently make two of them the same system.

    Each ordering must also be paired with ITS OWN score column: attaching
    `rerank_scores` to the pool ordering would mislabel which arm produced a
    passage, and the mislabelling would be invisible because both are floats in
    [0, 1].
    """
    from src.answer.answer_path import PASSAGE_FIELDS

    assert set(PASSAGE_FIELDS) == {"retrieved", "reranked", "pool_reranked"}
    assert PASSAGE_FIELDS["retrieved"] == "retrieved_scores"
    assert PASSAGE_FIELDS["reranked"] == "rerank_scores"
    assert PASSAGE_FIELDS["pool_reranked"] == "pool_rerank_scores"
    assert len(set(PASSAGE_FIELDS.values())) == len(PASSAGE_FIELDS), "a score column is reused"
    assert {
        SYSTEMS[s]["field"] for s in ("vector", "rerank", "rerank-pool")
    } == set(PASSAGE_FIELDS)


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


# --------------------------------------------------------------------------
# The trimmed protocol (2026-08-16): derived cost, subsampled latency
# --------------------------------------------------------------------------

def test_cost_is_derived_over_every_scored_row_not_sampled():
    """$/query is exactly determined by recorded quantities, so it is computed
    over all scored rows rather than observed on whichever subset the live pass
    could afford. A live row must not be needed for the cost column at all."""
    from eval.run_benchmark import analytic_cost

    retrieval = {"sh-001": {"embed": 0.000003, "rerank": 0.002}}
    board = scoreboard([row(system="rerank", rid="sh-001", cost_usd=0.004)], retrieval)
    assert board["rerank"]["cost_is_derived"] is True
    assert board["rerank"]["cost_n"] == 1
    # generation + embed + rerank, because `rerank` pays a rerank call
    assert board["rerank"]["cost_mean"] == pytest.approx(0.004 + 0.000003 + 0.002)
    assert board["rerank"]["cost_mean_sampled"] is None, "no live row was supplied"


def test_the_vector_baseline_is_not_charged_for_a_rerank_it_never_makes():
    from eval.run_benchmark import analytic_cost

    retrieval = {"a": {"embed": 0.000003, "rerank": 0.002}}
    vec = analytic_cost(row(system="vector", rid="a", cost_usd=0.004), retrieval)
    rer = analytic_cost(row(system="rerank", rid="a", cost_usd=0.004), retrieval)
    assert vec == pytest.approx(0.004003)
    assert rer - vec == pytest.approx(0.002)
    assert SYSTEMS["vector"]["reranks"] is False


def test_an_unpriced_component_propagates_as_none_rather_than_zero():
    """config.price_of's rule: a route with an unpriced call has an unknown total,
    and a number silently short is worse than an admitted gap."""
    from eval.run_benchmark import analytic_cost

    assert analytic_cost(row(rid="a", cost_usd=None), {"a": {"embed": 0.0, "rerank": 0.0}}) is None
    assert analytic_cost(row(rid="a", cost_usd=0.004), {}) is None


def test_the_live_sample_is_deterministic_and_stratified():
    """Latency does not need every row, but WHICH rows must not depend on when the
    pass was run."""
    from eval.run_benchmark import live_sample

    rows = load_questions()
    a, b = live_sample(rows, 30), live_sample(rows, 30)
    assert [r["id"] for r in a] == [r["id"] for r in b]
    assert [r["id"] for r in live_sample(list(reversed(rows)), 30)] == [r["id"] for r in a]
    assert {r["stratum"] for r in a} == {r["stratum"] for r in rows}, "a stratum was dropped"
    assert live_sample(rows, 1000) == rows


def test_the_p95_carries_its_own_denominator_in_the_table():
    """A p95 measured on 30 rows and one measured on 100 must not look alike."""
    artifact = [
        *[row(system=s, rid="sh-001") for s in SYSTEM_ORDER],
        *[row(system=s, rid="sh-001", mode="live", latency_ms=4000.0)
          for s in ("vector", "rerank", "hybrid")],
    ]
    table = markdown_table(scoreboard(artifact, {}))
    assert "(n=1)" in table, "the p95 must publish the n it was measured over"


def test_the_oracle_arm_publishes_no_latency_or_cost_claim():
    """It uses gold route labels a live request does not have, so a per-query
    figure for it would describe something nobody can run."""
    artifact = [
        *[row(system=s, rid="sh-001") for s in SYSTEM_ORDER],
        row(system=ORACLE_SYSTEM, rid="sh-001", mode="live", latency_ms=4000.0),
    ]
    table = markdown_table(scoreboard(artifact, {}))
    oracle_line = next(l for l in table.splitlines() if "[ceiling]" in l and l.startswith("|"))
    assert oracle_line.rstrip().endswith("| - | - |"), oracle_line


# --------------------------------------------------------------------------
# Repeat sweeps -- `run_tag`
#
# A repeat run is identical to the published one in `system` and `mode`, which
# are the only two fields the scoreboard groups on. So without a tag the second
# sample of an arm would be pooled INTO that arm: the denominator would double,
# every row would appear twice, and the pooled cell would silently average a
# system against itself. These tests pin the isolation in both directions.
# --------------------------------------------------------------------------

def test_a_tagged_row_is_not_scored_as_part_of_the_published_arm():
    """The published table is the untagged rows and nothing else."""
    artifact = [
        row(rid="sh-001", verdict="correct"),
        row(rid="sh-001", verdict="wrong", run_tag="e1-run-b"),
    ]
    board = scoreboard(artifact)
    cell = board["hybrid"]["per_stratum"]["single-hop"]
    assert cell["n"] == 1, "the tagged repeat must not enlarge the denominator"
    assert cell["pass"] == 1, "the tagged repeat must not change the numerator"


def test_a_tagged_sweep_alone_produces_no_published_systems():
    """A run that is entirely tagged has measured nothing publishable. It must
    read as an empty board rather than as a benchmark result."""
    board = scoreboard([row(run_tag="e1-run-a"), row(run_tag="e1-run-a", rid="sh-002")])
    assert board["systems"] == []


def test_an_untagged_artifact_is_unaffected_by_the_tag_filter():
    """The filter must be a no-op on every artifact written before tags existed;
    rows predating the field have no `run_tag` key at all."""
    artifact = three_systems()
    for r in artifact:
        r.pop("run_tag", None)
    board = scoreboard(artifact)
    assert board["systems"], "pre-tag artifacts must still score"
    assert board["common"]["n"] >= 1


def test_sweep_records_the_tag_and_can_restrict_to_a_stratum():
    """`only_strata` is what makes a stratum-local re-run possible without
    pretending to be a full pass. Signature-level check: no key, no spend."""
    signature = inspect.signature(sweep).parameters
    assert "run_tag" in signature
    assert "only_strata" in signature
    assert signature["run_tag"].default == ""
    assert signature["only_strata"].default is None


def test_the_markdown_table_leads_with_the_comparable_denominator():
    """The defect this fixes: the README published `pass_total/scored`, which is
    each system's OWN denominator over rows the other systems did not all score.
    `common` is computed and was not published. Both now appear, and the
    comparable one is first."""
    artifact = three_systems()
    board = scoreboard(artifact)
    table = markdown_table(board)
    assert f"Overall (n={board['common']['n']})" in table
    assert "Not comparable across systems" in table
    # The per-stratum headers carry the caveat marker.
    assert "Single-hop^" in table


def test_every_per_system_column_in_the_table_is_marked_non_comparable():
    """A reader must not be able to pick an unmarked column that is not
    comparable. Every header except `System`, `Overall`, latency and cost is a
    per-system denominator and carries the marker."""
    board = scoreboard(three_systems())
    header = markdown_table(board).splitlines()[0]
    cells = [c.strip() for c in header.strip("|").split("|")]
    exempt = {"System", "p95 latency", "$/query"}
    for cell in cells:
        if cell in exempt or cell.startswith("Overall"):
            continue
        assert cell.endswith("^"), f"{cell!r} is per-system and is not marked"
