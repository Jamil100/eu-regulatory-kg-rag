# ADR 0004 — Embedding dimension choice

## Status

**Accepted (2026-07-31).** Was *Proposed (pending measurement)* since scaffolding.
Measured with `python -m src.index.recall_harness`; full numbers in
`docs/metrics/vector-index.md`.

## Context

Embed v4 supports Matryoshka truncation (1536 → 512/256) and int8 quantization.
The question the ADR set was whether the smaller vector costs enough recall to
be worth its extra storage and latency.

Both arms are columns on the same `chunks` table (`embedding_1536`,
`embedding_512`), so the comparison sees one identical row set by construction
rather than by verification. 512 is requested through the API's
`output_dimension` parameter — Cohere's own truncate-and-renormalise — not a
client-side slice.

## Decision

**Adopt 512** — but on storage and latency, not on the recall verdict the
original rule asked for.

The stated rule was "adopt 512 if it loses <2% recall". Measured on 21 labeled
queries carrying 51 gold chunk references, at k=10, exact search:

| | 1536 | 512 |
|---|---|---|
| micro recall@10 | 29/51 — **56.9%** | 28/51 — **54.9%** |
| hit rate@10 | **85.7%** | **85.7%** |
| p50 latency | 53.4 ms | **6.7 ms** |
| HNSW index size | 8.8 MB | **2.9 MB** |

The rule technically passes: 1.96% < 2%. **It should not be read as a result.**
The entire difference is **one gold chunk in one query** (`xr-003`), and one
chunk over 21 queries is inside this eval set's resolution — the same 51
references would move the figure by 2% if a single question were reworded. The
arms are indistinguishable on recall.

What is not ambiguous is everything else: **3.0× the index size and ~8× the
query latency** for a difference the measurement cannot see. That is the basis
for the decision.

## Consequences

- `embedding_512` becomes the query path. `embedding_1536` stays in the table
  for now so the comparison can be re-run when the eval set reaches its 100-row
  target and can actually resolve a 2% difference; it should be dropped once it
  has stopped earning its 8.8 MB.
- **The latency gap is bigger than 3× because the vectors are TOASTed.** Both
  columns are `storage=e`, so a 1536-dim vector (6,148 bytes) lives out of line
  and every comparison pays a TOAST fetch: heap is 1.7 MB against 12 MB of
  TOAST. The 8× is 3× more data through a slower path, not 3× more arithmetic.
- **The HNSW indexes currently earn nothing and are kept deliberately.** At 1,108
  rows the planner chooses a Seq Scan every time and is right to — exhaustive
  search over the whole corpus costs ~6 ms at 512 dims. Forced onto the index,
  recall is *identical* at every `ef_search` (40/100/200) and latency is
  *worse*. So the recall numbers above are exact-search numbers, and the
  measured ceiling belongs to the embedding, not the index.
- The `ef_search` sweep the ADR called for therefore has no signal to report at
  this corpus size. It is retained in the harness (`--force-index` path) because
  it becomes meaningful the moment the corpus grows, and because a sweep left on
  the planner's default silently compares three identical plans and prints a
  flat line that looks like a finding.

## int8: checked, not adopted

Cohere returns int8 embeddings via `embedding_types=["int8"]`. **pgvector has no
int8 vector type**, so they cannot be stored as a searchable vector — there is
`vector` (fp32), `halfvec` (fp16), `bit` and `sparsevec`, and nothing in
between. The roadmap line "int8 quantization" is not implementable against this
store as written.

`halfvec` is the precision lever that does exist (pgvector 0.8.6 is installed,
well past the 0.7.0 that introduced it) and would halve storage again on top of
the dimension saving. Deliberately deferred: the dimension experiment already
delivers 3×, and a second storage arm would need its own recall measurement on
an eval set that has just been shown to be too small to resolve one.

## Caveat that travels with every number here

The gold set is 40 distinct chunks — **3.6% of the corpus** — and 34 of the 40
are AI Act, so GDPR-side retrieval is close to unmeasured. This is sound for a
*comparison* where both arms see the same slice. It is thin as an *absolute*
retrieval claim, and the 56.9% should never be quoted without the 3.6%
attached. See `docs/metrics/eval-set.md`.
