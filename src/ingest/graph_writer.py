"""Idempotent Neo4j writes.

MERGE on (type, canonical_name), never CREATE. Every relationship carries
source_chunk_id and confidence. Re-running ingestion is a no-op.

`build_graph()` is deliberately pure -- it derives the whole graph from
`extractions.jsonl` + `resolve_corpus()` with no database in sight, so every
count below is assertable in a test that runs without Neo4j.

Edges live in extractions.jsonl under RAW head/tail strings, and
resolved-entities.json holds nodes only. Joining the two by raw string matches
just ~48% of endpoints; going through `resolve_corpus()["key"]` matches 98.4%
(6,660 of 6,767). That is why this module imports the resolver rather than
reading the resolved JSON file.

THREE EDGE-CASE POLICIES. `extract.py` already states the principle -- "a
dropped edge looks identical to one that was never extracted, and that is how
the ontology hole hid last time" -- so:

  * Endpoint violations (241) are LOADED and TAGGED `endpoint_violation: true`.
    Countable and filterable in the graph rather than silently absent from it.
  * Dangling edges (107) are SKIPPED and listed in the report. Their endpoint
    was never declared as an entity, so it has no type and therefore no label;
    an untyped node would pollute the *unlabeled* `path_between` match with
    shortest paths through nodes that do not really exist.
  * Isolated nodes (112) are LOADED. They are real extracted entities, and
    dropping them would erase the orphan-entity signal from the graph.

Usage:
    python -m src.ingest.graph_writer                    # dry run, needs no database
    python -m src.ingest.graph_writer --apply
    python -m src.ingest.graph_writer --apply --verify    # second load must not change counts
    python -m src.ingest.graph_writer --reset --apply
    python -m src.ingest.graph_writer --report data/processed/graph-load-report.json
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from src.config import settings
from src.ingest.audit import load_extractions
from src.ingest.entity_resolution import resolve_corpus
from src.ingest.extract import ALLOWED_ENDPOINTS, ROOT, EntityType, RelationType

if TYPE_CHECKING:  # keep `neo4j` off the import path of the pure functions
    from neo4j import Driver

REPORT_PATH = ROOT / "data" / "processed" / "graph-load-report.json"

# Neo4j 5 cannot parameterize a label or a relationship type (dynamic
# `MERGE (n:$(row.type))` is Cypher 25+), so both are interpolated into the query
# text. The ontology is a closed Literal, and every value is checked against it
# before it reaches a format string -- nothing from the data can become Cypher.
ENTITY_TYPES: frozenset[str] = frozenset(get_args(EntityType))
RELATION_TYPES: frozenset[str] = frozenset(get_args(RelationType))

# One shared secondary label. It gives the two unlabeled templates
# (`definition_of`, `path_between`) an index to hit, and lets edge writes match
# an endpoint without knowing its type -- names are globally unique after type
# reconciliation, so a single lookup is unambiguous.
SHARED_LABEL = "Entity"

BATCH = 1_000


# --------------------------------------------------------------------------
# Pure derivation -- no database
# --------------------------------------------------------------------------

def build_graph() -> dict[str, Any]:
    """Derive nodes and edges from the corpus. No I/O beyond reading the files."""
    resolution = resolve_corpus()
    key, types = resolution["key"], resolution["types"]

    nodes = [
        {
            "type": n["type"],
            "canonical_name": n["canonical_name"],
            "display_name": n["display_name"],
            "aliases": sorted(n["aliases"]),
            "mentions": n["mentions"],
            "chunk_ids": sorted(n["chunk_ids"]),
        }
        for n in resolution["nodes"].values()
    ]

    # MERGE keys on (head, TYPE, tail, source_chunk_id), so build the same key
    # here: what collapses in this dict is exactly what collapses in the graph.
    edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []

    for row in load_extractions():
        for rel in row.relationships:
            head, tail = key.get(rel.head), key.get(rel.tail)
            if head is None or tail is None:
                skipped.append({
                    "chunk_id": rel.source_chunk_id,
                    "type": rel.type,
                    "head": rel.head,
                    "tail": rel.tail,
                    # The endpoint(s) that were never declared as an entity --
                    # this is the list worth reading, not the edge count.
                    "missing": ([rel.head] if head is None else [])
                               + ([rel.tail] if tail is None else []),
                })
                continue
            allowed = ALLOWED_ENDPOINTS.get(rel.type)
            violation = bool(allowed) and (
                types[head] not in allowed[0] or types[tail] not in allowed[1]
            )
            edges[(rel.type, head, tail, rel.source_chunk_id)] = {
                "type": rel.type,
                "head": head,
                "tail": tail,
                "source_chunk_id": rel.source_chunk_id,
                "confidence": rel.confidence,
                "endpoint_violation": violation,
            }

    edge_rows = list(edges.values())
    return {
        "nodes": nodes,
        "edges": edge_rows,
        "skipped": skipped,
        "stats": _stats(nodes, edge_rows, skipped),
    }


def _stats(
    nodes: list[dict], edges: list[dict], skipped: list[dict]
) -> dict[str, Any]:
    adjacency: dict[str, set[str]] = collections.defaultdict(set)
    for e in edges:
        adjacency[e["head"]].add(e["tail"])
        adjacency[e["tail"]].add(e["head"])

    components = _component_sizes(adjacency)
    violations = [e for e in edges if e["endpoint_violation"]]
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "skipped_dangling": len(skipped),
        "skipped_dangling_names": sorted({n for s in skipped for n in s["missing"]}),
        "endpoint_violations": len(violations),
        "violations_by_type": dict(
            collections.Counter(e["type"] for e in violations).most_common()
        ),
        "isolated_nodes": sum(1 for n in nodes if n["canonical_name"] not in adjacency),
        "components": len(components),
        "largest_components": components[:5],
        "nodes_by_type": dict(
            collections.Counter(n["type"] for n in nodes).most_common()
        ),
        "edges_by_type": dict(
            collections.Counter(e["type"] for e in edges).most_common()
        ),
    }


def _component_sizes(adjacency: dict[str, set[str]]) -> list[int]:
    seen: set[str] = set()
    sizes: list[int] = []
    for start in adjacency:
        if start in seen:
            continue
        stack, component = [start], set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency[node] - component)
        seen |= component
        sizes.append(len(component))
    return sorted(sizes, reverse=True)


# --------------------------------------------------------------------------
# Neo4j
# --------------------------------------------------------------------------

def connect() -> Driver:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    driver.verify_connectivity()
    return driver


def apply_schema(driver: Driver) -> None:
    """Per-label uniqueness on canonical_name, plus the shared lookup index.

    Neo4j 5 Community has property uniqueness but not `IS NODE KEY` (Enterprise).
    Per-label uniqueness is what MERGE needs anyway, and type reconciliation
    guarantees exactly one type per resolved name, so it cannot reject a
    legitimate node. All DDL is IF NOT EXISTS, so re-running is a no-op.
    """
    with driver.session() as session:
        for label in sorted(ENTITY_TYPES):
            session.run(
                f"CREATE CONSTRAINT entity_{label.lower()}_name IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.canonical_name IS UNIQUE"
            )
        session.run(
            f"CREATE INDEX entity_name IF NOT EXISTS "
            f"FOR (n:{SHARED_LABEL}) ON (n.canonical_name)"
        )


def write_nodes(driver: Driver, nodes: list[dict]) -> int:
    by_type: dict[str, list[dict]] = collections.defaultdict(list)
    for node in nodes:
        by_type[node["type"]].append(node)

    written = 0
    with driver.session() as session:
        for label, rows in sorted(by_type.items()):
            if label not in ENTITY_TYPES:
                raise ValueError(f"{label!r} is not in the ontology")
            query = f"""
                UNWIND $rows AS row
                MERGE (n:{label} {{canonical_name: row.canonical_name}})
                SET n:{SHARED_LABEL},
                    n.type = row.type,
                    n.display_name = row.display_name,
                    n.aliases = row.aliases,
                    n.mentions = row.mentions,
                    n.chunk_ids = row.chunk_ids
            """
            for i in range(0, len(rows), BATCH):
                batch = rows[i:i + BATCH]
                session.execute_write(
                    lambda tx, q=query, b=batch: tx.run(q, rows=b).consume()
                )
                written += len(batch)
    return written


def write_edges(driver: Driver, edges: list[dict]) -> int:
    by_type: dict[str, list[dict]] = collections.defaultdict(list)
    for edge in edges:
        by_type[edge["type"]].append(edge)

    written = 0
    with driver.session() as session:
        for rel_type, rows in sorted(by_type.items()):
            if rel_type not in RELATION_TYPES:
                raise ValueError(f"{rel_type!r} is not in the ontology")
            query = f"""
                UNWIND $rows AS row
                MATCH (h:{SHARED_LABEL} {{canonical_name: row.head}})
                MATCH (t:{SHARED_LABEL} {{canonical_name: row.tail}})
                MERGE (h)-[r:{rel_type} {{source_chunk_id: row.source_chunk_id}}]->(t)
                SET r.confidence = row.confidence,
                    r.endpoint_violation = row.endpoint_violation
            """
            for i in range(0, len(rows), BATCH):
                batch = rows[i:i + BATCH]
                session.execute_write(
                    lambda tx, q=query, b=batch: tx.run(q, rows=b).consume()
                )
                written += len(batch)
    return written


def graph_counts(driver: Driver) -> dict[str, Any]:
    """Counts read back FROM the database -- the input to the idempotency check."""
    with driver.session() as session:
        nodes_by_type = {
            r["type"]: r["n"] for r in session.run(
                f"MATCH (n:{SHARED_LABEL}) RETURN n.type AS type, count(*) AS n"
            )
        }
        edges_by_type = {
            r["type"]: r["n"] for r in session.run(
                "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n"
            )
        }
        violations = session.run(
            "MATCH ()-[r]->() WHERE r.endpoint_violation RETURN count(r) AS n"
        ).single()["n"]
        isolated = session.run(
            f"MATCH (n:{SHARED_LABEL}) WHERE NOT (n)--() RETURN count(n) AS n"
        ).single()["n"]
    return {
        "nodes": sum(nodes_by_type.values()),
        "edges": sum(edges_by_type.values()),
        "endpoint_violations": violations,
        "isolated_nodes": isolated,
        "nodes_by_type": dict(sorted(nodes_by_type.items(), key=lambda kv: -kv[1])),
        "edges_by_type": dict(sorted(edges_by_type.items(), key=lambda kv: -kv[1])),
    }


def reset(driver: Driver) -> None:
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_report(graph: dict, live: dict | None = None) -> None:
    stats = graph["stats"]
    print("=" * 72)
    print("GRAPH LOAD")
    print("=" * 72)
    print(f"nodes                   : {stats['nodes']:,}")
    print(f"edges                   : {stats['edges']:,}")
    print(f"skipped (dangling)      : {stats['skipped_dangling']}"
          f"  ({len(stats['skipped_dangling_names'])} distinct names) -- policy: skip, report")
    print(f"endpoint violations     : {stats['endpoint_violations']}"
          f"  -- policy: load, tag endpoint_violation=true")
    print(f"isolated nodes          : {stats['isolated_nodes']}  -- policy: load")
    print(f"components              : {stats['components']}"
          f"  largest {stats['largest_components']}")

    print("\nNODES BY TYPE")
    for label, count in stats["nodes_by_type"].items():
        print(f"  {label:<14} {count:>6}")

    print("\nEDGES BY TYPE")
    for rel_type, count in stats["edges_by_type"].items():
        bad = stats["violations_by_type"].get(rel_type, 0)
        flag = f"   ({bad} endpoint-violating)" if bad else ""
        print(f"  {rel_type:<16} {count:>6}{flag}")

    if live is not None:
        print("\nIN DATABASE (read back)")
        print(f"  nodes                 : {live['nodes']:,}")
        print(f"  edges                 : {live['edges']:,}")
        print(f"  endpoint violations   : {live['endpoint_violations']}")
        print(f"  isolated nodes        : {live['isolated_nodes']}")
        ok = (live["nodes"] == stats["nodes"]
              and live["edges"] == stats["edges"]
              and live["endpoint_violations"] == stats["endpoint_violations"])
        print(f"  matches derivation    : {'YES' if ok else 'NO -- investigate'}")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description="Load the resolved graph into Neo4j.")
    ap.add_argument("--apply", action="store_true", help="write to Neo4j")
    ap.add_argument("--reset", action="store_true", help="DETACH DELETE everything first")
    ap.add_argument("--verify", action="store_true", help="read counts back from the database")
    ap.add_argument("--report", type=Path, nargs="?", const=REPORT_PATH,
                    help="write the load report as JSON")
    args = ap.parse_args()

    graph = build_graph()

    live = None
    if args.apply or args.reset or args.verify:
        driver = connect()
        try:
            if args.reset:
                reset(driver)
                print("reset: all nodes and relationships deleted")
            if args.apply:
                apply_schema(driver)
                n = write_nodes(driver, graph["nodes"])
                e = write_edges(driver, graph["edges"])
                print(f"merged {n:,} nodes and {e:,} relationships")
            if args.verify or args.apply:
                live = graph_counts(driver)
        finally:
            driver.close()

    _print_report(graph, live)

    if args.report:
        payload = {**graph["stats"], "dangling_edges": graph["skipped"]}
        if live is not None:
            payload["in_database"] = live
        args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote report -> {args.report}")


if __name__ == "__main__":
    main()
