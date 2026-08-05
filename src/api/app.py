"""FastAPI app.

`POST /ask` -> `{answer, citations[], route, latency_ms, cost_usd}` with
server-side validated citations.

WHAT THIS MODULE DOES AND DOES NOT DO.

`src/answer/answer_path.py:6` states the contract: *"Step 7's `app.py` calls
`answer()` and does nothing else."* It holds. There is no retrieval, no
assembly, no prompt and no validation here -- all five stages live behind
`answer()`, which was built taking `driver=`/`conn=`/`client=` precisely so this
module could own their lifecycle and nothing more.

THE THREE HANDLES, AND WHY THEY ARE BUILT ONCE.

Every constructor on the path is expensive in a way that is invisible until it
runs per request:

  * `graph_writer.connect()` calls `verify_connectivity()` on every call
    (`graph_writer.py:274`) -- a Bolt round trip before any query.
  * `pgvector_schema.connect()` runs `CREATE EXTENSION IF NOT EXISTS vector` and
    `register_vector` per connection (`pgvector_schema.py:46-50`).
  * Four modules each build their own `cohere.ClientV2`, and each one builds an
    `httpx` client with a fresh connection pool and a fresh TLS handshake.
  * `entity_linker.build_index()` parses 1.8 MB of extractions. It is
    `lru_cache`d and its docstring names this request path as the reason.

The phase plan calls per-request construction of all three "the obvious way to
make a fast path slow", so they are built in the lifespan and handed down.

WHY A POOL AND NOT A CONNECTION.

The handler is `def`, not `async def`, so FastAPI runs it in a threadpool and two
concurrent requests are two threads. One shared psycopg connection would
serialise them at best. `retriever.py:12-18` was already written against the
pooled case -- *"At Step 7 that connection comes out of a pool and the next
request inherits it"* -- which is also why `configure` below reproduces
`pgvector_schema.connect()` exactly rather than approximately: a pooled
connection without `register_vector` fails the `%s::vector` cast in
`retriever.search_sql`, and a pooled connection without `autocommit` changes the
transaction semantics every call site was measured under.

WHY STARTUP DOES NOT FAIL.

Each handle is built inside its own `try`, and a failure is recorded rather than
raised. Three reasons, in increasing order of importance: `/health` should be
able to say *which* dependency is missing; `tests/conftest.py`'s entire design is
DB-backed tests skipping rather than reddening, and an app that cannot be
imported without Docker cannot be tested without Docker; and a server that exits
on a database that is not up yet is a server that cannot be restarted first.

WHY THE HANDLER ROUTES INSTEAD OF LETTING `answer()` DO IT.

Two things are wanted from routing and neither existing entry point gives both.
`router.route()` (`router.py:333`) logs the decision -- it takes a `run_id` for
exactly this caller -- but returns a bare `Route` and discards
`RouterResult.linked`, so `graph_search` would re-link the question from
scratch. `answer()` keeps the linked entities but calls `route_by_rules`
directly and writes no log row. Calling `route_by_rules` once here and threading
both `route=` and `linked=` into `answer()` gets the log row *and* the single
link. `router.route()` is deliberately left untouched: ADR-0012's adopted 21 of
22 was measured through it, and editing it silently re-measures Step 3.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.answer.answer_path import AnswerPathError, AnswerResult, NoContextError, answer
from src.config import settings
from src.query import decision_log
from src.query.decision_log import Decision
from src.query.router import RouterError, route_by_rules
from src.schemas import AskRequest, AskResponse

__all__ = ["Handles", "app", "ask", "health"]

# Pool bounds. `min_size=1` so the first request does not pay the connect; a
# `max_size` above the number of threads uvicorn will run a sync handler in buys
# nothing, and this is a single-node demo service, not a fleet.
POOL_MIN, POOL_MAX = 1, 4

# Seconds to wait for the pool to come up at startup. Long enough for a container
# that is still starting, short enough that a missing database is a degraded
# `/health` within a few seconds rather than a hang.
POOL_OPEN_TIMEOUT = 5.0


@dataclass
class Handles:
    """Everything built once per process, plus what failed to build.

    `errors` maps a dependency name to the exception string. It is what
    `/health` reports and what `/ask` refuses on, so a missing dependency is
    named to the caller rather than surfacing as a `NoneType` attribute error
    four frames down.
    """

    driver: Any = None
    pool: Any = None
    client: Any = None
    run_id: str = ""
    log_path: Path | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not self.errors


def _redact(text: str) -> str:
    """Remove the Postgres DSN and its password from an error string.

    `settings.postgres_dsn` is `postgresql://user:password@host/db` and psycopg
    puts the whole thing in some `OperationalError` messages. `/health` reports
    these strings and the decision log persists them, so redaction happens at the
    point they are captured rather than at each of the places they are read.
    """
    dsn = settings.postgres_dsn
    if dsn and dsn in text:
        text = text.replace(dsn, "<postgres_dsn>")
    if "://" in dsn and "@" in dsn:
        credentials = dsn.split("://", 1)[1].rsplit("@", 1)[0]
        if ":" in credentials:
            password = credentials.split(":", 1)[1]
            if password and password in text:
                text = text.replace(password, "<redacted>")
    return text


def _describe(exc: BaseException) -> str:
    return _redact(f"{type(exc).__name__}: {exc}")


def _configure(conn: Any) -> None:
    """Make a pooled connection identical to `pgvector_schema.connect()`.

    Runs once per physical connection, not per checkout. The `CREATE EXTENSION`
    is the same idempotent statement `pgvector_schema.connect()` issues for the
    same reason -- the type cannot be registered before the extension exists.
    """
    from pgvector.psycopg import register_vector

    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)


def build_handles() -> Handles:
    """Build the three handles, recording failures instead of raising."""
    handles = Handles(run_id=decision_log.new_run_id())

    try:
        from src.ingest.graph_writer import connect

        handles.driver = connect()
    except Exception as exc:  # noqa: BLE001 -- the reason is reported, not swallowed
        handles.errors["neo4j"] = _describe(exc)

    pool = None
    try:
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            kwargs={"autocommit": True, "connect_timeout": int(POOL_OPEN_TIMEOUT)},
            configure=_configure,
            # Default 300 s. A worker that keeps retrying a database that is not
            # there holds a thread the interpreter then cannot join at exit --
            # "couldn't stop thread pool-1-worker-0" on every degraded start.
            reconnect_timeout=POOL_OPEN_TIMEOUT,
            open=False,
        )
        pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT)
        handles.pool = pool
    except Exception as exc:  # noqa: BLE001
        handles.errors["postgres"] = _describe(exc)
        # A pool that failed to open still has worker threads running. Without
        # this the process prints "couldn't stop thread pool-1-worker-0" at exit
        # and every test that starts a degraded app leaks a thread.
        if pool is not None:
            try:
                pool.close()
            except Exception:  # noqa: BLE001,S110
                pass

    try:
        # `generate.get_client()`, not `embedder.get_client()`: the latter raises
        # `SystemExit` on a missing key (`embedder.py:100-103`), which would take
        # the worker down instead of degrading it.
        from src.answer.generate import get_client

        handles.client = get_client()
    except Exception as exc:  # noqa: BLE001
        handles.errors["cohere"] = _describe(exc)

    try:
        from src.query.entity_linker import build_index

        build_index()  # lru_cache(maxsize=1); warmed so no request pays the 0.17 s
    except Exception as exc:  # noqa: BLE001
        handles.errors["index"] = _describe(exc)

    return handles


def close_handles(handles: Handles) -> None:
    """Release what was built. Best effort: shutdown must not raise."""
    for handle in (handles.driver, handles.pool):
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:  # noqa: BLE001,S110 -- nothing useful to do at shutdown
            pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.handles = build_handles()
    try:
        yield
    finally:
        close_handles(app.state.handles)


app = FastAPI(title="kg-rag-eu-ai-act", lifespan=lifespan)


def _handles(request: Request) -> Handles:
    """The process's handles, or an empty set if the lifespan never ran."""
    return getattr(request.app.state, "handles", None) or Handles()


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    """`ok` when every dependency built, `degraded` naming the ones that did not.

    Returns 200 either way. A degraded service is still serving `/health`, and
    the point of the endpoint is to say *why* `/ask` is answering 503 -- a
    non-200 that carries no detail would make an operator go read logs to learn
    something this body already knows.
    """
    handles = _handles(request)
    body = {"status": "ok" if handles.ready else "degraded"}
    for name in ("neo4j", "postgres", "cohere", "index"):
        body[name] = handles.errors.get(name, "ok")
    return body


def _log(handles: Handles, decision: Decision) -> None:
    """Append one decision. A logging failure must not fail a served request.

    The append is `flush()` + `os.fsync()` per row (`decision_log.py:98-99`) --
    a blocking write on the request path, kept because the roadmap's *"it cannot
    be reconstructed later"* is the whole reason the log exists, and a few
    milliseconds against a multi-second request is the right trade. It is
    measured rather than assumed: see `docs/metrics/answer-path.md`.
    """
    try:
        decision_log.append(decision, handles.log_path)
    except Exception:  # noqa: BLE001,S110
        pass


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, request: Request) -> AskResponse:
    """Route -> retrieve -> assemble -> generate -> validate citations."""
    start = time.perf_counter()

    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is empty")

    handles = _handles(request)
    if not handles.ready:
        # Names the dependencies, not the exceptions. `/health` carries the
        # reasons; an error body is the wrong place to put a driver's idea of a
        # useful message.
        raise HTTPException(
            status_code=503,
            detail=f"unavailable: {', '.join(sorted(handles.errors))}",
        )

    decision = Decision(
        run_id=handles.run_id, question=question, router="rules", route=None
    )
    status = 200
    result: AnswerResult | None = None
    try:
        routed = route_by_rules(question)
        if routed.route is None:
            raise RouterError(f"no route for {question!r}")
        decision.route, decision.rule = routed.route, routed.rule
        decision.linked = [e.canonical_name for e in routed.linked or []]

        with handles.pool.connection() as conn:
            result = answer(
                question,
                route=routed.route,
                linked=routed.linked,
                driver=handles.driver,
                conn=conn,
                client=handles.client,
            )
        decision.cost_usd = result.cost_usd
        decision.outcome = {
            "citations": len(result.citations),
            "rejected": result.rejected,
            "uncited": result.uncited,
            "regenerated": result.regenerated,
            "documents_sent": result.documents_sent,
            "graph_sent": result.graph_sent,
            "passage_sent": result.passage_sent,
        }
    except (RouterError, NoContextError) as exc:
        # Nothing failed. The question could not be routed, or the route it took
        # found nothing to ground an answer in -- both are facts about the
        # request, which is what 4xx means.
        status, decision.error = 422, _describe(exc)
        raise HTTPException(status_code=422, detail=_redact(str(exc))) from exc
    except AnswerPathError as exc:
        status, decision.error = 502, _describe(exc)
        raise HTTPException(status_code=502, detail=_redact(str(exc))) from exc
    except Exception as exc:
        # The type name only. A psycopg or neo4j error can carry the DSN, and
        # `settings.postgres_dsn` has the password in it; the full string goes to
        # the log, redacted, and not to the caller.
        status, decision.error = 502, _describe(exc)
        raise HTTPException(
            status_code=502, detail=f"answer path failed: {type(exc).__name__}"
        ) from exc
    finally:
        # In the `finally` so a failed request is logged too. Phase 5 needs the
        # failures at least as much as the successes.
        decision.latency_ms = (time.perf_counter() - start) * 1000
        if decision.outcome is None:
            decision.outcome = {"status": status}
        else:
            decision.outcome["status"] = status
        _log(handles, decision)

    return AskResponse(
        answer=result.answer,
        citations=result.citations,
        route=result.route,
        # Around the whole handler, not around the model call: routing, the
        # connection checkout and the log write are latency the caller pays.
        latency_ms=decision.latency_ms,
        cost_usd=result.cost_usd,
    )


@app.exception_handler(AnswerPathError)
def _answer_path_error(request: Request, exc: AnswerPathError) -> JSONResponse:
    """Backstop for an `AnswerPathError` raised outside the handler's own try.

    The handler maps every case it can reach. This exists so that if one ever
    escapes -- from a dependency, a background path, a future endpoint -- the
    client gets a 502 with a message rather than a 500 with a traceback.
    """
    status = 422 if isinstance(exc, NoContextError) else 502
    return JSONResponse(status_code=status, content={"detail": str(exc)})
