"""Parameterized Cypher template library.

The model chooses a template and fills parameters; the query text is fixed.
This is a security control (no injection), a reproducibility control, and an
interview talking point. Never let the model write raw Cypher.

Every template that projects nodes uses RETURN DISTINCT, and that is load-bearing
rather than tidiness. The graph stores one relationship per asserting chunk, so a
fact repeated across the corpus becomes parallel edges -- `high risk ai system
-[:CLASSIFIED_AS]-> high risk` is asserted by 124 different chunks. Without
DISTINCT that one hot fact multiplied `obligations_for_system` from 169 rows to
24,428. The projections return nodes, so distinct node tuples is the correct
semantic; per-chunk provenance stays on the edges for citation validation to read.
"""

from __future__ import annotations

TEMPLATES: dict[str, str] = {
    # role <- obligations <- articles
    "obligations_for_role": """
        MATCH (r:ActorRole {canonical_name: $role})<-[:APPLIES_TO]-(o:Obligation)<-[:IMPOSES]-(a:Article)
        RETURN DISTINCT r, o, a
    """,
    # system -> risk category -> obligations chain
    #
    # The obligation leg was originally a second, UNCONNECTED `MATCH (a:Article)
    # -[:IMPOSES]->(o:Obligation)` -- a cartesian product returning every one of
    # the 1,219 IMPOSES edges crossed with the matched system. It returned rows,
    # which is exactly why the bug survived being written. Now it hangs off the
    # system, and it is OPTIONAL so a classified system with no duties of its own
    # still returns its risk grading (34 of the 41 classified systems are that case).
    "obligations_for_system": """
        MATCH (s:SystemType {canonical_name: $system_type})-[:CLASSIFIED_AS]->(rc:RiskCategory)
        OPTIONAL MATCH (s)<-[:APPLIES_TO]-(o:Obligation)<-[:IMPOSES]-(a)
        RETURN DISTINCT s, rc, o, a
    """,
    # obligation -> authority + penalty article
    "enforcement_chain": """
        MATCH (o:Obligation {canonical_name: $obligation})-[:ENFORCED_BY]->(auth:Authority)
        OPTIONAL MATCH (o)-[:PENALIZED_UNDER]->(p:Article)
        RETURN DISTINCT o, auth, p
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
        MATCH (t:Entity {canonical_name: $term})-[:DEFINED_IN]->(a:Article|Annex)
        RETURN DISTINCT t, a
    """,
    # INTERACTS_WITH neighborhood -- the AI Act <-> GDPR bridge.
    #
    # Both ends were :Article, and the template returned ZERO rows on the loaded
    # graph: all 130 INTERACTS_WITH edges point at the *instrument*, not at an
    # article of it (Article->Regulation 108, Annex->Regulation 11,
    # Regulation->Regulation 10, Right->Regulation 1). The bridge was there all
    # along; the template was asserting a shape the corpus never produced.
    "cross_regulation": """
        MATCH (a:Entity {canonical_name: $article})-[:INTERACTS_WITH]-(b:Entity)
        RETURN DISTINCT a, b
    """,
    # bounded shortest path (<= 4 hops)
    "path_between": """
        MATCH p = shortestPath(
            (a:Entity {canonical_name: $entity_a})-[*..4]-(b:Entity {canonical_name: $entity_b})
        )
        RETURN p
    """,
}


def get_template(name: str) -> str:
    return TEMPLATES[name]
