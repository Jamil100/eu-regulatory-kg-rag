"""The graph retrieval path, end to end: question -> statements with citations.

select (template_selector) -> validate (graph_query) -> execute (Neo4j) ->
label (pgvector) -> render (path_to_prose). The vector path's counterpart is
`retriever.retrieve` + `reranker.rerank`; this returns the same `ContextDoc`
type so Step 6's `assemble` can dedupe across both.

THIS IS THE ONE PLACE THE TWO STORES MEET, AND THAT IS WHY IT IS ITS OWN MODULE.

`graph_query` is the pure Neo4j execution layer and must not grow a psycopg
import; `path_to_prose` is a pure rendering function that takes labels as data.
The join between them -- Neo4j gives `source_chunk_id`, pgvector holds
`citation_label` -- lives here and nowhere else.

`citation_label` is SELECTed, never recomputed. It is `NOT NULL UNIQUE` in
src/index/schema.sql:46 precisely so the answer path reads it, and
`retriever.py:170` records the same rule for the vector side. `Chunk` can derive
a label from its own fields, but deriving it here would mean two code paths
producing the string Phase 5 grades on, which is the "a key derived from a field,
and the field then dropped" row of the recurrence tracker waiting to happen.

ONE LABEL LOOKUP PER REQUEST, NOT ONE PER STATEMENT. `obligations_for_role
('deployer')` alone yields 60 rows whose provenance spans dozens of chunks; a
per-statement SELECT would turn one query into dozens. `label_map` takes the
whole plan's chunk ids at once.

Usage:
    python -m src.query.graph_path --question "..."
    python -m src.query.graph_path --question "..." --json
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.answer.path_to_prose import MAX_PROVENANCE, ProseError, path_to_prose
from src.query.entity_linker import LinkedEntity, LinkIndex
from src.query.graph_query import run_template
from src.query.template_selector import TemplateCall, select
from src.schemas import ContextDoc

if TYPE_CHECKING:
    from neo4j import Driver
    from psycopg import Connection

__all__ = ["GraphPathError", "GraphResult", "graph_search", "label_map"]


class GraphPathError(RuntimeError):
    """The graph path could not produce documents.

    Deliberately not `SystemExit`, for the reason `RouterError` (router.py:101)
    and `RetrieverError` (retriever.py:58) both give -- this runs inside a
    FastAPI worker at Step 7 and one question getting a 400 must not take the
    process down with it.
    """


@dataclass
class GraphResult:
    """One graph retrieval, with everything Step 7's cost accumulator needs.

    Latency is split for the same reason `RetrievalResult` splits it: the parts
    have different orders of magnitude and different causes. Selection is local
    and free, `query_ms` is Neo4j, `label_ms` is Postgres. One total would hide
    which moved.

    `empty_calls` is carried rather than derived because it is the finding of
    ADR-0013: a call can pass `validate()` -- ADR-0002's boundary -- and still
    match no node, because validation checks parameter names and the graph
    matches parameter values.
    """

    docs: list[ContextDoc] = field(default_factory=list)
    plan: list[TemplateCall] = field(default_factory=list)
    rows_returned: int = 0
    empty_calls: int = 0
    selector: str = ""
    rule: str | None = None
    cost_usd: float | None = 0.0
    select_ms: float = 0.0
    query_ms: float = 0.0
    label_ms: float = 0.0
    render_ms: float = 0.0

    @property
    def latency_ms(self) -> float:
        return self.select_ms + self.query_ms + self.label_ms + self.render_ms


def label_map(chunk_ids: Iterable[str], conn: Connection | None = None) -> dict[str, str]:
    """chunk_id -> citation_label for the ids given, in one round trip.

    `conn` follows the house lifecycle -- caller-owned if passed, closed here if
    not -- so Step 7 hands in a pooled connection and this module owns nothing.
    Ids with no row are simply absent from the result; `path_to_prose` is what
    decides that a missing label is fatal, because it is the function that knows
    whether the id was going to be cited.
    """
    ids = list(dict.fromkeys(chunk_ids))
    if not ids:
        return {}

    owned = conn is None
    if conn is None:
        from src.index.pgvector_schema import connect

        conn = connect()
    try:
        rows = conn.execute(
            "SELECT chunk_id, citation_label FROM chunks WHERE chunk_id = ANY(%s)", (ids,)
        ).fetchall()
    finally:
        if owned:
            conn.close()
    return {chunk_id: label for chunk_id, label in rows}


def graph_search(
    question: str,
    *,
    driver: Driver | None = None,
    conn: Connection | None = None,
    linked: list[LinkedEntity] | None = None,
    index: LinkIndex | None = None,
    max_provenance: int = MAX_PROVENANCE,
) -> GraphResult:
    """Select templates, execute them, and render the rows as citable statements.

    `linked` is passed straight through to the selector so a request that has
    already linked the question (the router does, and keeps the result on
    `RouterResult.linked`) does not link it a second time.

    A template that returns no rows is counted, not raised on: an anchor the
    graph does not hold is a fact about the question, and the other calls in the
    plan may still answer it. A template that fails to *execute* is different and
    does raise, because that is a broken query rather than an empty one.
    """
    started = time.perf_counter()
    selection = select(question, index, linked)
    select_ms = (time.perf_counter() - started) * 1000

    if not selection.plan:
        return GraphResult(
            plan=[],
            selector=selection.rule or "",
            rule=selection.rule,
            cost_usd=selection.cost_usd,
            select_ms=select_ms,
        )

    owned_driver = driver is None
    if driver is None:
        from src.ingest.graph_writer import connect

        driver = connect()

    results: list[tuple[TemplateCall, list[dict]]] = []
    rows_returned = 0
    empty_calls = 0
    started = time.perf_counter()
    try:
        for call in selection.plan:
            try:
                rows = run_template(call.template, call.params, driver)
            except Exception as exc:  # noqa: BLE001 -- re-raised as this layer's type
                raise GraphPathError(
                    f"{call.template}{call.params} failed: {type(exc).__name__}: {exc}"
                ) from exc
            rows_returned += len(rows)
            empty_calls += not rows
            if rows:
                results.append((call, rows))
    finally:
        if owned_driver:
            driver.close()
    query_ms = (time.perf_counter() - started) * 1000

    # Every chunk the whole plan might cite, resolved in one SELECT.
    from src.query.graph_query import provenance_of

    wanted = [c for _, rows in results for row in rows for c in provenance_of(row)]
    started = time.perf_counter()
    labels = label_map(wanted, conn)
    label_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    docs: list[ContextDoc] = []
    seen: set[str] = set()
    for call, rows in results:
        try:
            rendered = path_to_prose(
                rows, call.template, labels=labels, max_provenance=max_provenance
            )
        except ProseError as exc:
            raise GraphPathError(str(exc)) from exc
        for doc in rendered:
            # Two templates in one plan can render the same statement -- the
            # role and system obligation legs overlap on shared duties. Dedupe on
            # the text, keeping the first, so the prompt does not pay twice.
            if doc.text not in seen:
                seen.add(doc.text)
                docs.append(doc)
    render_ms = (time.perf_counter() - started) * 1000

    return GraphResult(
        docs=docs,
        plan=selection.plan,
        rows_returned=rows_returned,
        empty_calls=empty_calls,
        selector=selection.rule or "",
        rule=selection.rule,
        cost_usd=selection.cost_usd,
        select_ms=select_ms,
        query_ms=query_ms,
        label_ms=label_ms,
        render_ms=render_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-n", type=int, default=20, help="statements to print")
    args = parser.parse_args()

    try:
        result = graph_search(args.question)
    except GraphPathError as exc:
        print(f"graph path failed: {exc}")
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "plan": [{"template": c.template, "params": c.params, "rule": c.rule}
                             for c in result.plan],
                    "rows_returned": result.rows_returned,
                    "empty_calls": result.empty_calls,
                    "docs": [d.model_dump() for d in result.docs],
                    "latency_ms": round(result.latency_ms, 2),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"plan ({result.rule}):")
    for call in result.plan:
        print(f"    {call.template}  {call.params}")
    if not result.plan:
        print("    (nothing to traverse from)")
    print(
        f"\n{result.rows_returned} rows -> {len(result.docs)} statements  "
        f"({result.empty_calls} call(s) matched no node)\n"
        f"select {result.select_ms:.1f} ms, query {result.query_ms:.0f} ms, "
        f"label {result.label_ms:.0f} ms, render {result.render_ms:.1f} ms\n"
    )
    for doc in result.docs[: args.n]:
        flag = " [derived]" if doc.derived else ""
        print(f"  {doc.citation_label:<26} {doc.text}{flag}")
    if len(result.docs) > args.n:
        print(f"  ... {len(result.docs) - args.n} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
