"""The eval set is the measuring instrument for every benchmark claim, so it gets
checked like one.

These run without a database: the golds are validated against the corpus JSONL and
against the edges in `extractions.jsonl`, not against a live Neo4j.

Every check here was first run by hand during the 2026-07-31 review, which found a
harness pointing at a deleted file, ten rows declaring graph edges that do not
exist, and a question whose gold chunk did not contain its own answer. This file
is what stops those from coming back -- the repo's rule is that a verification not
in the code is a memory, not a verification.
"""

from __future__ import annotations

import collections
import json
from typing import get_args

import pytest

from eval.run_benchmark import QUESTIONS, load_questions
from src.ingest.extract import CHUNK_FILES, RelationType
from src.schemas import Route

# `must_cite` and "has gold chunks" per stratum. The three refusal modes differ, and
# the difference is the point: out-of-scope and unanswerable must cite NOTHING, while
# hard-negative rejects a false premise and must ground the correction in retrieved
# text. A single global rule would let an out-of-scope row carrying chunks through.
CITATION_RULES: dict[str, tuple[bool, bool]] = {
    "out-of-scope": (False, False),
    "unanswerable": (False, False),
    "hard-negative": (True, True),
}
DEFAULT_CITATION_RULE = (True, True)

KNOWN_STRATA = {
    "single-hop", "two-hop", "three-hop", "cross-regulation",
    "aggregation", "out-of-scope", "unanswerable", "hard-negative",
}

HOPS_BY_STRATUM = {"out-of-scope": {0, 1}, "unanswerable": {0, 1}, "hard-negative": {0, 1}}


def _seed_route(row: dict) -> str:
    """The route a purely mechanical derivation would produce.

    Phase 3 Step 3 labels `route` by hand, and this is the derive half of
    derive-then-verify: it is *not* the gold label, it is the thing the gold label
    is checked against so that a hand judgement has to be stated rather than
    silently absorbed. 21 of 23 rows agree with it; the two that do not carry a
    `route_reason` saying why, which is the only reason this function exists.
    """
    if row.get("graph_traversable") is False:
        return "vector"
    if not row["ontology_edges"]:
        return "vector"
    return "both" if row["hops"] >= 2 else "vector"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return load_questions()


@pytest.fixture(scope="module")
def corpus_ids() -> set[str]:
    ids: set[str] = set()
    for path in CHUNK_FILES:
        with path.open(encoding="utf-8") as fh:
            ids |= {json.loads(line)["chunk_id"] for line in fh if line.strip()}
    return ids


@pytest.fixture(scope="module")
def edges_by_chunk() -> dict[str, set[str]]:
    """Relationship types actually extracted from each chunk."""
    from src.ingest.extract import EXTRACTIONS_PATH

    out: dict[str, set[str]] = collections.defaultdict(set)
    with EXTRACTIONS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            for rel in json.loads(line)["relationships"]:
                out[rel["source_chunk_id"]].add(rel["type"])
    return out


def _missing_edges(row: dict, edges_by_chunk: dict[str, set[str]]) -> list[str]:
    present: set[str] = set()
    for chunk_id in row["source_chunk_ids"]:
        present |= edges_by_chunk.get(chunk_id, set())
    return [e for e in row["ontology_edges"] if e not in present]


def _excuse(row: dict) -> str:
    """The expected_fail reason, or "" if absent/blank."""
    return row.get("expected_fail", {}).get("reason", "").strip()


# --------------------------------------------------------------------------
# The harness can find the file at all
# --------------------------------------------------------------------------

def test_questions_file_resolves():
    """A1: run_benchmark.py pointed at questions.jsonl after the set was renamed to
    eval-questions.jsonl, so load_questions() raised FileNotFoundError."""
    assert QUESTIONS.exists(), f"{QUESTIONS} does not exist"
    assert load_questions(), "eval set is empty"


# --------------------------------------------------------------------------
# Gold passages
# --------------------------------------------------------------------------

def test_gold_chunks_exist(rows, corpus_ids):
    """source_chunk_ids is the recall harness's ground truth; an id that is not in
    the corpus can never be retrieved, so the row would score 0 forever."""
    for row in rows:
        unknown = [c for c in row["source_chunk_ids"] if c not in corpus_ids]
        assert not unknown, f"{row['id']}: gold chunks not in corpus: {unknown}"


def test_gold_chunks_are_unique_per_row(rows):
    for row in rows:
        dupes = [c for c, n in collections.Counter(row["source_chunk_ids"]).items() if n > 1]
        assert not dupes, f"{row['id']}: duplicate gold chunks {dupes} would skew recall"


def test_enough_scoreable_rows_for_the_dimension_experiment(rows):
    """ADR-0004 needs ~20 labeled queries to decide 1536 vs 512."""
    scoreable = [r for r in rows if r["source_chunk_ids"]]
    assert len(scoreable) >= 20, f"only {len(scoreable)} rows carry gold chunks"


# --------------------------------------------------------------------------
# Declared graph edges
# --------------------------------------------------------------------------

def test_ontology_edges_are_in_the_ontology(rows):
    valid = set(get_args(RelationType))
    for row in rows:
        unknown = [e for e in row["ontology_edges"] if e not in valid]
        assert not unknown, f"{row['id']}: {unknown} are not relationship types"


def test_declared_edges_exist_or_row_is_expected_fail(rows, edges_by_chunk):
    """The canary check.

    Written as ONE disjunction on purpose. Checking presence first and the flag
    second would fail an expected-fail row before ever reaching its flag, which
    would force exactly the 3h-002 relabel that was rejected: that row declares
    EXEMPT_FROM for the Art. 6(3) derogation, the extractor emits PERMITS, and the
    row is meant to stay red until the extractor learns the distinction.
    """
    for row in rows:
        if not row["source_chunk_ids"]:
            continue
        missing = _missing_edges(row, edges_by_chunk)
        assert not missing or _excuse(row), (
            f"{row['id']}: declares {missing}, which its gold chunks do not carry, "
            f"and has no expected_fail reason"
        )


def test_expected_fail_rows_still_fail(rows, edges_by_chunk):
    """A mute must not outlive its cause.

    When the extractor learns EXEMPT_FROM, this fails and tells you to delete the
    flag from 3h-002. Without it, a silenced canary stays silenced forever.
    """
    for row in rows:
        if not _excuse(row):
            continue
        missing = _missing_edges(row, edges_by_chunk)
        assert missing, (
            f"{row['id']}: expected_fail is set but every declared edge now exists "
            f"-- remove the flag"
        )


def test_expected_fail_reason_is_not_blank(rows):
    """A blank reason would silence a canary while looking like documentation."""
    for row in rows:
        if "expected_fail" not in row:
            continue
        assert _excuse(row), f"{row['id']}: expected_fail must carry a non-empty reason"
        assert row["expected_fail"].get("since"), f"{row['id']}: expected_fail needs a `since` date"


# --------------------------------------------------------------------------
# Citation conventions -- per refusal mode, not global
# --------------------------------------------------------------------------

def test_citation_rule_matches_stratum(rows):
    for row in rows:
        want = CITATION_RULES.get(row["stratum"], DEFAULT_CITATION_RULE)
        got = (row["must_cite"], bool(row["source_chunk_ids"]))
        assert got == want, (
            f"{row['id']} ({row['stratum']}): (must_cite, has_gold_chunks) is {got}, "
            f"expected {want}"
        )


def test_refusal_rows_promise_no_citation(rows):
    """out-of-scope and unanswerable must produce nothing to cite; hard-negative is
    the one refusal mode that must ground its rejection in retrieved text."""
    for row in rows:
        if row["stratum"] in ("out-of-scope", "unanswerable"):
            assert row["citations"] == [], f"{row['id']}: refusal rows must not carry citations"
        if row["stratum"] == "hard-negative":
            assert row["citations"], f"{row['id']}: hard-negative must cite the correcting text"


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------

def test_ids_are_unique_and_gapless(rows):
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate ids"
    by_prefix: dict[str, list[int]] = collections.defaultdict(list)
    for i in ids:
        prefix, _, num = i.rpartition("-")
        by_prefix[prefix].append(int(num))
    for prefix, nums in by_prefix.items():
        expected = list(range(1, max(nums) + 1))
        assert sorted(nums) == expected, (
            f"{prefix}-* numbering has gaps: {sorted(nums)} (expected {expected})"
        )


def test_strata_are_known(rows):
    for row in rows:
        assert row["stratum"] in KNOWN_STRATA, f"{row['id']}: unknown stratum {row['stratum']!r}"


def test_hops_are_consistent_with_stratum(rows):
    expected = {"single-hop": {1}, "two-hop": {2}, "three-hop": {3}}
    for row in rows:
        allowed = expected.get(row["stratum"]) or HOPS_BY_STRATUM.get(row["stratum"])
        if allowed is None:
            continue
        assert row["hops"] in allowed, (
            f"{row['id']} ({row['stratum']}): hops={row['hops']}, expected one of {allowed}"
        )


def test_required_fields_present(rows):
    required = {
        "id", "stratum", "hops", "question", "gold", "citations",
        "source_chunk_ids", "grading_rule", "ontology_edges", "must_cite", "verified",
        "route",
    }
    for row in rows:
        missing = required - set(row)
        assert not missing, f"{row['id']}: missing fields {sorted(missing)}"
        assert row["question"].strip(), f"{row['id']}: empty question"
        assert row["gold"].strip(), f"{row['id']}: empty gold"
        assert row["grading_rule"].strip(), f"{row['id']}: empty grading_rule"


def test_every_row_is_verified(rows):
    """`verified` is a human sign-off that the gold was read out of the source text,
    not drafted from memory. An unverified row cannot score anything, so it must not
    reach a benchmark run.

    This gate landed last, on 2026-07-31, once hn-001's gold was written and signed
    off -- shipping it earlier would only have reddened the suite on a row that was
    known to be incomplete.
    """
    unverified = [r["id"] for r in rows if not r.get("verified")]
    assert not unverified, f"unverified rows must not ship: {unverified}"


def test_non_traversable_rows_carry_a_reason(rows):
    """xr-003/xr-004 compare AIA Art. 99 to GDPR Art. 83, which never cross-cite, so
    no article-level bridge is derivable and the hybrid has no edge over the vector
    baseline there. That is a reportable result, so it must be stated, not implied."""
    for row in rows:
        if row.get("graph_traversable") is False:
            assert row.get("graph_traversable_reason", "").strip(), (
                f"{row['id']}: graph_traversable=false needs a reason"
            )


# --------------------------------------------------------------------------
# Router gold labels (Phase 3 Step 3)
# --------------------------------------------------------------------------

def test_routes_are_known(rows):
    """`route` is what the router is scored against, so an unknown value there
    would be counted as a misroute forever rather than as a typo."""
    valid = set(get_args(Route))
    for row in rows:
        assert row["route"] in valid, (
            f"{row['id']}: route {row['route']!r} is not one of {sorted(valid)}"
        )


def test_non_traversable_rows_route_to_vector(rows):
    """Two independent readings reached this conclusion and neither was in code.

    The eval author read the law and set `graph_traversable: false` on xr-003 and
    xr-004 (AIA Art. 99 and GDPR Art. 83 never cross-cite). Step 2's linker, working
    only from the graph, then reached nothing those rows' gold chunks assert. A
    router that sends either row to `graph` is wrong for a reason this repo has
    already established twice, so it is asserted rather than left to the gold label.
    """
    for row in rows:
        if row.get("graph_traversable") is False:
            assert row["route"] == "vector", (
                f"{row['id']}: graph_traversable=false but route={row['route']!r}"
            )


def test_route_overrides_carry_a_reason(rows):
    """A hand label that contradicts the mechanical seed must say why.

    Without this, `route` degrades into an unfalsifiable field: any router result
    could be accommodated by quietly moving a label. Same shape as
    test_non_traversable_rows_carry_a_reason -- a negative needs a stated reason.
    """
    for row in rows:
        if row["route"] != _seed_route(row):
            assert row.get("route_reason", "").strip(), (
                f"{row['id']}: route={row['route']!r} overrides the seed "
                f"{_seed_route(row)!r} and carries no route_reason"
            )


def test_route_labels_are_not_a_single_class(rows):
    """The measurement is only meaningful if a constant router cannot win it.

    Necessity labelling was adopted knowing it might make one class dominant; the
    agreed mitigation was to report an `always-<mode>` arm as the majority-class
    baseline. This pins the number that arm scores, so a later relabelling that
    quietly makes the task trivial shows up here rather than as a flattering
    router accuracy.
    """
    counts = collections.Counter(r["route"] for r in rows)
    assert len(counts) == 3, f"all three routes must appear as gold: {dict(counts)}"
    majority = counts.most_common(1)[0][1] / len(rows)
    assert majority < 0.60, (
        f"majority class is {majority:.0%} of rows ({dict(counts)}); a constant "
        f"router would score that, and the comparison stops meaning anything"
    )
