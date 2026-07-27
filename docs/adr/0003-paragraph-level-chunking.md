# ADR 0003 — Why paragraph-level chunking

## Status
Accepted

## Context
A full article is often too long to embed cleanly; a single point is often too
short to stand alone.

## Decision
Chunk at the paragraph level with deterministic human-readable IDs
(e.g. `aia-art26-para1`).

## Consequences
Readable citations and easier debugging, at the cost of some cross-paragraph
context that the graph path recovers.
