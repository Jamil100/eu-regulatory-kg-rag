"""Entity resolution: normalize -> reconcile types -> exact match -> Embed v4 similarity.

Four stages, cheapest first, each measured before the next is reached for.

The stage the original design missed is **type reconciliation**. Resolution compares
within a type, so a name carrying two types can never merge -- `AI system` is both
`DefinedTerm` and `SystemType`, `Member State` spans three. The full-corpus audit
found 66 such names. They are not a resolution failure; they are invisible to
resolution entirely, which is why they get their own pass.

Order is load-bearing. Case-folding must happen BEFORE type reconciliation: taken
alone, `Member State` resolves to Authority and `member state` to ActorRole, so
reconciling first would freeze the same concept into two types that can never merge.
Pooling their votes after the fold gives ActorRole for both.

Usage:
    python -m src.ingest.entity_resolution           # report what each stage merges
    python -m src.ingest.entity_resolution --apply   # write resolved-entities.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import unicodedata
from pathlib import Path

from src.ingest.extract import EXTRACTIONS_PATH, FOREIGN_INSTRUMENTS, ROOT, Extraction

RESOLVED_PATH = ROOT / "data" / "processed" / "resolved-entities.json"

SIMILARITY_THRESHOLD = 0.90

# DefinedTerm is the ontology's designated catch-all ("the default home for a
# definiendum"), so when a name is both DefinedTerm and something specific, the
# specific type is the better reading. This is a principled default, not a
# preference: it follows from what the type means in the extraction prompt.
CATCH_ALL_TYPE = "DefinedTerm"

# Words that make something a body with powers rather than a role someone plays.
# Used only to break ties the vote cannot -- `certification body` came out 5-5.
AUTHORITY_HINTS = re.compile(
    r"\b(authority|authorities|body|bodies|board|office|commission|agency|"
    r"panel|forum|secretariat|institution|committee|supervisor)\b"
)

# Applied when the vote is tied and no lexical hint fires. Fixed so the result is
# reproducible rather than dependent on dict ordering.
TYPE_PRIORITY = [
    "Authority", "ActorRole", "SystemType", "RiskCategory", "LawfulBasis",
    "Right", "Penalty", "Obligation", "Article", "Annex", "Regulation", "DefinedTerm",
]

# Expand short forms to the canonical name already used elsewhere in the graph.
# Seeded from the dict extract.py applies at parse time, so there is one mapping
# rather than two that can drift apart.
ABBREVIATIONS = {short.lower(): short for short in FOREIGN_INSTRUMENTS.values()}
ABBREVIATIONS.update({"aia": "AI Act", "ai act": "AI Act", "eu ai act": "AI Act"})

_BRACKET_PARTNER = {")": "(", "]": "[", "(": ")", "[": "]"}


def _trim(text: str) -> str:
    """Strip surrounding punctuation, but keep brackets that belong to the name.

    A blind `.strip(" .,;:()[]")` ate the closing paren of every sub-numbered
    article: `AIA Art. 1(1)` became `aia art. 1(1`, and 1,026 of 3,366 nodes
    carried an unbalanced paren. Every Cypher template matches on
    `canonical_name`, so the query side would have had to reproduce that mangled
    form forever. A bracket is peeled only while it has no partner, which leaves
    `aia art. 1(1)` intact and still strips a stray `(a)` wrapper.
    """
    text = text.strip(" .,;:")
    while text:
        if text[-1] in ")]" and text.count(_BRACKET_PARTNER[text[-1]]) < text.count(text[-1]):
            text = text[:-1]
        elif text[0] in "([" and text.count(_BRACKET_PARTNER[text[0]]) < text.count(text[0]):
            text = text[1:]
        else:
            break
        text = text.strip(" .,;:")
    return text


def normalize(name: str) -> str:
    """Surface-form normalisation. Case, accents, quotes, punctuation, whitespace.

    Deliberately does NOT singularise here -- see `_plural_map`, which only merges
    a plural when its singular is independently attested in the corpus.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[\"'`]", "", text)
    text = re.sub(r"[\s ]+", " ", text)
    # Hyphens are pure surface variation here: `law-enforcement authority` and
    # `law enforcement authority` are one body. That pair measured 0.966 cosine,
    # the highest of any labelled pair -- no reason to pay for an embedding call
    # to settle something a character class settles.
    text = re.sub(r"[-‐-―]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = _trim(text)
    return ABBREVIATIONS.get(text, text)


def _display_name(surface: collections.Counter) -> str:
    """The raw mention to show a human. `canonical_name` is a key, not prose.

    Normalisation lowercases and de-hyphenates, so the key reads `aia art. 1(1)`
    and `high risk` where answer text wants `AIA Art. 1(1)` and `high-risk`. The
    most frequent surface form wins; ties break by length then lexically, so the
    choice is reproducible rather than dependent on iteration order.
    """
    return max(surface.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))[0]


def _plural_map(names: set[str]) -> dict[str, str]:
    """Map plural -> singular, but only where BOTH forms actually occur.

    Blind 's'-stripping breaks `premises`, `analysis`, `business`. Requiring the
    singular to be attested makes the rule data-driven instead of grammatical, so
    it cannot invent a merge the corpus does not support.
    """
    mapping: dict[str, str] = {}
    for name in names:
        for singular in (
            name[:-1] if name.endswith("s") else None,
            name[:-2] + "y" if name.endswith("ies") else None,
            name[:-2] if name.endswith("es") else None,
        ):
            if singular and singular != name and singular in names:
                mapping[name] = singular
                break
    return mapping


def reconcile_types(
    votes: dict[str, collections.Counter],
) -> tuple[dict[str, str], list[tuple[str, dict, str, str]]]:
    """Pick one type per normalized name from the pooled per-type occurrence counts.

    Returns (name -> type, decisions) where `decisions` records every name that had
    to be reconciled, with the vote and the rule that settled it -- so a surprising
    type in the graph can be traced back to why.
    """
    resolved: dict[str, str] = {}
    decisions: list[tuple[str, dict, str, str]] = []

    for name, counter in votes.items():
        if len(counter) == 1:
            resolved[name] = next(iter(counter))
            continue

        total = sum(counter.values())
        catch_all = counter.get(CATCH_ALL_TYPE, 0)
        candidates = {t: n for t, n in counter.items() if t != CATCH_ALL_TYPE}
        rule = "catch-all dropped"

        if not candidates:  # every vote was DefinedTerm
            candidates, rule = dict(counter), "all catch-all"
        elif catch_all * 2 > total:
            # The catch-all only loses to a type the corpus actually attests. When
            # DefinedTerm holds an outright majority, a single stray specific
            # mention is noise, not a better reading: `international organisation`
            # is 18 DefinedTerm against one ActorRole and one Authority, and
            # dropping the catch-all there elects Authority on a 1-1 tiebreak.
            candidates, rule = dict(counter), "catch-all kept (majority)"

        top = max(candidates.values())
        leaders = sorted(t for t, n in candidates.items() if n == top)

        if len(leaders) == 1:
            winner = leaders[0]
            rule += " + majority" if len(candidates) > 1 else ""
        elif "Authority" in leaders and AUTHORITY_HINTS.search(name):
            winner, rule = "Authority", rule + " + tie -> authority hint"
        else:
            winner = min(leaders, key=TYPE_PRIORITY.index)
            rule += " + tie -> priority"

        resolved[name] = winner
        decisions.append((name, dict(counter.most_common()), winner, rule))

    return resolved, decisions


# Only these types are worth an embedding pass. Measured reasons:
#   - Obligation is 94% unique (1141 distinct names for 1215 mentions), so there is
#     almost nothing to merge, and obligations differ by qualifiers that matter --
#     a false merge there silently rewrites what the law requires.
#   - Article/Annex/Regulation names are already deterministic and namespaced.
# What is left is the role-like vocabulary, where 956 ActorRole mentions collapse
# to 79 names and the roadmap's `deployer`/`deployers` case actually lives.
EMBEDDABLE_TYPES = {"ActorRole", "Authority", "SystemType", "RiskCategory"}

EMBED_BATCH = 96  # Embed v4 rejects more than 96 texts per call.


def _cosine(u: list[float], v: list[float]) -> float:
    du = sum(x * x for x in u) ** 0.5
    dv = sum(x * x for x in v) ** 0.5
    return sum(x * y for x, y in zip(u, v)) / (du * dv) if du and dv else 0.0


def embedding_candidates(
    nodes: dict[tuple[str, str], dict], threshold: float = SIMILARITY_THRESHOLD
) -> list[tuple[str, str, str, float]]:
    """Within-type name pairs above `threshold`, as (type, a, b, similarity).

    Returned as CANDIDATES, not merges. On a 25-pair hand-labelled set drawn from
    the two regulations, 0.90 produced zero false merges but missed 7 of 10 true
    ones -- and the classes are not linearly separable at any threshold, because
    embeddings score an abbreviation (`dpo` vs `data protection officer`, 0.42)
    far below a pair the law keeps distinct (`supervisory authority` vs `lead
    supervisory authority`, 0.75). Legal modifiers create new entities; embeddings
    read them as near-synonyms. See docs/adr/adr-0009-entity-resolution.md.
    """
    import os

    import cohere
    from dotenv import load_dotenv

    load_dotenv(override=True)
    client = cohere.ClientV2(
        api_key=os.getenv("CO_API_KEY") or os.getenv("COHERE_API_KEY")
    )

    by_type: dict[str, list[str]] = collections.defaultdict(list)
    for (etype, name) in nodes:
        if etype in EMBEDDABLE_TYPES:
            by_type[etype].append(name)

    names = sorted({n for group in by_type.values() for n in group})
    vectors: dict[str, list[float]] = {}
    for i in range(0, len(names), EMBED_BATCH):
        batch = names[i:i + EMBED_BATCH]
        res = client.embed(
            texts=batch,
            model=os.getenv("MODEL_EMBED", "embed-v4.0"),
            input_type="clustering",
            embedding_types=["float"],
        )
        vectors.update(zip(batch, res.embeddings.float_))

    out = []
    for etype, group in by_type.items():
        group = sorted(group)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                score = _cosine(vectors[a], vectors[b])
                if score >= threshold:
                    out.append((etype, a, b, score))
    return sorted(out, key=lambda r: -r[3])


def load_entities() -> list[dict]:
    """Every entity mention across the corpus, with its source chunk."""
    out = []
    with EXTRACTIONS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = Extraction.model_validate(json.loads(line))
            for entity in row.entities:
                out.append({
                    "chunk_id": row.chunk_id,
                    "type": entity.type,
                    "canonical_name": entity.canonical_name,
                    "aliases": entity.aliases,
                })
    return out


def resolve_corpus() -> dict:
    """Run the deterministic stages over the whole corpus and report what each did."""
    mentions = load_entities()
    raw_names = {m["canonical_name"] for m in mentions}

    # Stage 1 -- surface normalisation.
    norm = {n: normalize(n) for n in raw_names}
    after_norm = set(norm.values())

    # Stage 2 -- plural folding, only where both forms are attested.
    plurals = _plural_map(after_norm)
    key = {n: plurals.get(norm[n], norm[n]) for n in raw_names}
    after_plural = set(key.values())

    # Stage 3 -- type reconciliation on the pooled votes.
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for m in mentions:
        votes[key[m["canonical_name"]]][m["type"]] += 1
    types, decisions = reconcile_types(votes)

    # Surviving nodes are (resolved type, resolved name) pairs.
    nodes: dict[tuple[str, str], dict] = {}
    surfaces: dict[tuple[str, str], collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    for m in mentions:
        k = key[m["canonical_name"]]
        node = nodes.setdefault((types[k], k), {
            "type": types[k], "canonical_name": k, "display_name": "",
            "aliases": set(), "mentions": 0, "chunk_ids": set(),
        })
        node["mentions"] += 1
        node["chunk_ids"].add(m["chunk_id"])
        surfaces[(types[k], k)][m["canonical_name"]] += 1
        if m["canonical_name"] != k:
            node["aliases"].add(m["canonical_name"])
        node["aliases"].update(m["aliases"])

    for nid, node in nodes.items():
        node["display_name"] = _display_name(surfaces[nid])

    return {
        "mentions": len(mentions),
        "raw_names": len(raw_names),
        "after_normalize": len(after_norm),
        "after_plural": len(after_plural),
        "plural_merges": plurals,
        "collisions": len(decisions),
        "decisions": decisions,
        "nodes": nodes,
        "key": key,
        "types": types,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Resolve extracted entities to graph nodes.")
    ap.add_argument("--apply", action="store_true", help="write resolved-entities.json")
    ap.add_argument("--show", type=int, default=25, help="how many decisions to print")
    ap.add_argument("--embed", action="store_true",
                    help="also list embedding merge CANDIDATES (costs API calls)")
    ap.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD)
    args = ap.parse_args()

    r = resolve_corpus()

    print("=" * 72)
    print("ENTITY RESOLUTION -- deterministic stages")
    print("=" * 72)
    print(f"entity mentions            : {r['mentions']:,}")
    print(f"distinct canonical_names   : {r['raw_names']:,}")
    print(f"after normalize()          : {r['after_normalize']:,}"
          f"  (-{r['raw_names'] - r['after_normalize']})")
    print(f"after plural folding       : {r['after_plural']:,}"
          f"  (-{r['after_normalize'] - r['after_plural']})")
    print(f"cross-type collisions      : {r['collisions']}")
    print(f"FINAL NODES                : {len(r['nodes']):,}")

    if r["plural_merges"]:
        print(f"\nPLURAL MERGES ({len(r['plural_merges'])}) -- both forms attested in corpus")
        for plural, singular in sorted(r["plural_merges"].items())[:12]:
            print(f"  {plural:<40} -> {singular}")

    print(f"\nTYPE RECONCILIATION ({r['collisions']} names carried more than one type)")
    for name, votes, winner, rule in sorted(
        r["decisions"], key=lambda d: -sum(d[1].values())
    )[: args.show]:
        print(f"  {name[:38]:<40} {str(votes)[:44]:<46} -> {winner:<12} [{rule}]")
    if r["collisions"] > args.show:
        print(f"  ... and {r['collisions'] - args.show} more")

    by_type = collections.Counter(n["type"] for n in r["nodes"].values())
    print("\nNODES BY TYPE")
    for t, c in by_type.most_common():
        mark = "  <- embedding pass applies" if t in EMBEDDABLE_TYPES else ""
        print(f"  {t:<14} {c:>6}{mark}")

    if args.embed:
        cands = embedding_candidates(r["nodes"], args.threshold)
        print(f"\nEMBEDDING CANDIDATES at cosine >= {args.threshold}"
              f"  ({len(cands)} pairs) -- REVIEW, not auto-merged")
        for etype, a, b, score in cands[:30]:
            print(f"  {score:.4f}  [{etype}] {a[:34]:<36} ~ {b[:34]}")
        if not cands:
            print("  (none -- the deterministic stages already merged what is mergeable)")

    if args.apply:
        payload = [
            {**n, "aliases": sorted(n["aliases"]), "chunk_ids": sorted(n["chunk_ids"])}
            for n in r["nodes"].values()
        ]
        RESOLVED_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {len(payload):,} nodes -> {RESOLVED_PATH}")
    print("=" * 72)


if __name__ == "__main__":
    main()
