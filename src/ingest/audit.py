"""Corpus-scale audit of extractions.jsonl. Read-only, no API calls.

The pilot's "0% validation failure" is the metric `docs/failure-notes.md` calls out
as having been produced by the broken part. This re-runs every integrity check at
corpus scale and prints the numbers that belong in the metrics doc, so the claim
"the extraction is clean" is backed by a count rather than an impression.

Re-run this after entity resolution to see what the resolution stage actually
merged.

Usage:
    python -m src.ingest.audit
    python -m src.ingest.audit --json   # machine-readable, for the metrics doc
"""

from __future__ import annotations

import argparse
import collections
import json

from src.ingest.extract import (
    CHUNK_FILES,
    EXTRACTIONS_PATH,
    FAILURES_PATH,
    Extraction,
    dangling_refs,
    endpoint_violations,
    granularity_miss,
    load_chunks,
    orphan_entities,
)


def load_extractions() -> list[Extraction]:
    with EXTRACTIONS_PATH.open(encoding="utf-8") as fh:
        return [Extraction.model_validate(json.loads(line)) for line in fh if line.strip()]


def bar(n: int, total: int, width: int = 28) -> str:
    filled = round(width * n / total) if total else 0
    return "#" * filled + "." * (width - filled)


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit the extracted corpus.")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    corpus = load_chunks()
    by_id = {c["chunk_id"]: c for c in corpus}
    rows = load_extractions()
    n_failures = sum(
        1 for line in FAILURES_PATH.open(encoding="utf-8") if line.strip()
    ) if FAILURES_PATH.exists() else 0

    covered = {r.chunk_id for r in rows}
    missing = [c["chunk_id"] for c in corpus if c["chunk_id"] not in covered]

    entities = collections.Counter(e.type for r in rows for e in r.entities)
    relations = collections.Counter(x.type for r in rows for x in r.relationships)
    confidence = collections.Counter(x.confidence for r in rows for x in r.relationships)

    # Distinct canonical names per type -- the direct input to entity resolution.
    names_by_type: dict[str, set[str]] = collections.defaultdict(set)
    types_by_name: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        for e in r.entities:
            names_by_type[e.type].add(e.canonical_name)
            types_by_name[e.canonical_name].add(e.type)

    # One name carrying two types can never be merged by resolution, which
    # compares within a type. These fragment the graph permanently.
    collisions = {n: sorted(t) for n, t in types_by_name.items() if len(t) > 1}

    dangling = [(r.chunk_id, x) for r in rows for x in dangling_refs(r)]
    orphans = [(r.chunk_id, x) for r in rows for x in orphan_entities(r)]
    violations = [(r.chunk_id, x) for r in rows for x in endpoint_violations(r)]
    bare = [
        (r.chunk_id, m)
        for r in rows
        if r.chunk_id in by_id and (m := granularity_miss(r, by_id[r.chunk_id]))
    ]

    total_edges = sum(relations.values())
    total_entities = sum(entities.values())

    if args.json:
        print(json.dumps({
            "chunks_in_corpus": len(corpus),
            "chunks_extracted": len(rows),
            "chunks_missing": missing,
            "failures": n_failures,
            "entities": total_entities,
            "relationships": total_edges,
            "entity_types": dict(entities),
            "relation_types": dict(relations),
            "distinct_names": {t: len(v) for t, v in sorted(names_by_type.items())},
            "type_collisions": collisions,
            "dangling": len(dangling),
            "orphans": len(orphans),
            "endpoint_violations": len(violations),
            "bare_articles": len(bare),
            "confidence": {str(k): v for k, v in sorted(confidence.items())},
        }, indent=2))
        return

    print("=" * 72)
    print("EXTRACTION AUDIT")
    print("=" * 72)
    pct = len(rows) / len(corpus) if corpus else 0
    print(f"corpus chunks           : {len(corpus)}")
    print(f"extracted               : {len(rows)}  ({pct:.1%})")
    print(f"recorded failures       : {n_failures}")
    print(f"unextracted             : {len(missing)}")
    if missing:
        print(f"    {missing[:12]}{' ...' if len(missing) > 12 else ''}")
    print(f"entities                : {total_entities:,}")
    print(f"relationships           : {total_edges:,}")

    print("\nENTITY TYPES")
    for t, c in entities.most_common():
        print(f"  {t:<14} {c:>6}  {bar(c, entities.most_common(1)[0][1])}"
              f"  {len(names_by_type[t]):>5} distinct")

    print("\nRELATIONSHIP TYPES")
    top = relations.most_common(1)[0][1] if relations else 1
    for t, c in relations.most_common():
        print(f"  {t:<16} {c:>6}  {bar(c, top)}")
    for t in ("INTERACTS_WITH", "PENALIZED_UNDER", "SETS_PENALTY", "GRANTS", "EXEMPT_FROM"):
        if t not in relations:
            print(f"  {t:<16} {0:>6}  <-- UNUSED: ontology hole or correct absence? decide in writing")

    print("\nINTEGRITY")
    print(f"  dangling head/tail    : {len(dangling)}")
    print(f"  orphan entities       : {len(orphans)}  ({len(orphans)/max(total_entities,1):.1%} of entities)")
    print(f"  endpoint violations   : {len(violations)}  ({len(violations)/max(total_edges,1):.1%} of edges)")
    print(f"  bare self-articles    : {len(bare)}")

    if violations:
        print("\n  most common violations:")
        kinds = collections.Counter(v.split(":")[0] for _, v in violations)
        for k, c in kinds.most_common(8):
            print(f"    {c:>4}  {k}")

    print(f"\nTYPE COLLISIONS  ({len(collisions)} names carry more than one type)")
    print("  Entity resolution compares within a type, so these can never merge.")
    for name, types in sorted(collisions.items())[:20]:
        print(f"    {name[:44]:<46} {types}")
    if len(collisions) > 20:
        print(f"    ... and {len(collisions) - 20} more")

    print("\nCONFIDENCE")
    for value, c in sorted(confidence.items()):
        print(f"  {value:<6} {c:>6}  {bar(c, max(confidence.values()))}")
    print(f"  distinct values: {len(confidence)} "
          f"-- coarse ordinal, do not threshold finely or average as a probability")
    print("=" * 72)


if __name__ == "__main__":
    main()
