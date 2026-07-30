"""Shared fixtures.

The graph fixtures skip rather than fail when Neo4j is unreachable: the database
lives in a container that CI does not run, and a test suite that goes red on a
missing service teaches people to ignore it.
"""

from __future__ import annotations

import pytest

from src.ingest import graph_writer


@pytest.fixture(scope="session")
def graph() -> dict:
    """The pure derivation. No database, so this always runs."""
    return graph_writer.build_graph()


@pytest.fixture(scope="session")
def driver():
    """A live Neo4j driver, or skip."""
    try:
        connection = graph_writer.connect()
    # Deliberately broad: the point is to skip on *any* reason the database is not
    # there -- driver not installed, container down, wrong credentials, WSL asleep.
    # Narrowing this would turn some of those into a red suite instead of a skip.
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unreachable ({type(exc).__name__}); skipping graph tests")
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def loaded(driver, graph):
    """Skip unless the graph in the database matches the current derivation.

    Guards against asserting query behaviour against a stale or empty database and
    reporting it as a template failure.
    """
    counts = graph_writer.graph_counts(driver)
    if counts["nodes"] != graph["stats"]["nodes"] or counts["edges"] != graph["stats"]["edges"]:
        pytest.skip(
            f"database holds {counts['nodes']} nodes / {counts['edges']} edges, "
            f"derivation expects {graph['stats']['nodes']} / {graph['stats']['edges']}; "
            "run `python -m src.ingest.graph_writer --apply`"
        )
    return driver
