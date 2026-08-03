"""Parameterized Cypher template library.

The model chooses a template and fills parameters; the query text is fixed.
This is a security control (no injection), a reproducibility control, and an
interview talking point. Never let the model write raw Cypher.

Every template projects distinct node tuples AND the per-edge provenance behind
them, and the way it does that is load-bearing rather than tidiness. The graph
stores one relationship per asserting chunk, so a fact repeated across the corpus
becomes parallel edges -- `high risk ai system -[:CLASSIFIED_AS]-> high risk` is
asserted by 124 different chunks.

That is why provenance is aggregated rather than projected as a column. Naming a
relationship variable and putting `rel.source_chunk_id` in a `RETURN DISTINCT`
makes it part of the grouping key and re-multiplies the rows -- measured on the
loaded graph at **24,428 rows where 169 are correct**, which is the same number
this library returned before `DISTINCT` was added. Inside `collect()` the
relationship variable is aggregated, so the non-aggregated columns become the
implicit grouping key, which is exactly the distinct node tuple `DISTINCT` used
to produce. `DISTINCT` is therefore gone from every template and the row counts
did not move (60 / 169 / 1 / 1 / 4 / 1-path, verified 2026-08-03).

Two rules follow from that, and both are load-bearing:

1. **On an OPTIONAL leg, collect the property, never a map literal.** `collect`
   drops nulls, so `collect(DISTINCT pu.source_chunk_id)` yields `[]` when the
   OPTIONAL MATCH misses. A map literal is never null even when every value in it
   is, so `collect(DISTINCT {chunk: pu.source_chunk_id})` yields
   `[{chunk: null}]` -- measured, not assumed. That is a fake citation with a
   null chunk id, and it would have reached citation validation looking like
   provenance.
2. **`derived` is surfaced only where it can be true.** ADR-0010's 22 bridges are
   all `INTERACTS_WITH` (`MATCH ()-[r]->() WHERE r.derived` returns that type and
   only that type, n=22), so only `cross_regulation` and `path_between` can
   traverse one. `test_derived_is_confined_to_interacts_with` is what keeps that
   scope honest; if a future derivation tags another type, that test fails first.

Column convention: one `*_chunks` list per relationship leg, named for the leg.
"""

from __future__ import annotations

TEMPLATES: dict[str, str] = {
    # role <- obligations <- articles
    "obligations_for_role": """
        MATCH (r:ActorRole {canonical_name: $role})<-[ap:APPLIES_TO]-(o:Obligation)<-[im:IMPOSES]-(a:Article)
        RETURN r, o, a,
               collect(DISTINCT ap.source_chunk_id) AS applies_chunks,
               collect(DISTINCT im.source_chunk_id) AS imposes_chunks
    """,
    # system -> risk category -> obligations chain
    #
    # The obligation leg was originally a second, UNCONNECTED `MATCH (a:Article)
    # -[:IMPOSES]->(o:Obligation)` -- a cartesian product returning every one of
    # the 1,219 IMPOSES edges crossed with the matched system. It returned rows,
    # which is exactly why the bug survived being written. Now it hangs off the
    # system, and it is OPTIONAL so a classified system with no duties of its own
    # still returns its risk grading (34 of the 41 classified systems are that case).
    #
    # `classified_chunks` is the hot fact: for `high risk ai system` it is the same
    # 124 chunk ids on all 169 rows. That is correct -- 124 chunks really do assert
    # the grading -- but a consumer rendering every one of them into a citation
    # list is the multiplication coming back through the prose instead of the rows.
    "obligations_for_system": """
        MATCH (s:SystemType {canonical_name: $system_type})-[ca:CLASSIFIED_AS]->(rc:RiskCategory)
        OPTIONAL MATCH (s)<-[ap:APPLIES_TO]-(o:Obligation)<-[im:IMPOSES]-(a)
        RETURN s, rc, o, a,
               collect(DISTINCT ca.source_chunk_id) AS classified_chunks,
               collect(DISTINCT ap.source_chunk_id) AS applies_chunks,
               collect(DISTINCT im.source_chunk_id) AS imposes_chunks
    """,
    # obligation -> authority + penalty article. Only 4 of the 216 enforced
    # obligations also carry PENALIZED_UNDER, so `penalty_chunks == []` is the
    # common case and the one the plain-property collect exists to get right.
    "enforcement_chain": """
        MATCH (o:Obligation {canonical_name: $obligation})-[en:ENFORCED_BY]->(auth:Authority)
        OPTIONAL MATCH (o)-[pu:PENALIZED_UNDER]->(p:Article)
        RETURN o, auth, p,
               collect(DISTINCT en.source_chunk_id) AS enforced_chunks,
               collect(DISTINCT pu.source_chunk_id) AS penalty_chunks
    """,
    # term -> defining provision. The head is any type -- DEFINED_IN accepts nine
    # of them -- so it carries only the shared :Entity label, which exists to give
    # these otherwise-unlabeled matches an index instead of a full scan.
    #
    # The tail was pinned to :Article and silently missed the 48 terms defined only
    # in an Annex (of 337 defined terms). DEFINED_IN's endpoint contract is
    # Article *or* Annex -- AIA Annex IV defines `computational resources`, Annex
    # VIII defines `status of the ai system` -- so pinning one of the two was a
    # partial version of the cross_regulation bug, and it survived a non-empty test
    # because the term used to probe it happened to be defined in an Article.
    "definition_of": """
        MATCH (t:Entity {canonical_name: $term})-[df:DEFINED_IN]->(a:Article|Annex)
        RETURN t, a,
               collect(DISTINCT df.source_chunk_id) AS defined_chunks
    """,
    # INTERACTS_WITH neighborhood -- the AI Act <-> GDPR bridge.
    #
    # Both ends were :Article, and the template returned ZERO rows on the loaded
    # graph: all 130 INTERACTS_WITH edges point at the *instrument*, not at an
    # article of it (Article->Regulation 108, Annex->Regulation 11,
    # Regulation->Regulation 10, Right->Regulation 1). The bridge was there all
    # along; the template was asserting a shape the corpus never produced.
    #
    # The match is undirected, so the edge alone does not say which way the
    # citation ran -- `startNode(ix) = a` is what lets path_to_prose render "AIA
    # Art. 2(7) interacts with GDPR" rather than guess. This is the one template
    # whose provenance is a map: its only leg is mandatory, so there is no null to
    # be smuggled through in a map literal (see rule 1 in the module docstring),
    # and keeping chunk/derived/direction paired per edge is what lets an answer
    # say *which* of its citations came from an inferred bridge.
    "cross_regulation": """
        MATCH (a:Entity {canonical_name: $article})-[ix:INTERACTS_WITH]-(b:Entity)
        RETURN a, b,
               collect(DISTINCT {
                   chunk: ix.source_chunk_id,
                   derived: ix.derived,
                   outbound: startNode(ix) = a
               }) AS provenance
    """,
    # bounded shortest path (<= 4 hops)
    #
    # `shortestPath` returns ONE path, and where the hop it picks has parallel
    # edges the driver picks one of them arbitrarily -- so `chunks` names *an*
    # asserting chunk, not *the* asserting chunk. That is a real limitation and it
    # is recorded in docs/metrics/graph-load.md rather than papered over.
    # `allShortestPaths` would enumerate them and is exactly the 24,428-row
    # multiplication in another costume; do not switch to it.
    "path_between": """
        MATCH p = shortestPath(
            (a:Entity {canonical_name: $entity_a})-[*..4]-(b:Entity {canonical_name: $entity_b})
        )
        RETURN p,
               [r IN relationships(p) | r.source_chunk_id] AS chunks,
               [r IN relationships(p) | type(r)]           AS types,
               [r IN relationships(p) | r.derived]         AS derived_flags
    """,
}

# The declared parameter set per template. This is the other half of ADR-0002:
# the model picks a template *name* and fills *declared* parameters, and both are
# checked against this library before anything reaches a driver. Kept as a sibling
# dict rather than folded into TEMPLATES so `TEMPLATES[name]` stays a plain string
# a caller can hand straight to `session.run`.
TEMPLATE_PARAMS: dict[str, frozenset[str]] = {
    "obligations_for_role": frozenset({"role"}),
    "obligations_for_system": frozenset({"system_type"}),
    "enforcement_chain": frozenset({"obligation"}),
    "definition_of": frozenset({"term"}),
    "cross_regulation": frozenset({"article"}),
    "path_between": frozenset({"entity_a", "entity_b"}),
}


def get_template(name: str) -> str:
    return TEMPLATES[name]
