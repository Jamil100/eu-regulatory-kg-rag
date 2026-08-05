"""`POST /ask`: lifecycle, contract, error mapping, and the decision log.

No container, no API key, no spend for anything but the two live tests at the
bottom, which skip without both. The fakes follow the house pattern from
`tests/test_generate.py` and `tests/test_reranker.py`: an object in the real
one's shape, and one that asserts it was never called.

WHY THE HANDLES ARE STUBBED RATHER THAN BUILT.

`build_handles()` reads `data/processed/extractions.jsonl` (via
`entity_linker.build_index`) and `data/` is gitignored wholesale, so a fresh
clone has no corpus. A test suite that needs the corpus to check that a 422 is a
422 would be red on every machine that has not run the ingest, which is the same
failure `tests/conftest.py` avoids by skipping on a missing container. Everything
here that does not need real data gets a `Handles` built by hand.

THE ONE ASSERTION THAT IS NOT ABOUT TODAY.

`cost_usd` is `float | None` and `RERANK_PRICE_PER_SEARCH` is currently a number,
so no live route can produce `None`. `test_cost_usd_serialises_as_null_when_a_component_is_unpriced`
sets the constant back to `None` -- the honest state if the aggregator figure is
withdrawn (`config.py:88-91`) -- so the widened arm is exercised rather than
assumed. A widening whose new branch never runs is `failure-notes.md`'s own
*"a metric that looked like success"*.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from src.answer.answer_path import AnswerPathError, AnswerResult, NoContextError
from src.api import app as app_module
from src.api.app import Handles, app
from src.query.entity_linker import LinkedEntity
from src.query.router import RouterError, RouterResult
from src.schemas import Citation

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakePool:
    """A `psycopg_pool.ConnectionPool` as far as `/ask` is concerned.

    Records every checkout, so a test can assert the handler took a connection
    from the pool rather than opening one -- the whole point of Step 7.
    """

    def __init__(self) -> None:
        self.checkouts = 0
        self.closed = False
        self.conn = object()

    @contextmanager
    def connection(self):
        self.checkouts += 1
        yield self.conn

    def close(self) -> None:
        self.closed = True


class ExplodingPool:
    @contextmanager
    def connection(self):
        raise AssertionError("the pool was used when it should not have been")

    def close(self) -> None:
        pass


def a_result(**overrides) -> AnswerResult:
    """An `AnswerResult` in the shape `answer()` really returns."""
    defaults = dict(
        answer="A deployer is a natural or legal person using an AI system.",
        citations=[
            Citation(
                chunk_id="aia-art3-para1-def4",
                start=0,
                end=10,
                text="A deployer",
                citation_label="AIA Art. 3(4)",
                source="PASSAGE",
                document_id="d0",
            )
        ],
        route="vector",
        documents_sent=5,
        passage_sent=5,
        cost_usd=0.0043,
        generate_ms=2500.0,
    )
    defaults.update(overrides)
    return AnswerResult(**defaults)


@pytest.fixture
def handles(tmp_path):
    """Handles built by hand, with the decision log redirected to `tmp_path`."""
    return Handles(
        driver=object(),
        pool=FakePool(),
        client=object(),
        run_id="20260805T000000Z-testrun",
        log_path=tmp_path / "decisions.jsonl",
    )


@pytest.fixture
def client(monkeypatch, handles):
    """A `TestClient` whose lifespan installs the hand-built handles.

    Also stubs `route_by_rules`, because the real one needs the corpus. Tests
    that care about routing override it again.
    """
    monkeypatch.setattr(app_module, "build_handles", lambda: handles)
    monkeypatch.setattr(
        app_module,
        "route_by_rules",
        lambda question: RouterResult(
            route="vector",
            rule="R5-default",
            linked=[
                LinkedEntity(
                    canonical_name="deployer",
                    type="ActorRole",
                    display_name="deployer",
                    span="deployer",
                    via="canonical",
                    ambiguous=False,
                )
            ],
        ),
    )
    with TestClient(app) as test_client:
        yield test_client


def logged(handles) -> list[dict]:
    if not handles.log_path.exists():
        return []
    return [json.loads(line) for line in handles.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def test_the_app_starts_with_every_dependency_down(monkeypatch):
    """Startup records failures instead of raising.

    An app that cannot boot without Docker cannot be tested without Docker, and
    a server that exits because the database is not up yet cannot be started
    before it.
    """
    def explode(*args, **kwargs):
        raise ConnectionError("nothing is running")

    monkeypatch.setattr("src.ingest.graph_writer.connect", explode)
    monkeypatch.setattr("psycopg_pool.ConnectionPool", explode)
    monkeypatch.setattr("src.answer.generate.get_client", explode)
    monkeypatch.setattr("src.query.entity_linker.build_index", explode)

    built = app_module.build_handles()

    assert not built.ready
    assert set(built.errors) == {"neo4j", "postgres", "cohere", "index"}
    assert built.run_id, "a run id is assigned even when nothing else builds"


def test_health_is_200_and_names_the_missing_dependency(monkeypatch):
    degraded = Handles(run_id="r", errors={"neo4j": "ServiceUnavailable: down"})
    monkeypatch.setattr(app_module, "build_handles", lambda: degraded)

    with TestClient(app) as test_client:
        response = test_client.get("/health")

    assert response.status_code == 200, "a degraded service still answers /health"
    body = response.json()
    assert body["status"] == "degraded"
    assert body["neo4j"].startswith("ServiceUnavailable")
    assert body["postgres"] == "ok"


def test_health_is_ok_when_everything_built(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert {body["neo4j"], body["postgres"], body["cohere"], body["index"]} == {"ok"}


def test_ask_is_503_and_does_not_touch_the_pipeline_when_degraded(monkeypatch):
    degraded = Handles(run_id="r", pool=ExplodingPool(), errors={"postgres": "down"})
    monkeypatch.setattr(app_module, "build_handles", lambda: degraded)
    monkeypatch.setattr(
        app_module, "answer", lambda *a, **k: pytest.fail("answer() was called")
    )

    with TestClient(app) as test_client:
        response = test_client.post("/ask", json={"question": "anything"})

    assert response.status_code == 503
    assert response.json()["detail"] == "unavailable: postgres"


def test_the_handles_are_built_once_and_shared_across_requests(monkeypatch, handles):
    """The reason this step exists. Per-request construction is the slow path."""
    builds = []

    def build():
        builds.append(1)
        return handles

    monkeypatch.setattr(app_module, "build_handles", build)
    monkeypatch.setattr(app_module, "route_by_rules", lambda q: RouterResult(route="vector", rule="R5"))
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result())

    with TestClient(app) as test_client:
        for _ in range(3):
            assert test_client.post("/ask", json={"question": "q"}).status_code == 200

    assert len(builds) == 1, "the lifespan built handles more than once"
    assert handles.pool.checkouts == 3, "one connection checkout per request"


def test_answer_receives_the_lifespan_handles_and_a_pooled_connection(client, handles):
    """Identity, not truthiness. A `driver=None` would still return 200."""
    seen = {}

    def record(question, **kwargs):
        seen.update(kwargs)
        return a_result()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app_module, "answer", record)
        client.post("/ask", json={"question": "What is a deployer?"})

    assert seen["driver"] is handles.driver
    assert seen["client"] is handles.client
    assert seen["conn"] is handles.pool.conn, "the connection did not come from the pool"


def test_the_route_and_linked_entities_are_threaded_into_answer(client):
    """`answer()` must not re-route or re-link what the handler already did.

    `router.route()` would have discarded `RouterResult.linked` and made
    `graph_search` link the question a second time. This is the assertion that
    keeps the handler's reason for routing itself true.
    """
    seen = {}

    def record(question, **kwargs):
        seen.update(kwargs)
        return a_result()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app_module, "answer", record)
        client.post("/ask", json={"question": "What is a deployer?"})

    assert seen["route"] == "vector"
    assert [e.canonical_name for e in seen["linked"]] == ["deployer"]


def test_shutdown_closes_the_driver_and_the_pool(monkeypatch):
    class Closeable:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    driver, pool = Closeable(), Closeable()
    monkeypatch.setattr(
        app_module, "build_handles", lambda: Handles(driver=driver, pool=pool, client=object(), run_id="r")
    )

    with TestClient(app):
        pass

    assert driver.closed and pool.closed


def test_shutdown_does_not_raise_when_a_handle_close_fails():
    class Angry:
        def close(self):
            raise RuntimeError("no")

    app_module.close_handles(Handles(driver=Angry(), pool=Angry()))


# --------------------------------------------------------------------------
# The response contract
# --------------------------------------------------------------------------

def test_ask_returns_all_five_fields(client, monkeypatch):
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result())

    body = client.post("/ask", json={"question": "What is a deployer?"}).json()

    assert set(body) == {"answer", "citations", "route", "latency_ms", "cost_usd"}
    assert body["answer"].startswith("A deployer")
    assert body["route"] == "vector"
    assert body["cost_usd"] == pytest.approx(0.0043)
    assert body["latency_ms"] > 0


def test_latency_is_the_whole_handler_not_the_pipeline_sum(client, monkeypatch):
    """`AnswerResult.latency_ms` is five stage timers. The caller pays more.

    Routing, the connection checkout, serialisation and the fsync of the
    decision log are all outside those five timers and all inside the request.
    `query-path.md:418-420` already guards against reading two differently-scoped
    latencies as a regression; this pins which one `/ask` reports.
    """
    # Stage timers that are a fiction, so reporting them would be visible.
    stages = a_result(route_ms=99_000.0, generate_ms=1_000.0)

    def slow(*args, **kwargs):
        time.sleep(0.02)
        return stages

    monkeypatch.setattr(app_module, "answer", slow)
    reported = client.post("/ask", json={"question": "q"}).json()["latency_ms"]

    assert reported >= 20, "the handler did not measure its own wall clock"
    assert reported < stages.latency_ms / 10, "the pipeline's stage sum was reported instead"


def test_cost_usd_passes_through_unrounded(client, monkeypatch):
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result(cost_usd=0.00987654))
    assert client.post("/ask", json={"question": "q"}).json()["cost_usd"] == pytest.approx(0.00987654)


def test_cost_usd_serialises_as_null_when_a_component_is_unpriced(client, monkeypatch):
    """The widened arm, exercised rather than waited for.

    `price_of` returns None when a model has no rate and its docstring requires
    that None propagate rather than read as zero. With
    `RERANK_PRICE_PER_SEARCH = None` -- the honest state if the third-party rate
    is withdrawn -- a reranking route has an unknown total, and `null` is how the
    response says so.
    """
    monkeypatch.setattr("src.config.RERANK_PRICE_PER_SEARCH", None)
    from src.config import price_of_rerank

    assert price_of_rerank(1.0) is None, "the premise of this test no longer holds"

    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result(cost_usd=None))
    response = client.post("/ask", json={"question": "q"})

    assert response.status_code == 200
    assert '"cost_usd":null' in response.text.replace(" ", "")


def test_every_citation_field_survives_the_round_trip(client, monkeypatch):
    """All seven, including the three ADR-0014 added.

    `citation_label` is the string Phase 5 grades on. A response that dropped it
    would pass every test that only counted citations.
    """
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result())

    cited = client.post("/ask", json={"question": "q"}).json()["citations"][0]

    assert cited == {
        "chunk_id": "aia-art3-para1-def4",
        "start": 0,
        "end": 10,
        "text": "A deployer",
        "citation_label": "AIA Art. 3(4)",
        "source": "PASSAGE",
        "document_id": "d0",
    }


def test_all_three_routes_are_reported_verbatim(client, monkeypatch):
    """The response echoes what the pipeline took, not what the router asked for.

    Note the closure name: `answer()` is *called* with a `route=` keyword, so a
    fake capturing the loop variable as `route` would be silently overwritten by
    the handler's own argument and this test would pass for the wrong reason.
    """
    for taken in ("graph", "vector", "both"):
        monkeypatch.setattr(app_module, "answer", lambda *a, _r=taken, **k: a_result(route=_r))
        assert client.post("/ask", json={"question": "q"}).json()["route"] == taken


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question", ["", "   ", "\n\t "])
def test_an_empty_question_is_422(client, question, monkeypatch):
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: pytest.fail("answer() was called"))
    response = client.post("/ask", json={"question": question})
    assert response.status_code == 422
    assert response.json()["detail"] == "question is empty"


def test_a_missing_question_is_422(client):
    assert client.post("/ask", json={}).status_code == 422


def test_no_context_is_422_not_502(client, monkeypatch):
    """Nothing failed. There was nothing to ground an answer in.

    The distinction is a subclass rather than a message match, so this test
    fails loudly if `NoContextError` is ever collapsed back into its parent.
    """
    def raise_no_context(*args, **kwargs):
        raise NoContextError("route 'graph' produced no documents for 'q'")

    monkeypatch.setattr(app_module, "answer", raise_no_context)
    response = client.post("/ask", json={"question": "q"})

    assert response.status_code == 422
    assert "no documents" in response.json()["detail"]


def test_a_pipeline_failure_is_502(client, monkeypatch):
    def raise_answer_path(*args, **kwargs):
        raise AnswerPathError("vector retrieval failed: connection reset")

    monkeypatch.setattr(app_module, "answer", raise_answer_path)
    response = client.post("/ask", json={"question": "q"})

    assert response.status_code == 502
    assert "vector retrieval failed" in response.json()["detail"]


def test_an_unroutable_question_is_422(client, monkeypatch):
    monkeypatch.setattr(app_module, "route_by_rules", lambda q: RouterResult(route=None, error="unparseable"))
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: pytest.fail("answer() was called"))

    assert client.post("/ask", json={"question": "q"}).status_code == 422


def test_a_router_error_is_422(client, monkeypatch):
    def raise_router(question):
        raise RouterError("the router could not classify this")

    monkeypatch.setattr(app_module, "route_by_rules", raise_router)
    assert client.post("/ask", json={"question": "q"}).status_code == 422


def test_an_unexpected_exception_is_502_and_leaks_no_dsn(client, monkeypatch):
    """A driver's error message is not an API contract, and it carries secrets."""
    from src.config import settings

    def raise_raw(*args, **kwargs):
        raise RuntimeError(f"connection failed: {settings.postgres_dsn}")

    monkeypatch.setattr(app_module, "answer", raise_raw)
    response = client.post("/ask", json={"question": "q"})

    assert response.status_code == 502
    assert response.json()["detail"] == "answer path failed: RuntimeError"
    assert settings.postgres_dsn not in response.text


def test_redaction_removes_the_dsn_and_the_password():
    from src.config import settings

    password = settings.postgres_dsn.split("://", 1)[1].rsplit("@", 1)[0].split(":", 1)[1]
    text = f"OperationalError on {settings.postgres_dsn} using {password}"

    cleaned = app_module._redact(text)

    assert settings.postgres_dsn not in cleaned
    assert password not in cleaned


# --------------------------------------------------------------------------
# The decision log
# --------------------------------------------------------------------------

def test_one_row_is_logged_per_request(client, handles, monkeypatch):
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result())

    client.post("/ask", json={"question": "first"})
    client.post("/ask", json={"question": "second"})

    rows = logged(handles)
    assert [row["question"] for row in rows] == ["first", "second"]
    assert {row["run_id"] for row in rows} == {handles.run_id}, "one run id per process"
    assert rows[0]["router"] == "rules" and rows[0]["rule"] == "R5-default"
    assert rows[0]["linked"] == ["deployer"]
    assert rows[0]["outcome"]["citations"] == 1
    assert rows[0]["outcome"]["status"] == 200
    assert rows[0]["latency_ms"] > 0


def test_a_failed_request_is_logged_too(client, handles, monkeypatch):
    """Phase 5 needs the failures at least as much as the successes."""
    def raise_answer_path(*args, **kwargs):
        raise AnswerPathError("graph retrieval failed")

    monkeypatch.setattr(app_module, "answer", raise_answer_path)
    client.post("/ask", json={"question": "doomed"})

    row = logged(handles)[-1]
    assert row["error"].startswith("AnswerPathError")
    assert row["outcome"]["status"] == 502
    assert row["route"] == "vector", "the route it took before failing is still evidence"


def test_the_log_is_appended_never_truncated(client, handles, monkeypatch):
    """`failure-notes.md` §3 is still OPEN about a writer that opened `w`."""
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result())
    for i in range(5):
        client.post("/ask", json={"question": f"q{i}"})
    assert len(logged(handles)) == 5


def test_a_logging_failure_does_not_fail_a_served_request(client, handles, monkeypatch):
    monkeypatch.setattr(app_module, "answer", lambda *a, **k: a_result())

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app_module.decision_log, "append", explode)

    assert client.post("/ask", json={"question": "q"}).status_code == 200


# --------------------------------------------------------------------------
# The artifact -- pure, no DB, no key, no spend
# --------------------------------------------------------------------------

def rows():
    """The committed sweep, or skip. Every artifact test goes through here.

    `load_artifact()` raises `SystemExit` on a missing file, which is right for
    a CLI and reddens a test suite. Two tests originally called it directly and
    failed on a clean checkout for a reason that had nothing to do with them.
    """
    from src.api.ask_eval import ARTIFACT, load_artifact

    if not ARTIFACT.exists():
        pytest.skip(f"{ARTIFACT.name} not written yet; run --eval --refresh")
    return load_artifact()


def board():
    from src.api.ask_eval import scoreboard

    return scoreboard(rows())


def test_the_scoreboard_is_pure(monkeypatch):
    """The published table is recomputable with everything switched off.

    The property that lets `docs/metrics/answer-path.md` and this file quote the
    same numbers. Enforced by making the two constructors explode: a
    `scoreboard()` that reached for a database would fail here rather than in
    six months on someone else's laptop.
    """
    def explode(*args, **kwargs):
        raise AssertionError("scoreboard() touched a live dependency")

    monkeypatch.setattr("src.index.pgvector_schema.connect", explode)
    monkeypatch.setattr("src.ingest.graph_writer.connect", explode)

    assert board()["served"] > 0


def test_every_eval_question_was_served():
    served = rows()
    computed = board()
    assert computed["rows"] == 23, "the sweep did not cover the eval set"
    assert not computed["failed"], f"rows failed: {computed['failed']}"
    assert len({row["question_id"] for row in served}) == 23


def test_the_per_route_ns_reconcile_with_adr_0012s_recorded_miss():
    """The gold labels are 13 / 9 / 1. What `/ask` serves is 14 / 8 / 1.

    The difference is one row, `th-004`, and it is not a defect found here: it is
    ADR-0012's single recorded miss, left unrepaired on purpose because the fix
    that repairs it moves `oos-002` the wrong way (`query-path.md:716-717`). The
    first version of this test asserted `taken == gold` -- that the router is
    perfect -- which is a claim the project had already measured to be false.

    So it is pinned as a *named* disagreement rather than smoothed away. If the
    router improves, this test fails and the per-route ns in
    `docs/metrics/answer-path.md` have to be re-derived, which is the point.
    """
    served = rows()
    gold = Counter(row["gold_route"] for row in served)
    taken = Counter(row["route"] for row in served if row["status_code"] == 200)
    missed = [r["question_id"] for r in served if r["route"] != r["gold_route"]]

    assert dict(gold) == {"vector": 13, "both": 9, "graph": 1}
    assert dict(taken) == {"vector": 14, "both": 8, "graph": 1}
    assert missed == ["th-004"], "a route disagreement other than ADR-0012's appeared"


def test_cost_is_non_zero_on_every_route():
    """The verification row's own wording. A $0.00 route would mean unbilled."""
    for route, stats in board()["per_route"].items():
        assert stats["cost_median"] is not None, f"{route} priced nothing"
        assert stats["cost_median"] > 0, f"{route} cost $0.00, which no route does live"
        assert stats["unpriced"] == 0, f"{route} has {stats['unpriced']} unpriced rows"


def test_no_per_route_p95_is_published():
    """The pre-registered refusal, made structural.

    n is 13 / 9 / 1. A per-route p95 over 9 observations is the maximum with a
    better name, and over 1 it is the observation. The pooled figure is computed
    once and is named `pooled` so it cannot be quoted as a per-route number.
    """
    computed = board()
    for stats in computed["per_route"].values():
        assert not any(key.endswith("p95") for key in stats)
    assert computed["pooled"]["latency_p95"] is not None


def test_the_served_latency_is_bounded_by_what_the_client_observed():
    """`wall_ms >= latency_ms` on every row, or the two columns measure nothing.

    The handler's clock starts inside the request and stops before serialisation,
    so the client must always see at least as much time as the server reported.
    A row that violated it would mean one of the two timers is wrong.
    """
    for row in rows():
        if row["status_code"] == 200:
            assert row["wall_ms"] >= row["latency_ms"], row["question_id"]


# The table published in `docs/metrics/answer-path.md` under "The live figure
# Step 7 owed". Here so that a re-run which moves a number fails the suite
# instead of quietly leaving the document wrong -- the same job
# `test_the_retention_curve_is_what_adr_0014_adopts_on` does for ADR-0014.
PUBLISHED = {
    "vector": {"n": 14, "cost_median": 0.0063, "latency_p50": 3996},
    "both": {"n": 8, "cost_median": 0.0116, "latency_p50": 3291},
    "graph": {"n": 1, "cost_median": 0.0084, "latency_p50": 7463},
}
PUBLISHED_TOTAL_COST = 0.1793
PUBLISHED_POOLED_P50 = 3419
PUBLISHED_POOLED_P95 = 20076


def test_the_published_per_route_table_is_what_the_artifact_says():
    computed = board()["per_route"]

    assert set(computed) == set(PUBLISHED)
    for route, published in PUBLISHED.items():
        got = computed[route]
        assert got["n"] == published["n"], route
        assert got["cost_median"] == pytest.approx(published["cost_median"], abs=5e-5), route
        assert got["latency_p50"] == pytest.approx(published["latency_p50"], abs=1), route


def test_the_published_pooled_figures_are_what_the_artifact_says():
    pooled = board()["pooled"]

    assert pooled["cost_total"] == pytest.approx(PUBLISHED_TOTAL_COST, abs=5e-5)
    assert pooled["latency_p50"] == pytest.approx(PUBLISHED_POOLED_P50, abs=1)
    assert pooled["latency_p95"] == pytest.approx(PUBLISHED_POOLED_P95, abs=1)


def test_the_graph_route_reconciles_with_the_replayed_sweep_table():
    """$0.0084 live and $0.0084 replayed, and it is not a coincidence.

    Route `graph` never enters the vector path, so the sweep's $0.00 replay of
    the vector half removed nothing from it. That makes this one row the control
    for the other two: `vector` at 1.47x and `both` at 1.17x are the embed and
    rerank round trip the replay skipped, not a drift in how cost is computed.
    If this row ever stops matching, the two multipliers stop meaning that.
    """
    replayed_graph = 0.0084  # docs/metrics/answer-path.md, the Step 6 sweep table
    assert board()["per_route"]["graph"]["cost_median"] == pytest.approx(
        replayed_graph, abs=5e-5
    )


def test_percentile_is_nearest_rank_and_never_invents_a_value():
    from src.api.ask_eval import _p

    observed = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert _p(observed, 0.95) in observed
    assert _p([], 0.95) is None
    assert _p([7.0], 0.95) == 7.0


# --------------------------------------------------------------------------
# Live -- skips without containers and a key
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def live_client(loaded, indexed):
    """A real app against real stores. Skips unless everything is present."""
    from src.config import settings

    if not settings.cohere_api_key:
        pytest.skip(f"{settings.cohere_api_key_var} is not set; skipping live API tests")

    with TestClient(app) as test_client:
        health = test_client.get("/health").json()
        if health["status"] != "ok":
            pytest.skip(f"app degraded: {health}")
        yield test_client


@pytest.mark.parametrize("question_id,expected_route", [
    ("sh-001", "vector"),
    ("th-001", "both"),
    ("ag-001", "graph"),   # the only `graph`-routed row in the set
])
def test_live_ask_answers_with_validated_citations(live_client, question_id, expected_route):
    """All three routes, end to end, against live stores. Costs money.

    The questions are read out of `eval/eval-questions.jsonl` by id rather than
    typed here. A hand-written question is a hand-written route: the first
    version of this test invented one and asserted `graph`, and the rules router
    sent it to `vector` -- correctly, because nothing in its shape triggered R3.
    Reading the gold row means the expectation is the router's own label.
    """
    from src.answer.citation_validator import LABEL_RE
    from src.api.ask_eval import load_questions

    row = next(r for r in load_questions() if r["id"] == question_id)
    assert row["route"] == expected_route, "the gold label moved; update this test"

    response = live_client.post("/ask", json={"question": row["question"]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"answer", "citations", "route", "latency_ms", "cost_usd"}
    assert body["route"] == expected_route
    assert body["answer"].strip()
    assert body["latency_ms"] > 0
    assert body["cost_usd"] is not None and body["cost_usd"] > 0
    assert body["citations"], "a grounded answer with no citations is not grounded"
    for cited in body["citations"]:
        assert LABEL_RE.fullmatch(cited["citation_label"]), cited["citation_label"]
        assert body["answer"][cited["start"]:cited["end"]] == cited["text"]
