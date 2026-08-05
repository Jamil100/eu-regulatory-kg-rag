"""Convert graph rows into readable statements before they reach the prompt.

e.g. (deployer)-[APPLIES_TO]-(FRIA obligation)-[IMPOSED_BY]-(AIA Art. 27)
  -> "AIA Art. 27 imposes: conduct a fundamental rights impact assessment."
Each statement keeps the source_chunk_ids of the edge it was built from.

ONE STATEMENT PER RELATIONSHIP LEG, NOT ONE PER ROW.

This is what caps the hot fact, and it caps it structurally rather than with a
magic number. `obligations_for_system('high risk ai system')` returns 169 rows
and every one of them carries the same 124-126 `classified_chunks`, because 124
different chunks assert that a high-risk AI system is classified as high-risk
(`cypher_templates.py:60-63`; the live `--baseline` prints `124-126` as prov/row).
Rendering per row would emit that one fact 169 times and drag 124 citations
behind each copy -- the 24,428-row multiplication `DISTINCT` was added to kill,
arriving through the prose instead of through the row count, which is precisely
what `docs/metrics/graph-load.md:225` warns Step 5 about. Rendering per leg and
deduping on the statement emits it **once**.

WHY `ContextDoc` AND NOT `Chunk` (ADR-0011).

A rendered statement is not a corpus row. It has no chunk_id of its own until
this function picks one from the projected provenance, and `Chunk` is
`extra="forbid"` with nowhere to put either that provenance or the `derived`
flag an ADR-0010 bridge needs to carry. `chunk_id` and `citation_label` are
required on every `ContextDoc` including a GRAPH one, and keeping that true "by
construction" is named in the ADR as this function's job -- so a provenance
chunk with no label in `labels` raises here rather than becoming an uncitable
document that Step 6 would have to reject.

DISPLAY NAMES, NOT KEYS.

Every rendered name comes from `display_name`, falling back to `canonical_name`.
ADR-0009's Correction added that field for exactly this: `canonical_name` is
lowercased and de-hyphenated because it is a key, so prose built from it cites
`high risk` and `aia art. 1(1)` instead of `high-risk` and `AIA Art. 1(1)`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Callable

from src.schemas import ContextDoc

__all__ = ["MAX_PROVENANCE", "ANNEX_CAVEAT", "ProseError", "path_to_prose"]

# How many asserting provisions to name inline. Beyond this the statement says
# how many there were rather than listing them: a citation list of 124 entries is
# not evidence, it is the multiplication again.
MAX_PROVENANCE = 3

# Annex VIII and Annex XI are the two annexes whose nodes are ambiguous, and the
# ambiguity is recorded and deliberately unfixed (docs/failure-notes.md:1055-1058,
# `OPEN` since 2026-07-31). `user_prompt()` never passed `section` to the
# extractor, so the three Annex VIII "point 1" chunks reached Command A with
# identical metadata -- and those three are registration duties attaching to
# *different actors* (Section A providers of Annex III systems, Section B
# providers of Art. 6(1) systems, Section C deployers). A citation from this path
# to Annex VIII has not been verified to the section, so the statement says so
# rather than reading as confident. Fixing it costs ~$0.70 of re-extraction and
# is deferred to Phase 5 by decision, not by oversight.
_AMBIGUOUS_ANNEX = re.compile(r"\bannex\s+(viii|xi)\b", re.I)
ANNEX_CAVEAT = (
    "[unverified annex section: the extractor was never given `section`, so this "
    "annex node does not distinguish Sections A/B/C -- see failure-notes.md]"
)


class ProseError(RuntimeError):
    """A row could not be rendered into a citable statement.

    Deliberately not `SystemExit`, matching `RouterError` (router.py:101) and
    `RetrieverError` (retriever.py:58): this runs inside a FastAPI worker at
    Step 7.
    """


def _name(node: Any) -> str | None:
    """A node's prose name. `display_name` first -- ADR-0009's Correction."""
    if not isinstance(node, dict):
        return None
    return node.get("display_name") or node.get("canonical_name")


def _is_ambiguous_annex(*nodes: Any) -> bool:
    return any(
        isinstance(n, dict)
        and "Annex" in (n.get("labels") or [])
        and _AMBIGUOUS_ANNEX.search(str(_name(n) or ""))
        for n in nodes
    )


# --------------------------------------------------------------------------
# The renderers -- one per relationship leg, pure, each testable against a
# hand-built row dict with no database.
#
# A leg is (statement, provenance_key, nodes_touched). A leg that is absent
# returns nothing: `obligations_for_system` and `enforcement_chain` both have
# OPTIONAL legs whose node columns come back null, which is exactly the case the
# templates collect a bare property for rather than a map literal -- an absent
# leg is `[]` and not `[{chunk: None}]`.
# --------------------------------------------------------------------------

Leg = tuple[str, str, tuple[Any, ...]]
Renderer = Callable[[dict[str, Any]], list[Leg]]


def _obligations_for_role(row: dict[str, Any]) -> list[Leg]:
    role, obligation, article = row.get("r"), row.get("o"), row.get("a")
    rn, on, an = _name(role), _name(obligation), _name(article)
    legs: list[Leg] = []
    if rn and on:
        legs.append((f"{on} applies to {rn}.", "applies_chunks", (role, obligation)))
    if an and on:
        legs.append((f"{an} imposes: {on}.", "imposes_chunks", (article, obligation)))
    return legs


def _obligations_for_system(row: dict[str, Any]) -> list[Leg]:
    system, risk = row.get("s"), row.get("rc")
    obligation, article = row.get("o"), row.get("a")
    sn, rn = _name(system), _name(risk)
    on, an = _name(obligation), _name(article)
    legs: list[Leg] = []
    if sn and rn:
        legs.append((f"{sn} is classified as {rn}.", "classified_chunks", (system, risk)))
    if sn and on:
        legs.append((f"{on} applies to {sn}.", "applies_chunks", (system, obligation)))
    if an and on:
        legs.append((f"{an} imposes: {on}.", "imposes_chunks", (article, obligation)))
    return legs


def _enforcement_chain(row: dict[str, Any]) -> list[Leg]:
    obligation, authority, penalty = row.get("o"), row.get("auth"), row.get("p")
    on, aun, pn = _name(obligation), _name(authority), _name(penalty)
    legs: list[Leg] = []
    if on and aun:
        legs.append((f"{on} is enforced by {aun}.", "enforced_chunks", (obligation, authority)))
    if on and pn:
        legs.append((f"{on} is penalised under {pn}.", "penalty_chunks", (obligation, penalty)))
    return legs


def _definition_of(row: dict[str, Any]) -> list[Leg]:
    term, provision = row.get("t"), row.get("a")
    tn, pn = _name(term), _name(provision)
    if tn and pn:
        return [(f"{tn} is defined in {pn}.", "defined_chunks", (term, provision))]
    return []


def _cross_regulation(row: dict[str, Any]) -> list[Leg]:
    """One statement per asserting edge, because direction is per edge.

    `cross_regulation` is the one template whose provenance is a map rather than
    a bare list, and the map is what makes this renderable: the MATCH is
    undirected, so without `outbound` the prose would have to guess which way the
    citation ran (`cypher_templates.py:105-111`). `derived` is per edge too --
    ADR-0010's 22 bridges are inferred from a REFERENCES pair rather than
    asserted, and a statement built on one has to be able to say so.
    """
    a, b = row.get("a"), row.get("b")
    an, bn = _name(a), _name(b)
    if not (an and bn):
        return []
    legs: list[Leg] = []
    for entry in row.get("provenance") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("chunk"), str):
            continue
        head, tail = (an, bn) if entry.get("outbound") else (bn, an)
        legs.append((f"{head} interacts with {tail}.", entry["chunk"], (a, b)))
    return legs


def _path_between(row: dict[str, Any]) -> list[Leg]:
    """One statement per hop, index-aligned to the parallel provenance lists.

    `chunks[i]` names *an* asserting chunk and not *the* one: `shortestPath`
    picks one arbitrary edge where a hop has parallel edges, recorded as a real
    limitation in graph-load.md rather than papered over. Switching to
    `allShortestPaths` to enumerate them is the 24,428-row multiplication in
    another costume (`cypher_templates.py:121-128`).
    """
    path = row.get("p")
    if not isinstance(path, dict):
        return []
    nodes = path.get("nodes") or []
    types = row.get("types") or path.get("types") or []
    chunks = row.get("chunks") or []
    legs: list[Leg] = []
    for i, edge_type in enumerate(types):
        if i + 1 >= len(nodes) or i >= len(chunks):
            break
        head, tail = _name(nodes[i]), _name(nodes[i + 1])
        if not (head and tail) or not isinstance(chunks[i], str):
            continue
        readable = str(edge_type).replace("_", " ").lower()
        legs.append((f"{head} {readable} {tail}.", chunks[i], (nodes[i], nodes[i + 1])))
    return legs


_RENDERERS: dict[str, Renderer] = {
    "obligations_for_role": _obligations_for_role,
    "obligations_for_system": _obligations_for_system,
    "enforcement_chain": _enforcement_chain,
    "definition_of": _definition_of,
    "cross_regulation": _cross_regulation,
    "path_between": _path_between,
}

# The two templates whose provenance is per-edge rather than per-leg, so a Leg's
# second element is a chunk id rather than a column name. Kept as data because
# `test_derived_is_confined_to_interacts_with` pins the same scope on the graph
# side: all 22 derived edges are INTERACTS_WITH, and these are the only two
# templates that can traverse one.
_PER_EDGE = frozenset({"cross_regulation", "path_between"})


def _derived_of(template: str, row: dict[str, Any], chunk: str) -> bool:
    """Was this statement built on an inferred edge (ADR-0010)?"""
    if template == "cross_regulation":
        return any(
            isinstance(e, dict) and e.get("chunk") == chunk and bool(e.get("derived"))
            for e in row.get("provenance") or []
        )
    if template == "path_between":
        chunks = row.get("chunks") or []
        flags = row.get("derived_flags") or []
        return any(c == chunk and bool(flags[i]) for i, c in enumerate(chunks) if i < len(flags))
    return False


def path_to_prose(
    rows: list[dict],
    template: str,
    *,
    labels: Mapping[str, str],
    max_provenance: int = MAX_PROVENANCE,
) -> list[ContextDoc]:
    """Render template rows as citable statements, one per distinct leg.

    `template` is required and the stub's signature did not have it: with only
    the rows, this function could tell an `obligations_for_role` row from a
    `definition_of` row solely by sniffing which keys are present, which is a
    guess dressed as a dispatch. Widened here and recorded in ADR-0013 -- the
    same treatment Step 0 gave the four signatures it changed.

    `labels` maps chunk_id -> citation_label and is injected rather than looked
    up, which keeps this function pure and testable with no database. Step 6
    validates citations against a retrieved set, so a statement whose chunk has
    no label would be a document that cannot survive validation; it raises here
    instead, where the cause is still visible.

    Returns ContextDoc with `score=None` -- a graph statement has no similarity
    score, and nothing may sort or threshold across the two sources on that field
    (retriever.py:26-30).
    """
    if template not in _RENDERERS:
        raise ProseError(f"{template!r} has no renderer; have {sorted(_RENDERERS)}")

    # statement -> (ordered chunk ids, derived, ambiguous annex)
    seen: dict[str, tuple[list[str], bool, bool]] = {}
    for row in rows:
        for statement, provenance_key, nodes in _RENDERERS[template](row):
            if template in _PER_EDGE:
                chunks = [provenance_key]
            else:
                value = row.get(provenance_key)
                chunks = sorted({c for c in (value or []) if isinstance(c, str)})
            if not chunks:
                # An OPTIONAL leg that matched nothing collects to `[]`, which is
                # the shape rule 1 of the template library exists to guarantee.
                continue
            derived = any(_derived_of(template, row, c) for c in chunks)
            caveat = _is_ambiguous_annex(*nodes)
            if statement in seen:
                prior_chunks, prior_derived, prior_caveat = seen[statement]
                seen[statement] = (
                    list(dict.fromkeys([*prior_chunks, *chunks])),
                    prior_derived or derived,
                    prior_caveat or caveat,
                )
            else:
                seen[statement] = (chunks, derived, caveat)

    docs: list[ContextDoc] = []
    for statement, (chunks, derived, caveat) in seen.items():
        missing = [c for c in chunks if c not in labels]
        if missing:
            raise ProseError(
                f"no citation_label for {missing[:3]} (of {len(missing)}) behind "
                f"{statement!r}; a graph statement that cannot be cited must not "
                f"reach the prompt (ADR-0011)"
            )
        shown = chunks[:max_provenance]
        cited = ", ".join(labels[c] for c in shown)
        if len(chunks) > len(shown):
            cited += f", +{len(chunks) - len(shown)} more"
        text = f"{statement} ({cited})"
        if caveat:
            text = f"{text} {ANNEX_CAVEAT}"
        docs.append(
            ContextDoc(
                chunk_id=chunks[0],
                text=text,
                citation_label=labels[chunks[0]],
                source="GRAPH",
                score=None,
                derived=derived,
                # `shown` and not `chunks`. The named provisions are the ones the
                # statement's own text cites, so this is the list a citation may
                # fan out over -- capped at what was shown, never at the full 124.
                # Before ADR-0014 this list was computed here, rendered into the
                # text as labels, and then dropped: `chunk_id` kept one of it and
                # nothing else survived the boundary, so ADR-0013's 24-of-32 was
                # measured against a set `Citation` could not carry.
                provenance=shown,
            )
        )
    return docs
