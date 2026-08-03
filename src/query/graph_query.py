"""Execute a Cypher template and return plain Python.

`cypher_templates` stays a pure string library with no driver in it; this is the
module that binds parameters, runs the query, and converts the result. The split
is deliberate -- `cypher_templates` imports without `neo4j` installed, so the
template library and its parameter contract stay testable with no database.

TWO THINGS THIS ENFORCES.

  * ADR-0002 in code rather than in prose. The model picks a template *name* and
    fills *declared* parameters; both are validated against TEMPLATES and
    TEMPLATE_PARAMS and both raise before a driver is touched. `get_template` is
    a bare dict lookup with no validation, which is fine for a test that already
    knows the name and useless as a security boundary.
  * No `neo4j.Record` escapes. Rows come back as plain dicts -- nodes as their
    property maps, paths as a nodes/relationship-types pair. `path_to_prose` is
    already declared as `paths: list[dict]` (src/answer/path_to_prose.py), and a
    pure conversion is one that can be tested against a fake record instead of a
    container.

The driver is injectable and falls back to `graph_writer.connect()`, matching
`conn = conn or connect()` in src/index/embedder.py and recall_harness.py. That
is also what lets the `loaded` test fixture -- which *is* a driver -- pass its
own in. Note that `connect()` runs `verify_connectivity()` on every call, so a
request path should open one driver and pass it down rather than let this module
build one per query; wiring that lifecycle is Phase 3 Step 7's job, not this
module's.

Usage:
    python -m src.query.graph_query --template obligations_for_role --param role=deployer
    python -m src.query.graph_query --template path_between \\
        --param entity_a=deployer --param entity_b=GDPR --json
    python -m src.query.graph_query --baseline     # the six regression anchors
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

from src.query.cypher_templates import TEMPLATE_PARAMS, TEMPLATES

if TYPE_CHECKING:  # keep `neo4j` off the import path of the pure functions
    from neo4j import Driver

# The six anchors from docs/metrics/graph-load.md. `enforcement_chain` is absent
# because its parameter has to be discovered from the graph -- only 216 of 1,179
# obligations carry ENFORCED_BY -- so inventing one here would test the parameter
# rather than the template.
BASELINE_CASES: list[tuple[str, dict[str, str], int]] = [
    ("obligations_for_role", {"role": "deployer"}, 60),
    ("obligations_for_system", {"system_type": "high risk ai system"}, 169),
    ("definition_of", {"term": "provider"}, 1),
    ("definition_of", {"term": "computational resources"}, 1),
    ("cross_regulation", {"article": "aia art. 2(7)"}, 4),
    ("path_between", {"entity_a": "deployer", "entity_b": "GDPR"}, 1),
]


def validate(name: str, params: dict[str, Any]) -> None:
    """Raise unless the name is a known template and the parameters are exactly
    the ones it declares.

    Both directions matter. An undeclared extra parameter is silently ignored by
    the driver, so a caller that fills `system_type` for `obligations_for_role`
    would get a query missing `$role` instead of an error naming the mistake.
    """
    if name not in TEMPLATES:
        raise ValueError(
            f"{name!r} is not a template; choose from {sorted(TEMPLATES)}"
        )
    declared = TEMPLATE_PARAMS[name]
    given = set(params)
    if given != declared:
        missing = sorted(declared - given)
        extra = sorted(given - declared)
        raise ValueError(
            f"{name} takes {sorted(declared)}; "
            f"missing={missing} unexpected={extra}"
        )


def _node_to_dict(node: Any) -> dict[str, Any]:
    """A node as its property map, plus its labels.

    `display_name` is carried through because prose must use it -- ADR-0009's
    Correction added it precisely so the graph path cites `AIA Art. 1(1)` and
    `high-risk` rather than the canonical `aia art. 1(1)` and `high risk`.
    """
    return {"labels": sorted(node.labels), **dict(node)}


def _value_to_plain(value: Any) -> Any:
    """Convert one result value, leaving anything already plain alone."""
    # Imported here rather than at module scope so this module -- and the CLI's
    # --help -- work without the neo4j package present.
    from neo4j.graph import Node, Path, Relationship

    if isinstance(value, Node):
        return _node_to_dict(value)
    if isinstance(value, Relationship):
        return {"type": value.type, **dict(value)}
    if isinstance(value, Path):
        return {
            "nodes": [_node_to_dict(n) for n in value.nodes],
            "types": [r.type for r in value.relationships],
            "hops": len(value.relationships),
        }
    if isinstance(value, list):
        return [_value_to_plain(v) for v in value]
    return value


def row_to_dict(record: Any) -> dict[str, Any]:
    """One `neo4j.Record` as a plain dict. Pure apart from the type checks."""
    return {key: _value_to_plain(value) for key, value in dict(record).items()}


def run_template(
    name: str, params: dict[str, Any], driver: Driver | None = None
) -> list[dict[str, Any]]:
    """Validate, execute, and return rows as plain dicts."""
    validate(name, params)

    owned = driver is None
    if driver is None:
        from src.ingest.graph_writer import connect

        driver = connect()
    try:
        with driver.session() as session:
            return [row_to_dict(r) for r in session.run(TEMPLATES[name], **params)]
    finally:
        if owned:
            driver.close()


def provenance_of(row: dict[str, Any]) -> list[str]:
    """Every source_chunk_id in one row, deduped, order-stable.

    Callers should not have to know whether a template named its provenance
    `applies_chunks`, `provenance`, or `chunks` -- that is a rendering detail of
    the template, while "which chunks assert this row" is the question every
    consumer actually has. Citation validation (Step 6) checks membership against
    this, so a row that returns [] here is a row that cannot be cited.
    """
    found: list[str] = []
    for key, value in row.items():
        if not isinstance(value, list):
            continue
        if key.endswith("_chunks") or key == "chunks":
            found.extend(v for v in value if isinstance(v, str))
        elif key == "provenance":
            found.extend(
                item["chunk"] for item in value
                if isinstance(item, dict) and isinstance(item.get("chunk"), str)
            )
    return list(dict.fromkeys(found))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_params(pairs: list[str]) -> dict[str, str]:
    params = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        params[key] = value
    return params


def _run_baseline(driver: Driver) -> int:
    """The regression anchors, read off the live graph. Returns an exit code."""
    with driver.session() as session:
        obligation = session.run(
            "MATCH (o:Obligation)-[:ENFORCED_BY]->(:Authority) "
            "RETURN o.canonical_name AS name ORDER BY name LIMIT 1"
        ).single()["name"]

    cases = list(BASELINE_CASES)
    cases.insert(2, ("enforcement_chain", {"obligation": obligation}, 1))

    failed = 0
    print(f"{'template':24} {'rows':>6} {'expected':>9}  {'prov/row':>9}  status")
    print("-" * 72)
    for name, params, expected in cases:
        rows = run_template(name, params, driver)
        counts = [len(provenance_of(r)) for r in rows] or [0]
        ok = len(rows) == expected and min(counts) >= 1
        failed += not ok
        print(
            f"{name:24} {len(rows):>6} {expected:>9}  "
            f"{min(counts):>4}-{max(counts):<4}  {'ok' if ok else 'MOVED'}"
        )
    print(
        "\nRow counts must match docs/metrics/graph-load.md; a count that moved is a\n"
        "defect, not an improvement. prov/row is min-max source_chunk_ids per row."
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", choices=sorted(TEMPLATES))
    parser.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE",
        help="repeatable; must match the template's declared parameters",
    )
    parser.add_argument("--json", action="store_true", help="raw rows as JSON")
    parser.add_argument(
        "--baseline", action="store_true",
        help="run the six regression anchors and report row + provenance counts",
    )
    args = parser.parse_args()

    if not args.template and not args.baseline:
        parser.error("pass --template or --baseline")

    from src.ingest.graph_writer import connect

    driver = connect()
    try:
        if args.baseline:
            return _run_baseline(driver)

        rows = run_template(args.template, _parse_params(args.param), driver)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"{args.template}: {len(rows)} rows")
            for row in rows[:20]:
                chunks = provenance_of(row)
                names = [
                    v.get("display_name") or v.get("canonical_name")
                    for v in row.values()
                    if isinstance(v, dict) and "labels" in v
                ]
                print(f"  {' | '.join(n for n in names if n)}")
                print(f"      {len(chunks)} chunk(s): {', '.join(chunks[:4])}"
                      f"{' ...' if len(chunks) > 4 else ''}")
            if len(rows) > 20:
                print(f"  ... {len(rows) - 20} more")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
