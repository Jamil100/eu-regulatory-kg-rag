"""Parameterized Cypher template library.

The model chooses a template and fills parameters; the query text is fixed.
This is a security control (no injection), a reproducibility control, and an
interview talking point. Never let the model write raw Cypher.
"""

from __future__ import annotations

TEMPLATES: dict[str, str] = {
    # role <- obligations <- articles
    "obligations_for_role": """
        MATCH (r:ActorRole {canonical_name: $role})<-[:APPLIES_TO]-(o:Obligation)<-[:IMPOSES]-(a:Article)
        RETURN r, o, a
    """,
    # system -> risk category -> obligations chain
    "obligations_for_system": """
        MATCH (s:SystemType {canonical_name: $system_type})-[:CLASSIFIED_AS]->(rc:RiskCategory)
        MATCH (a:Article)-[:IMPOSES]->(o:Obligation)
        RETURN s, rc, o, a
    """,
    # obligation -> authority + penalty article
    "enforcement_chain": """
        MATCH (o:Obligation {canonical_name: $obligation})-[:ENFORCED_BY]->(auth:Authority)
        OPTIONAL MATCH (o)-[:PENALIZED_UNDER]->(p:Article)
        RETURN o, auth, p
    """,
    # term -> defining article + text
    "definition_of": """
        MATCH (t {canonical_name: $term})-[:DEFINED_IN]->(a:Article)
        RETURN t, a
    """,
    # INTERACTS_WITH neighborhood
    "cross_regulation": """
        MATCH (a:Article {canonical_name: $article})-[:INTERACTS_WITH]-(b:Article)
        RETURN a, b
    """,
    # bounded shortest path (<= 4 hops)
    "path_between": """
        MATCH p = shortestPath(
            (a {canonical_name: $entity_a})-[*..4]-(b {canonical_name: $entity_b})
        )
        RETURN p
    """,
}


def get_template(name: str) -> str:
    return TEMPLATES[name]
