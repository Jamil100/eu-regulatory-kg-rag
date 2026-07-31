# Vector index metrics

Status: **1,108 of 1,108 chunks embedded at both 1536 and 512 dimensions, loaded
into pgvector, recall measured.** ADR-0004 resolved.

Fourth companion to `extraction-cost-and-findings.md` (what came out of the
model), `graph-load.md` (what came out of the loader) and `eval-set.md` (the
instrument). This one measures the vector half of the hybrid store.

Regenerate with:

```bash
python -m src.index.embedder --apply        # load (idempotent)
python -m src.index.pgvector_schema         # what is in the database
python -m src.index.recall_harness          # the numbers below
pytest tests/test_embedder.py tests/test_chunks.py
```

---

## Shape

| | |
|---|---|
| Rows | **1,108** |
| paragraph / annex / definition | **906 / 108 / 94** |
| Embedded at 1536 | **1,108** |
| Embedded at 512 | **1,108** |
| Rows with a citation label | **1,108** (0 null, all distinct) |
| Rows with ≥1 resolved entity | **1,107** |
| `entity_ids` references | **7,465**, of which **0** name a node absent from the graph |
| pgvector | 0.8.6 |

**The one chunk with no entities is `gdpr-art70-para1`** — the 864-token EDPB
task list whose JSON does not fit in Command A's 8,192-token output ceiling, so
it never extracted. It is in the vector store with a working citation label. That
is not a loose end; it is the vector path covering a graph gap, and the fact that
it is *exactly* the one row with empty `entity_ids` is a self-consistency check
that passed without being arranged.

## Cost and time

| Stage | Cost | Time |
|---|---|---|
| Embed 1,108 chunks @ 512 | $0.0118 | 9.7 s |
| Embed 1,108 chunks @ 1536 | $0.0118 | 62.2 s |
| **Total API** | **$0.0236** | |
| Entity resolution (for `entity_ids`) | — | 0.14 s |
| Metadata upsert, 1,108 rows | — | 1.86 s |
| HNSW build, both columns | — | 0.78 s |

Billed input is 98,020 tokens per arm against the chunker's own count of 79,774
— Cohere's tokenizer, not a discrepancy to chase. Same token count either
dimension: `output_dimension` changes what comes back, not what is read.

**The whole vector index costs about two and a half cents.** Extraction cost
~$24. Worth stating plainly, because the intuition that "the embeddings" are a
significant line item is wrong by three orders of magnitude here — the money is
in the generative pass, and it always was.

## Storage

| | |
|---|---|
| Heap | 1,736 kB |
| TOAST | 12 MB |
| Total relation | 26 MB |
| `chunks_embedding_1536_hnsw` | **8,784 kB** |
| `chunks_embedding_512_hnsw` | **2,944 kB** |

The HNSW indexes are **2.98×** apart, which is the 3× ADR-0004 predicted, landing
almost exactly.

**Both vector columns are `storage=e` (EXTERNAL).** A 1536-dim `vector` is 6,148
bytes, comfortably past the ~2 kB threshold, so every vector lives out of line
and every comparison pays a TOAST fetch. Heap is 1.7 MB; TOAST is 12 MB. This is
the mechanism behind the latency table below — the gap between the arms is 8×,
not 3×, because it is 3× more data through a slower path rather than 3× more
arithmetic.

## Recall

21 labeled queries (the rows of `eval/eval-questions.jsonl` carrying gold
`source_chunk_ids`), 51 gold chunk references, k=10.

| dim | plan | ef_search | micro recall@10 | hit rate@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| 512 | exact | – | 28/51 — 54.9% | 85.7% | **6.68** | 7.75 |
| 512 | hnsw | 40 | 28/51 — 54.9% | 85.7% | 10.00 | 12.41 |
| 512 | hnsw | 100 | 28/51 — 54.9% | 85.7% | 9.04 | 11.25 |
| 512 | hnsw | 200 | 28/51 — 54.9% | 85.7% | 9.36 | 12.01 |
| 1536 | exact | – | 29/51 — 56.9% | 85.7% | 53.41 | 57.09 |
| 1536 | hnsw | 40 | 29/51 — 56.9% | 85.7% | 51.98 | 53.13 |
| 1536 | hnsw | 100 | 29/51 — 56.9% | 85.7% | 59.46 | 62.42 |
| 1536 | hnsw | 200 | 29/51 — 56.9% | 85.7% | 60.08 | 65.56 |

**Two metrics on purpose.** The set averages 2.4 gold chunks per question, so
"did it find *the* paragraph" and "did it find *all* the paragraphs" are
different questions. Hit rate is 85.7% and micro recall is 55–57%: the retriever
usually finds the thread and usually misses most of the rest of it. For a legal
citation that difference is the whole game, and a single averaged number hides
it.

The k=10 ceiling costs 1 reference: `ag-001` declares 11 gold chunks, so 50/51
(98.0%) is the maximum achievable, not 100%.

### The result that matters is per-stratum

Exact search, 1536:

| Stratum | micro recall@10 |
|---|---|
| hard-negative | 2/2 — **100%** |
| single-hop | 6/8 — **75%** |
| cross-regulation | 7/10 — 70% |
| three-hop | 4/6 — 67% |
| aggregation | 7/15 — 47% |
| two-hop | 3/10 — **30%** |

**This is the shape the whole project predicted, measured before the graph path
exists.** Vector search handles single-paragraph questions well and falls apart
on multi-hop and aggregation — exactly the blind spot ADR-0001 gave as the reason
for a knowledge graph. The two-hop 30% and aggregation 47% are the numbers the
hybrid has to beat in Phase 5, and having them now means the Phase 5 claim will
be a delta against a measured baseline rather than against an assumption.

The 20 gold chunks exact search never retrieves concentrate in AIA Art. 26
(6 of the 11 deployer-obligation paragraphs) and Art. 9 (4 of 6) — long articles
where each paragraph restates the subject only obliquely, so a question naming
the subject once retrieves one paragraph and ranks its siblings below ten
unrelated chunks that mention the subject explicitly.

### HNSW earns nothing at this scale, and that is a finding

At 1,108 rows the planner chooses a **Seq Scan** over the index every time
(`EXPLAIN` confirms; `Execution Time: 6.5 ms`), and it is right to. So:

- every recall number above is **exact search**, i.e. the true ceiling for this
  embedding at k=10 — the index cannot be blamed for 57%;
- forced onto the index with `enable_seqscan = off`, recall is **identical at
  every `ef_search`** and latency is **worse**;
- the `ef_search` sweep the ADR asked for has no signal to report yet.

The indexes are kept — 11.7 MB is cheap and the corpus will grow — but the sweep
is run through an explicit `force_index` path in the harness. Left on the
planner's default it would compare three identical plans and print a flat line
that reads like a tuning result.

## Defects fixed on the way in

Three were recorded while planning Phase 2 and are now closed; the fourth was
found during the work.

| Defect | Was | Now |
|---|---|---|
| `Chunk` rejected the corpus | **1,000 of 1,108 rows** failed validation | 1,108/1,108, asserted in `tests/test_chunks.py` |
| `schema.sql` had `article`/`paragraph` only | 202 chunks would load as anonymous text | 11 typed columns, per-shape provenance asserted |
| `entity_ids TEXT[]` declared, never written | always `'{}'` | 7,465 references, 0 dangling against the graph |
| **`section` dropped by the chunker** | 25 chunks shared 11 citation labels | 32 rows carry a section; labels unique |

The `Chunk` figure is worth its own line: the note that recorded it said "586 of
694 AI Act rows". That was the AI Act file only — GDPR articles are integers
too, so **all 414 GDPR rows failed as well**. The undercount came from checking
one file and reporting it as the corpus.

**The fourth was found by the uniqueness assertion, not by reading code.**
`annex_parser` computes a `section` for Annexes VIII (A/B/C) and XI (1/2), whose
point numbers restart per section; `chunker` used it to build the chunk_id and
then dropped it from the written record. To anything reading fields rather than
parsing ids, `Annex VIII(1)` named the registration duties of three different
actors at once. The id was right the whole time, which is why nothing had
noticed. Backfilled by re-deriving the annex rows from the source HTML and
asserting every other field byte-identical — 0 mismatches, so the fix adds a key
and changes nothing else. No re-extraction: `cache_key()` hashes only text, and
`user_prompt()` iterates a fixed key tuple that never included `section`.

## Open

- **The extractor was never told which section an Annex VIII point belongs to.**
  `user_prompt()`'s key tuple has no `section`, so the model could not have
  distinguished them either — the graph's Annex VIII nodes are as ambiguous as
  the citation labels were. Fixing it means re-extracting 32 chunks (~$0.70).
- **`embedding_1536` is retained without a job.** Kept so ADR-0004 can be re-run
  when the eval set can resolve a 2% difference; drop it when it cannot.
- **GDPR-side retrieval is nearly unmeasured** — 6 of 40 gold chunks, inherited
  straight from `eval-set.md`.
- **No reranker measurement yet.** Rerank 3.5 over the top-50 is the obvious
  lever on the 30% two-hop figure and is Phase 3 work.
- **Recall is measured on the corpus as chunked**, so paragraph-level chunking
  (ADR-0003) is untested against alternatives. The Art. 26 / Art. 9 miss pattern
  is the first evidence that it has a cost.
