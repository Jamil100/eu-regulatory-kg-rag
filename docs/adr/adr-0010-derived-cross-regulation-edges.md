# ADR 0010 — Derive article-level cross-regulation edges at load time

## Status

Accepted (2026-07-31). Implemented in `src/ingest/graph_writer.py`
(`_derive_cross_regulation_bridges`), asserted in `tests/test_graph_writer.py`.

## Context

`INTERACTS_WITH` is the AI Act ↔ GDPR bridge. The roadmap specifies it as *"article → article across
regulations — the AI Act ↔ GDPR bridge, and your best demo material"*, `ALLOWED_ENDPOINTS` permits
`Article↔Article`, and the `cross_regulation` Cypher template is written against that shape.

The loaded graph had **130 `INTERACTS_WITH` edges and not one connected two articles.** Every edge
terminated at a `Regulation` node.

The extractor was not missing the link. In **12 of 12** chunks that cite a specific foreign article, it
identified the article correctly, created the entity, and emitted a `REFERENCES` edge to it — then
emitted `INTERACTS_WITH` pointing at the instrument instead:

```
aia-art26-para9   REFERENCES      AIA Art. 26(9) → GDPR Art. 35   ← the article-level bridge, present
                  INTERACTS_WITH  AIA Art. 26(9) → GDPR           ← collapsed to the instrument
```

Root cause: **the system prompt's own few-shot example demonstrates the collapse.** The example builds
a `GDPR Art. 4(14)` entity, emits `REFERENCES` to it, then emits `INTERACTS_WITH → GDPR`. The model
reproduced what it was shown. This is the third defect traced to a prompt demonstration, after the
`RiskCategory` junk drawer and ADR-0007's `LawfulBasis` collapse.

Three options:

1. **Fix the prompt (ontology v4) and re-extract.** Correct at the source, but `cache_key()` hashes the
   system prompt, so it invalidates all 1,108 cached responses: ~$24 and several hours.
2. **Accept Regulation-level-only bridges.** Contradicts the roadmap and the ontology, and leaves
   `cross_regulation` returning nothing useful.
3. **Derive the article-level edge from the `REFERENCES` edge that already encodes it.** Deterministic,
   zero API cost, no cache invalidation.

## Decision

**Option 3.** At graph-load time, for every relationship where:

```
type == "REFERENCES"
  and type_of(head) in {Article, Annex} and type_of(tail) in {Article, Annex}
  and regulation_of(head) is not None and regulation_of(tail) is not None
  and regulation_of(head) != regulation_of(tail)
```

emit an additional `INTERACTS_WITH head→tail`, tagged `derived: true` and inheriting the source
edge's `source_chunk_id` and `confidence`.

`regulation_of` reads the instrument prefix off the resolved canonical name (`aia`, `gdpr`, `led`,
`eudpr`), which entity resolution already namespaces.

**Result: 22 bridges — 19 `Article→Article`, 3 `Annex→Article`.** Zero overlap with existing edges.
`INTERACTS_WITH` goes 130 → 152; `Article→Article` goes 0 → 19.

Landed with one validation-only ontology correction: `ALLOWED_ENDPOINTS["INTERACTS_WITH"]`'s head set
widened to include `Annex`, which also cleared **11 pre-existing endpoint violations** (241 → 230).

### Three constraints that make this a derivation and not an invention

1. **It asserts nothing the text does not.** A `REFERENCES` edge means the model read one provision
   citing another. Promoting a cross-boundary citation to "these two provisions interact" restates the
   same fact under the type the query layer expects. No new pair is created.
2. **Derived edges are labelled.** `derived: true` is a property on every one, so any analysis can
   separate what the model asserted from what the loader inferred. They keep `source_chunk_id`, so
   provenance and the pgvector join key survive.
3. **Both endpoints must be namespaced.** See the near-miss below.

## Consequences

**What it fixes.** `cross_regulation` returns article-level rows. Two eval questions become genuine
article-level traversals: `xr-001` gains `AIA Art. 3(37) → GDPR Art. 9(1)` — the special-categories
definition the question is actually about — and `xr-002` gains `AIA Art. 26(9) → GDPR Art. 35`, the
DPIA routing.

**What it deliberately cannot do.** The rule fires only where a cross-boundary `REFERENCES` edge
already exists. **AIA Art. 99 and GDPR Art. 83 — the two penalty regimes — never cite each other**, so
no bridge appears between them and none is invented. Those questions (`xr-003`, `xr-004`) are marked
`graph_traversable: false` in the eval set and reported as having no hybrid advantage. That is a real
result about the corpus, and manufacturing an edge to make the demo look better would have been the
single worst thing this ADR could have done. A test asserts no derived edge touches either article.

**The near-miss, recorded because it is the whole risk of this decision in miniature.** The first
implementation checked only whether the *head* carried an instrument prefix. A tail with no prefix then
counted as "a different regulation", and the pass produced **38 bridges instead of 22** — 16 of them
fabricated between a real article and a bare, un-namespaced `article 35`. It was caught only because 38
disagreed with the 22 measured during planning. A rule that invents edges can quietly manufacture
support for whatever you were hoping to show; the only defence is a number measured before the change
and compared after. Both endpoints must now be namespaced, and there is a test for it.

**What it does not fix — `OPEN`, ontology v4.** The prompt example still teaches the collapse, so any
future corpus extracted with this prompt has the same hole and needs the same load-time patch. The fix
is deferred, not declined: it costs a full re-extraction, and this recovers the same edges for free. It
should ride along with the next change that forces a re-run.

**Precedent set.** This is the first place the loader adds an edge the model did not emit. The bar
applied here — restates an existing assertion, labelled, provenance-preserving, cannot fabricate a pair,
tested both for what it does and for what it must not do — is the bar any future derivation should
clear. Cheap inference at load time is not free; it is a claim about the law.

## Alternatives rejected

- **Re-extract under a corrected prompt (v4).** Correct at the source and still the right eventual fix,
  but ~$24 and hours to recover edges that are already present under another type. Deferred, recorded.
- **Accept instrument-level bridges only.** Would have meant re-stratifying every cross-regulation
  question as vector-only and contradicting both the ontology and the roadmap. The evidence said the
  design intent was article-level and the extractor simply mis-filed it.
- **Link conceptually parallel provisions** (pair the penalty tiers, pair the discretionary-factor
  lists) so `xr-003`/`xr-004` become traversable. Rejected: that inference is not present in any text,
  and it is exactly the fabrication this ADR exists to rule out.
