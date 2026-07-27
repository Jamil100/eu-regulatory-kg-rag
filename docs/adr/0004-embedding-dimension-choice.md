# ADR 0004 — Embedding dimension choice

## Status
Proposed (pending measurement)

## Context
Embed v4 supports Matryoshka truncation (1536 -> 512/256) and int8 quantization.

## Decision
Index at 1536 and 512, measure recall@10 on ~20 labeled queries; adopt 512 if
it loses <2% recall.

## Consequences
Potential 3x storage/latency saving; decision recorded once measured.
