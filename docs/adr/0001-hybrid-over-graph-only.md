# ADR 0001 — Why hybrid over graph-only (or vector-only)

## Status
Accepted

## Context
Embeddings capture semantic similarity but not structure; graphs capture
structure but handle fuzzy language poorly.

## Decision
Build both retrieval paths and route between them with Command R7B.

## Consequences
Higher per-query cost and latency, offset by decisive accuracy gains on
multi-hop, cross-regulation, and aggregation questions.
