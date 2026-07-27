# ADR 0002 — Why templates over model-generated Cypher

## Status
Accepted

## Context
Letting the model emit raw Cypher risks injection into the graph DB and makes
queries unreproducible.

## Decision
A fixed library of ~6 parameterized Cypher templates; the model only chooses a
template and fills parameters.

## Consequences
Security + reproducibility control at the cost of query flexibility.
