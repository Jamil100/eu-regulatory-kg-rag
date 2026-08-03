"""Question -> node linking.

Turns a user's question into the canonical node keys the Cypher templates take as
parameters. `schema.sql:50-53` guarantees the shape: a resolved node name "is the
same string as the Neo4j MERGE key, so a value here is usable as a Cypher
parameter with no translation." That is the contract this module fulfils.

**Deterministic, not embedded.** The Phase 0 stub promised "alias lookup + Embed
v4 similarity". The similarity half is deliberately not built. ADR-0009 measured
it and it was the stage that did not work: `dpo` vs `data protection officer`
scored 0.42, *below* pairs that are legitimately distinct (`supervisory
authority` vs `lead supervisory authority`, 0.75), so no threshold separates the
classes. A lexical sweep costs nothing, calls nothing, and is reproducible.
`docs/metrics/query-path.md` records what an embedding stage would have to beat.

**Reused from Phase 1, not reimplemented.** `normalize()` for surface folding
(bracket-aware -- see the ADR-0009 Correction, where a blind `.strip("()")` left
1,026 of 3,366 node keys reading `aia art. 1(1`), and `resolve_corpus()` for the
three corpus-dependent parts a question cannot supply on its own: the plural map
(which merges only where both forms are attested), the type of each node, and the
alias lists. `resolve_corpus()["key"]` is keyed on *raw corpus* names, so it is
not the lookup path for a question span; the index below is built on the
normalised side instead.

Usage:
    python -m src.query.entity_linker --question "..."
    python -m src.query.entity_linker --eval            # no containers needed
    python -m src.query.entity_linker --eval --from-db  # cross-check pgvector
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# `_plural_map` is private to the resolver, and imported rather than reimplemented
# on purpose: the rule it encodes ("merge a plural only where the singular is
# independently attested") is the thing worth reusing, and a second copy could
# drift from the one the corpus was built with. `tests/test_graph_writer.py:216`
# already reaches for it the same way.
from src.ingest.entity_resolution import _plural_map, normalize, resolve_corpus

QUESTIONS = Path(__file__).resolve().parents[2] / "eval" / "eval-questions.jsonl"

# `_trim` (entity_resolution.py:65) peels " .,;:" -- the punctuation that ends a
# clause in a legal text. It has never needed to handle a question mark, because
# no node name ends in one. A question does, and the miss is not silent-in-effect:
# `"...require a notified body?"` failed to match `notified body`, fell back to
# the bare token `notified`, and linked the obligation `notify use of real time
# remote biometric identification system`. Stripped here, at the linker boundary,
# rather than in `_trim` -- widening `_trim` would move corpus node keys.
_QUESTION_PUNCT = "?!"


@dataclass(frozen=True)
class LinkedEntity:
    """One node a question reached, and the evidence for it."""

    canonical_name: str  # the Cypher parameter value, verbatim
    type: str  # Step 5 needs it: a typed template returns 0 rows on a wrong label
    display_name: str  # ADR-0009 Correction: prose only, never a key
    span: str  # the question text that matched
    via: str  # "canonical" | "alias"
    ambiguous: bool  # the surface named more than one node


@dataclass(frozen=True)
class LinkIndex:
    """Normalised surface -> node, plus what resolution needs to fold a span."""

    canonical: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    plural_merges: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=1)
def build_index() -> LinkIndex:
    """Build the surface index once.

    Cached because `resolve_corpus()` re-reads and re-parses 1.8 MB of
    `extractions.jsonl` on every call with no caching of its own. On an `/ask`
    request path that would be per-question.

    Aliases are stored raw (`"AIA Art. 1(1)"`, `"human-centric artificial
    intelligence"`), so both sides are normalised before they meet.

    **The plural fold is rebuilt over a wider pool than the resolver's.**
    `resolve_corpus()["plural_merges"]` folds only surfaces that appeared as an
    extracted `canonical_name`, which is 18 entries; aliases never went through
    it. Questions use plurals freely -- `ag-001` asks about "deployers" -- so the
    same `_plural_map` rule is re-run over canonical names *and* alias surfaces,
    which is 124 merges, 102 of them a plural surface resolving to a real node.
    Measured before adopting: **zero** of the extra merges fold a surface that is
    itself a node, and the classic breakages the rule exists to avoid stay
    unmerged (`premises`, `analysis`, `business`, `bias`, `practices`) because
    their singulars are not attested.
    """
    resolution = resolve_corpus()

    canonical: dict[str, str] = {}
    aliases: dict[str, set[str]] = {}
    display_names: dict[str, str] = {}

    for node in resolution["nodes"].values():
        name = node["canonical_name"]
        canonical[normalize(name)] = name
        display_names[name] = node["display_name"]
        for alias in node["aliases"]:
            surface = normalize(alias)
            if surface:
                aliases.setdefault(surface, set()).add(name)

    return LinkIndex(
        canonical=canonical,
        # Sorted so a surface naming several nodes yields them in a stable order:
        # the linker feeds template parameters, and an order that moved between
        # runs would make a graph answer irreproducible.
        aliases={surface: tuple(sorted(names)) for surface, names in aliases.items()},
        plural_merges=_plural_map(set(canonical) | set(aliases)),
        types=resolution["types"],
        display_names=display_names,
    )


def _surfaces(span: str) -> list[str]:
    """A question span reduced to the forms node keys are stored in.

    Two forms, not one, and the plain one is tried first so this can only add
    matches at a given position. `normalize()` deletes apostrophes, which turns a
    possessive into a plural that no fold covers: `"the GDPR's highest fine tier"`
    normalised to `gdprs` and `ag-003` linked no instrument at all. Stripping the
    possessive *before* normalising recovers `GDPR`. It is not done
    unconditionally because the corpus itself contains collapsed possessives --
    `"controller's representative"` is stored as `controllers representative`,
    and 122 surfaces would land on a different node if a trailing `s` came off.

    **Single-token spans only.** The sweep is leftmost-longest, so a two-token
    span that matches only via the fallback can shadow a plain match of the same
    length starting one token later: `"the controller's"` strips to the alias
    `the controller` and swallowed `"controller's representative"`. A possessive
    marks the end of its own noun phrase, so restricting the fallback to the
    token that carries it costs nothing the eval set exercises -- a multi-word
    possessive (`"the European Commission's report"`) is the known gap, recorded
    in `docs/metrics/query-path.md` §Open.
    """
    span = span.strip(_QUESTION_PUNCT)
    forms = [normalize(span)]
    stripped = re.sub(r"['’]s$|['’]$", "", span)
    if stripped != span and " " not in span:
        forms.append(normalize(stripped))
    return [form for form in dict.fromkeys(forms) if form]


def _resolve(surface: str, index: LinkIndex) -> tuple[tuple[str, ...], str] | None:
    """Node names for one normalised surface, and how they were found.

    Canonical before alias, because 59 surfaces are simultaneously one node's
    canonical name and another's alias (`placed on the market`, `union law`,
    `conformity assessment body`). Reading those as aliases would drop the node
    that owns the name.

    The plural fold is applied as a fallback rather than first, so a plural that
    is itself an attested node keeps its own identity.
    """
    folded = index.plural_merges.get(surface, surface)
    for candidate in (surface, folded):
        if candidate in index.canonical:
            return (index.canonical[candidate],), "canonical"
    for candidate in (surface, folded):
        if candidate in index.aliases:
            return index.aliases[candidate], "alias"
    return None


def link_detailed(question: str, index: LinkIndex | None = None) -> list[LinkedEntity]:
    """Every node the question names, longest match first, with its evidence.

    Longest-match-and-advance: `high-risk AI system` links that node and does not
    also emit `ai system`, which would put two competing parameters into the same
    template call.

    No n-gram cap. The longest index key is 141 tokens (obligation names are
    whole sentences), so any cap short of that is an invented constant; a
    question bounds the sweep on its own, at ~40 tokens.

    One entry per node, first mention winning: `ag-002` names the deployer twice
    (`"a deployer's obligations"`, `"when the deployer is"`), and a consumer that
    parameterises a template per entry would run the same Cypher twice for it.
    """
    index = index or build_index()
    tokens = question.split()

    linked: dict[str, LinkedEntity] = {}
    i = 0
    while i < len(tokens):
        for length in range(len(tokens) - i, 0, -1):
            span = " ".join(tokens[i : i + length])
            found = next(
                (hit for surface in _surfaces(span)
                 if (hit := _resolve(surface, index)) is not None),
                None,
            )
            if found is None:
                continue
            names, via = found
            for name in names:
                linked.setdefault(name, LinkedEntity(
                    canonical_name=name,
                    type=index.types.get(name, ""),
                    display_name=index.display_names.get(name, name),
                    span=span,
                    via=via,
                    ambiguous=len(names) > 1,
                ))
            i += length
            break
        else:
            i += 1
    return list(linked.values())


def link(question: str) -> list[str]:
    """Return resolved graph node IDs referenced by the question.

    Order-stable, and usable as Cypher parameters with no translation.
    """
    return [entity.canonical_name for entity in link_detailed(question)]


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def load_labeled_queries() -> list[dict]:
    """The scoreable rows. Reuses the recall harness's definition, not a copy."""
    from src.index.recall_harness import load_labeled_queries as loader

    return loader()


def gold_entities(from_db: bool = False) -> dict[str, list[str]]:
    """chunk_id -> the resolved node names that chunk asserts.

    Defaults to `embedder.entity_ids_by_chunk()`, which inverts the same resolver
    the graph loader used, so the measurement runs with no containers. `--from-db`
    reads pgvector's `entity_ids` column instead: the two are supposed to be the
    same strings (`schema.sql:50-53`), and checking that is a real cross-check
    rather than a restatement.
    """
    from src.index.embedder import entity_ids_by_chunk

    if not from_db:
        return entity_ids_by_chunk()

    from src.index.pgvector_schema import connect

    conn = connect()
    try:
        rows = conn.execute("SELECT chunk_id, entity_ids FROM chunks").fetchall()
    finally:
        conn.close()
    return {chunk_id: sorted(ids) for chunk_id, ids in rows if ids}


def _score(rows: list[dict], gold_by_chunk: dict[str, list[str]], drop_ambiguous: bool) -> dict:
    """Link rate, hit rate, precision and superset-recall, per stratum."""
    per_query = []
    for row in rows:
        entities = link_detailed(row["question"])
        if drop_ambiguous:
            entities = [e for e in entities if not e.ambiguous]
        linked = list(dict.fromkeys(e.canonical_name for e in entities))

        gold: set[str] = set()
        for chunk_id in row["source_chunk_ids"]:
            gold.update(gold_by_chunk.get(chunk_id, []))

        hit = sorted(set(linked) & gold)
        per_query.append({
            "id": row["id"],
            "stratum": row.get("stratum"),
            "linked": linked,
            "spans": [e.span for e in entities],
            "gold": len(gold),
            "in_gold": len(hit),
            "false_positives": sorted(set(linked) - gold),
        })

    strata: dict[str, dict] = {}
    for q in per_query:
        acc = strata.setdefault(q["stratum"], {
            "rows": 0, "linked_rows": 0, "hit_rows": 0,
            "linked": 0, "in_gold": 0, "gold": 0,
        })
        acc["rows"] += 1
        acc["linked_rows"] += bool(q["linked"])
        acc["hit_rows"] += bool(q["in_gold"])
        acc["linked"] += len(q["linked"])
        acc["in_gold"] += q["in_gold"]
        acc["gold"] += q["gold"]

    def rates(acc: dict) -> dict:
        return {
            **acc,
            "link_rate": acc["linked_rows"] / acc["rows"] if acc["rows"] else 0.0,
            "hit_rate": acc["hit_rows"] / acc["rows"] if acc["rows"] else 0.0,
            "precision": acc["in_gold"] / acc["linked"] if acc["linked"] else 0.0,
            "recall_superset": acc["in_gold"] / acc["gold"] if acc["gold"] else 0.0,
        }

    overall = {k: sum(a[k] for a in strata.values())
               for k in ("rows", "linked_rows", "hit_rows", "linked", "in_gold", "gold")}
    return {
        "drop_ambiguous": drop_ambiguous,
        "overall": rates(overall),
        "strata": {name: rates(acc) for name, acc in strata.items()},
        "per_query": per_query,
    }


def evaluate(from_db: bool = False) -> dict:
    """Both arms -- every link, and unambiguous links only -- plus the link rate
    over all 23 rows, including the two that carry no gold chunks.

    Those two are out-of-scope and unanswerable, so they are unscoreable against
    gold, but whether they link at all is still worth knowing: a question that
    links to nothing cannot take the graph route no matter what the router says.
    """
    all_rows = [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scoreable = load_labeled_queries()
    gold_by_chunk = gold_entities(from_db=from_db)

    return {
        "source": "pgvector" if from_db else "resolver",
        "rows_total": len(all_rows),
        "rows_scoreable": len(scoreable),
        "rows_linking": sum(bool(link(r["question"])) for r in all_rows),
        "gold_mean": (
            sum(len({e for c in r["source_chunk_ids"] for e in gold_by_chunk.get(c, [])})
                for r in scoreable) / len(scoreable)
            if scoreable else 0.0
        ),
        "all": _score(scoreable, gold_by_chunk, drop_ambiguous=False),
        "unambiguous": _score(scoreable, gold_by_chunk, drop_ambiguous=True),
    }


def compare_gold_sources() -> dict:
    """Does pgvector's `entity_ids` hold the same strings the resolver produces?"""
    resolver, database = gold_entities(False), gold_entities(True)
    chunks = set(resolver) | set(database)
    disagreeing = sorted(c for c in chunks if set(resolver.get(c, [])) != set(database.get(c, [])))
    return {"chunks": len(chunks), "disagreeing": disagreeing}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_arm(arm: dict, title: str) -> None:
    print(f"\n{title}")
    print(f"{'stratum':<17} {'link rate':>10} {'hit rate':>9} {'precision':>10} "
          f"{'recall*':>9}")
    for name, s in sorted(arm["strata"].items(), key=lambda kv: -kv[1]["precision"]):
        print(
            f"{name:<17} {s['linked_rows']:>3}/{s['rows']:<2} {s['link_rate']:>4.0%} "
            f"{s['hit_rows']:>3}/{s['rows']:<2} {s['hit_rate']:>3.0%} "
            f"{s['in_gold']:>3}/{s['linked']:<3} {s['precision']:>4.0%} "
            f"{s['recall_superset']:>8.0%}"
        )
    o = arm["overall"]
    print(
        f"{'ALL':<17} {o['linked_rows']:>3}/{o['rows']:<2} {o['link_rate']:>4.0%} "
        f"{o['hit_rows']:>3}/{o['rows']:<2} {o['hit_rate']:>3.0%} "
        f"{o['in_gold']:>3}/{o['linked']:<3} {o['precision']:>4.0%} "
        f"{o['recall_superset']:>8.0%}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Link a question to graph node keys.")
    ap.add_argument("--question", help="link one question and print what it reached")
    ap.add_argument("--eval", action="store_true", help="measure against the eval set")
    ap.add_argument("--from-db", action="store_true",
                    help="take gold entity_ids from pgvector instead of the resolver")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.question:
        entities = link_detailed(args.question)
        if args.json:
            print(json.dumps([e.__dict__ for e in entities], indent=2))
            return 0
        if not entities:
            print("no nodes linked")
            return 0
        for e in entities:
            flag = " AMBIGUOUS" if e.ambiguous else ""
            print(f"  {e.canonical_name!r:<50} {e.type:<12} via {e.via:<9} "
                  f"<- {e.span!r}{flag}")
        return 0

    if not args.eval:
        ap.print_help()
        return 2

    if args.from_db:
        agreement = compare_gold_sources()
        print(f"gold source cross-check: {agreement['chunks']} chunks, "
              f"{len(agreement['disagreeing'])} disagreeing between resolver and pgvector")
        for chunk_id in agreement["disagreeing"][:10]:
            print(f"  {chunk_id}")

    report = evaluate(from_db=args.from_db)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(
        f"{report['rows_linking']}/{report['rows_total']} questions link to >=1 node; "
        f"{report['rows_scoreable']} rows are scoreable against gold "
        f"(source: {report['source']})"
    )
    _print_arm(report["all"], "every link:")
    _print_arm(report["unambiguous"], "unambiguous links only:")
    print(
        f"\n* recall's denominator is every entity the gold chunks assert "
        f"({report['gold_mean']:.1f} nodes per row on average), not the entities the\n"
        f"  question names. A linker emitting 3-8 nodes cannot reach 100% of it, so this\n"
        f"  column is a lower bound by construction. Read precision instead."
    )

    offenders: dict[str, int] = {}
    for q in report["all"]["per_query"]:
        for name in q["false_positives"]:
            offenders[name] = offenders.get(name, 0) + 1
    if offenders:
        types = build_index().types
        print("\nmost frequent links absent from any gold chunk:")
        for name, count in sorted(offenders.items(), key=lambda kv: (-kv[1], kv[0]))[:12]:
            print(f"  {count:>2}x {name:<40} {types.get(name, '')}")

        # The instrument nodes are named by almost every question ("under the AI
        # Act...") and asserted as an entity by almost no chunk, so the gold set
        # cannot credit them however right they are. Separating them says how much
        # of the precision gap is the linker and how much is the denominator.
        o = report["all"]["overall"]
        regulations = sum(c for n, c in offenders.items() if types.get(n) == "Regulation")
        print(
            f"\n{regulations} of {o['linked'] - o['in_gold']} misses are Regulation nodes. "
            f"Precision excluding them: "
            f"{o['in_gold'] / (o['linked'] - regulations):.0%} "
            f"(vs {o['precision']:.0%} reported)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
