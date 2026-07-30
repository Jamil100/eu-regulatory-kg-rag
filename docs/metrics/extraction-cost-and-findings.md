# Extraction: cost and findings

## What this is for

The extraction pass (`src/ingest/extract.py`) calls Cohere Command A once per
chunk to pull ontology-constrained entities and relationships out of the EU AI
Act and GDPR. Every call costs money, and the corpus is 1108 chunks, so we
validate the ontology on a 10-chunk sample **before** paying for a full run.

This file records what those sample runs cost and what they revealed. It exists
so that:

- the full-corpus cost is a number someone checked, not a guess;
- the pricing assumption behind that number is written down and auditable;
- ontology changes have a before/after record — we can see whether a schema
  change actually improved extraction quality or just moved the problem.

Update this file whenever the ontology or prompt changes materially. Nothing
here is generated automatically; the numbers are copied from the cost report
`extract.py` prints at the end of each run.

Status: **full corpus extracted — 1107 of 1108 chunks (99.9%) under ontology v3.**
Actual cost ≈ **$24** against a $23.13 estimate. See Run 4.

---

## Pricing assumption

| | USD per 1M tokens |
|---|---|
| Command A input | $2.50 |
| Command A output | $10.00 |

Set in `extract.py` as `PRICE_INPUT_PER_TOKEN` / `PRICE_OUTPUT_PER_TOKEN`.
**Verify against cohere.com/pricing before trusting any estimate below** — every
figure here scales linearly with these two numbers.

Token counts come from the Cohere response's `usage.tokens` fields, not from an
estimator, so they are exact for the calls actually made.

---

## Run 1 — ontology v1, 10 test chunks

| Metric | Value |
|---|---|
| Chunks processed | 10 |
| Succeeded / failed | 10 / 0 (0.0% failure rate) |
| Retries | 0 |
| API calls | 10 |
| Total input tokens | 27,600 |
| Total output tokens | 8,133 |
| Avg tokens/chunk | 3,573 (2,760 in + 813 out) |
| Cost of run | $0.1503 |
| Cost per chunk | $0.0150 |
| **Estimated full corpus (1108 chunks)** | **$16.66** |

Note the corpus is **1108 chunks** (694 AI Act + 414 GDPR), not the 1016 assumed
when the work was scoped.

### Caveats on the $16.66

- **The sample is not representative.** The 10 test chunks were chosen to stress
  the ontology, so they skew long — three are 200–550 tokens against a corpus
  median well below that. Real cost is likely somewhat *under* $16.66 on this
  basis alone.
- **Working against that**, the ontology v2 system prompt is ~430 tokens longer
  than v1, and the system prompt is resent on every single call. At 1108 chunks
  that is roughly +480K input tokens, about +$1.20.
- **Prompt caching is not in use.** The system prompt is ~2,400 of the ~2,760
  average input tokens — i.e. ~87% of input spend is the same text 1108 times.
  If Cohere exposes prompt caching for Command A, that is the single biggest
  available saving.
- Retries cost a full second call. At the observed rate they are negligible.

---

## Run 2 — ontology v2, 4 re-extracted chunks

Only the four chunks affected by the v2 schema change were re-run
(`gdpr-art6-para1`, `gdpr-art9-para2`, `aia-art3-def37`, `aia-art9-para1`).

| Metric | Value |
|---|---|
| Chunks processed | 4 |
| Succeeded / failed | 4 / 0 |
| Retries | 1 (recovered) |
| API calls | 5 |
| Total input tokens | 19,348 |
| Total output tokens | 5,280 |

**Do not read a corpus estimate off this run.** These are four of the longest
chunks in the sample plus a retry, so its $0.0202/chunk is a worst case, not an
average. Run 1 remains the baseline.

This run was the first live exercise of the retry path: one response failed
Pydantic validation, was re-sent with the validation error appended, and passed.

---

## Run 3 — pre-flight probes, and ontology v3

Two probes before committing to the full run, on the reasoning that `cache_key()`
hashes the system prompt: any ontology fix discovered *after* the full run
invalidates all 1,108 cached responses and costs a second full run. Probes cost
~$0.28 total; the insurance is on a ~$15–23 purchase.

### 3a — penalty probe (3 chunks, ontology v2)

`aia-art99-para1`, `aia-art99-para4`, `gdpr-art83-para5`. Purpose: exercise
`PENALIZED_UNDER`, flagged in Run 1 as the one relationship type with zero uses.

**It fired** — 7 edges on the AIA chunk, 6 on the GDPR chunk, correctly typed
`Obligation → Article`. The type is no longer untested. Reading the rest of the
output found four problems; see ADR-0008.

Cost $0.0739 for 3 chunks ($0.0246/chunk). **Do not read a corpus estimate off
this run** — penalty provisions are list-dense, so output ran 1,586 tok/chunk
against the pilot's 813. Its $27.28 projection is a worst case.

### 3b — representative sample (15 chunks, ontology v2)

Seeded random draw (seed 42), proportional across both regulations, excluding
already-extracted chunks. **Sample mean 65 tokens vs corpus mean 72 — a ratio of
0.91**, against the existing pilot's 2.08. This is the first honest per-chunk
figure; Run 1's sample bias caveat is now resolved rather than merely noted.

| Metric | Value |
|---|---|
| Chunks processed | 15 |
| Succeeded / failed | 15 / 0 (0.0%) |
| Retries | 1 (6.7% — the pilot's 0% was optimistic) |
| Avg tokens/chunk | 3,420 in + 448 out |
| Cost per chunk | $0.0130 |
| **Estimated full corpus (v2 ontology)** | **$14.43** |

Quality on short text was the open worry and it held up: a 27-token chunk yielded
3 entities and 2 relationships, not an over-extracted blob. No evidence that Rule 1
("extract only what THIS chunk states") breaks down as chunks get shorter.

### 3c — re-run under ontology v3

Both probes were re-run after the ADR-0008 changes, since editing the prompt
invalidates the cache anyway. 28 chunks now stored.

| | v2 | v3 |
|---|---|---|
| Avg input tokens/chunk | 3,420 | 5,463 |
| Avg output tokens/chunk | 448 | 722 |
| Cost per chunk | $0.0130 | $0.0209 |
| **Estimated full corpus** | **$14.43** | **$23.13** |

The +60% buys the quality changes tabulated in ADR-0008 — `RiskCategory` mistyping
eliminated, rights consistently typed, penalty amounts captured, false exemptions
gone, article granularity at 100%, `REFERENCES` density up 4×.

**Where the money goes now.** Input is 65% of the run ($15.13 of $23.13) and ~96%
of input is the fixed system prompt, resent 1,108 times. Two levers could touch
that, and **neither is available**:

- *Prompt caching* — does not exist for Command A on Cohere's API. Checked and
  closed; see Open questions below.
- *Batching several chunks per call* — would amortise the prompt and roughly halve
  the run, and is declined on purpose: it muddies `source_chunk_id`, which citation
  validation depends on. **Attribution integrity was chosen over roughly half the
  run cost.**

So the $15.13 of input is structural. The only remaining lever is the length of the
system prompt itself, and the few-shot examples are 47% of it.

### New integrity checks

`extract.py` now reports four things per run that nothing measured before. On the
v3 re-run across 27 chunks: **0 dangling refs, 0 bare self-article names, ~1
endpoint violation per 13 chunks, and 18 orphan entities.**

`orphan_entities()` is the one that matters most — it is the mirror of
`dangling_refs()`, which only ever looked for edges with no entity. Nothing looked
for entities with no edge, and `aia-art99-para4` had nine cited Articles declared
and unconnected while every check passed clean.

---

## Run 4 — the full corpus

Took four attempts: one crash (an uncaught API error at chunk 215) and two
external kills at chunks 906 and 1012. **No work was lost in any of them** — the
disk cache preserved every paid call and, after the first crash, `flush()`
preserved the output file. See `docs/failure-notes.md` for the RCA.

| Metric | Value |
|---|---|
| Corpus chunks | 1108 |
| **Extracted** | **1107 (99.9%)** |
| **Validation failure rate** | **0.09%** (1 chunk) |
| Entities | 7,466 |
| Relationships | 6,767 |
| Estimated cost | $23.13 |
| **Actual cost** | **≈ $24** |
| Avg tokens/chunk (final pass) | 5,598 in + 743 out |
| `source_chunk_id` repairs | 32 |
| Transport retries | 0 |

The estimate held to within ~4%, which retroactively validates the Run 3b
representative sample as the right basis. Zero transport retries: sequential
extraction runs at ~10 calls/min, well under any rate limit.

### Ontology v3 at full scale

Every one of the 12 entity types and all 13 relationship types is used.

Regenerated from `python -m src.ingest.audit --json` on 2026-07-30. An earlier
version of this table was transcribed by hand mid-run and undercounted by 110
entities and 142 relationships against its own headline totals — read these off
the tool, do not retype them.

| Entity type | Uses | Distinct names |
|---|---|---|
| Article | 1998 | 1169 |
| Obligation | 1258 | **1182** |
| DefinedTerm | 1206 | 549 |
| ActorRole | 966 | **105** |
| Authority | 881 | 101 |
| SystemType | 334 | 73 |
| Regulation | 219 | 74 |
| Annex | 195 | 13 |
| RiskCategory | 148 | **4** |
| LawfulBasis | 145 | 136 |
| Right | 104 | 68 |
| Penalty | 12 | 11 |

Three things to read off this table:

- **`RiskCategory` is 4 distinct values across 148 uses.** Before v3 it was 6
  values of which 5 were not risk categories. The junk-drawer problem is gone.
- **`Obligation` is 94% unique** (1182 distinct of 1258). Obligations are phrased
  per-chunk and barely repeat, so entity resolution has almost nothing to merge
  there. The compressible types are `ActorRole` (966 → 105) and `Authority`
  (881 → 101). Tune the Phase-1 resolution threshold on those.
- **`bare self-articles: 0 of 1108`** — the v3 granularity rule held perfectly.
  This was ~50/50 under v2 and is the single change most protective of the
  `REFERENCES` backbone.

Relationship counts: `APPLIES_TO` 2649, `IMPOSES` 1253, `REFERENCES` 1164,
`DEFINED_IN` 514, `ENFORCED_BY` 383, `LISTED_IN` 191, `CLASSIFIED_AS` 171,
`PERMITS` 137, `INTERACTS_WITH` 130, `GRANTS` 86, `EXEMPT_FROM` 58,
`PENALIZED_UNDER` 19, `SETS_PENALTY` 12.

`INTERACTS_WITH` at 130 resolves the worry raised in Run 3 — the cross-regulation
bridge exists. `PENALIZED_UNDER` (19) and `SETS_PENALTY` (12) are thin because
penalties genuinely live in few articles, but the `enforcement_chain` Cypher
template depends on them, so confirm in Phase 1 Step 4 rather than assume.

> **Confirmed in Step 4, and the count was not the thing to check.** `enforcement_chain`
> works: 216 obligations carry `ENFORCED_BY` → `Authority`, and its `PENALIZED_UNDER`
> leg is an `OPTIONAL MATCH`, so the thinness degrades the answer rather than emptying
> the query (only 4 obligations have both). **`cross_regulation` was the one that
> failed, and its edge count was never the problem** — all 130 `INTERACTS_WITH` edges
> point at a *Regulation*, never at an Article, while the template required
> `Article↔Article`. A healthy-looking count in this table said nothing about whether
> the shape matched. See `docs/failure-notes.md`.

### Integrity at corpus scale

| Check | Count | Rate |
|---|---|---|
| Dangling head/tail refs | 54 | 0.8% of edges |
| Orphan entities (no edge) | 628 | 8.4% of entities |
| Endpoint violations | 357 | 5.3% of edges |
| Bare self-articles | 0 | 0% |
| Type collisions (name under 2+ types) | 66 | — |

All detected, none silently dropped. Run `python -m src.ingest.audit` to
regenerate; re-run it after entity resolution to measure what actually merged.

**The 66 type collisions are two distinct problems**, which matters because only
one is already in the Step 3 design:

- *Cross-type* — `AI system` is both `DefinedTerm` and `SystemType`; `Member State`
  spans `ActorRole`, `Authority` and `DefinedTerm`. A side effect of adding
  `DefinedTerm`: Art. 3 defines these terms, so the definition chunk types them one
  way and every other chunk types them another. Needs deterministic retyping, which
  the `entity_resolution.py` stub does not currently contemplate.
- *Case variants* — `Commission`/`commission`, `Board`/`board`, `AI Office`/`ai office`.
  This is `normalize()`'s job and already in scope.

Confidence is now **10 distinct values spanning 0.6–0.96** (0.95 alone is 3435 of
6767). Richer than the pilot's five, still ordinal, still not a probability.

### The one chunk that did not extract

`gdpr-art70-para1` — the corpus's largest at 864 tokens with 33 lettered
sub-points (the EDPB task list). Its JSON exceeds Command A's **hard 8192-token
output ceiling**, so it cannot be extracted in one call at any `max_tokens`
setting. This is a chunking constraint surfacing at extraction; the remedy is to
split oversized paragraphs at the chunker, as `rechunk_definitions.py` already
does for definitions. Deferred because `chunk_id` is now load-bearing across both
stores. The text remains in the vector path.

---

## Extraction quality (first 10 stored rows, ontology v1/v2)

Superseded by Run 3 for cost purposes; retained because the type distribution and
integrity findings below are what motivated ontology v2.

85 entities, 89 relationships. 6 rows are v1 output, 4 are v2.

**Entity types** — all 9 used:

| Type | Count |
|---|---|
| Article | 17 |
| LawfulBasis | 16 |
| SystemType | 10 |
| Obligation | 10 |
| ActorRole | 9 |
| RiskCategory | 8 |
| Authority | 6 |
| Annex | 5 |
| Regulation | 4 |

**Relationship types** — 10 of 11 used:

| Type | Count |
|---|---|
| APPLIES_TO | 32 |
| PERMITS | 16 |
| REFERENCES | 12 |
| IMPOSES | 10 |
| CLASSIFIED_AS | 7 |
| LISTED_IN | 4 |
| INTERACTS_WITH | 3 |
| ENFORCED_BY | 2 |
| DEFINED_IN | 2 |
| EXEMPT_FROM | 1 |
| PENALIZED_UNDER | 0 |

`PENALIZED_UNDER` is unused because none of the 10 sample chunks mentions
penalties. That is correct behaviour, not a gap — but it means the type is
still **untested**. Include a penalty provision (e.g. AIA Art. 99) in the next
sample.

**Integrity checks** — across all 10 rows:

- 0 relationships with a wrong `source_chunk_id`
- 0 dangling `head`/`tail` references (every endpoint resolves to a declared entity)
- 0 invented types (Pydantic `Literal` rejects them; verified directly against a
  fabricated `LawfulBasis`-before-v2 entity and a `PERMITS`-style unknown relation)

**Confidence distribution** — mean 0.896, median 0.90, range 0.75–0.95:

| Value | Count |
|---|---|
| 0.75 | 1 |
| 0.80 | 7 |
| 0.85 | 29 |
| 0.90 | 13 |
| 0.95 | 39 |

Nothing at 1.0, which was the thing to check. But the model emits only five
discrete values and 44% sit at 0.95, so this is **coarse ordinal confidence, not
a calibrated probability**. The ordering is meaningful — explicit statements land
at 0.95, inferences at 0.80–0.85 — so it is usable as a filter at coarse
thresholds. Do not threshold it finely, and do not average it as if it were a
probability.

---

## Findings

### 1. The v1 ontology mis-modelled permissions as obligations

The original 8-type ontology had no way to express "X makes this lawful". Faced
with GDPR Art. 6(1) — *"processing shall be lawful only if at least one of the
following applies"* — the model typed all six lawful bases as `Obligation` and
connected them with `IMPOSES`:

```
IMPOSES  GDPR Art. 6(1) -> obtain consent   (0.95)
```

That is legally wrong, not merely awkward. Art. 6(1) imposes no duty to obtain
consent; consent is one of six *alternative* grounds that make processing lawful.
The v1 graph asserted six simultaneous mandatory duties at 0.95 confidence. The
same distortion hit GDPR Art. 9(2), where the (a)–(j) derogations became 13
spurious `Obligation` entities, plus two invented `ENFORCED_BY` edges pointing at
"Union" and "Member State" typed as `Authority`.

**Root cause:** an ontology with `IMPOSES` but no permissive counterpart forces
the model to express permissions with the only edge available.

### 2. Ontology v2 fixed it — `LawfulBasis` + `PERMITS`

Added a 9th entity type and an 11th relationship type, with an explicit
permission-vs-obligation disambiguation rule and a worked few-shot example in the
system prompt. Results after re-extraction:

| Chunk | v1 | v2 |
|---|---|---|
| `gdpr-art6-para1` | 6 `Obligation` + 6 `IMPOSES` | 6 `LawfulBasis` + 6 `PERMITS`, 0 `IMPOSES` |
| `gdpr-art9-para2` | 13 `Obligation`, 2 spurious `ENFORCED_BY` | 10 `LawfulBasis` matching (a)–(j) exactly, 0 `Obligation`, 0 `ENFORCED_BY` |
| `aia-art9-para1` (control) | 4 `Obligation` + 4 `IMPOSES` | unchanged |

The control matters: a genuine obligation ("a risk management system shall be
established… ") still extracts as `Obligation`/`IMPOSES`. The permissive rule did
not bleed into real duties.

**Still not represented:** the *disjunction*. Art. 6(1) requires **at least one**
basis to hold; the graph shows six independent `PERMITS` edges with no way to
express "any one of these suffices". Acceptable for retrieval, wrong for anything
resembling compliance reasoning. Revisit if the query layer needs it.

### 3. Foreign instruments needed deterministic normalization

v1 emitted `Directive (EU) 2016/680` and `Regulation (EU) 2018/1725` as
`Regulation` entities with **empty aliases and no short name**, so they could
never resolve against anything else in the graph. v2 adds a small lookup dict
(`FOREIGN_INSTRUMENTS`) applied after parsing, so the same instrument cannot
enter the graph as two nodes:

| Citation | Canonical | Alias |
|---|---|---|
| Regulation (EU) 2016/679 | GDPR | full citation |
| Directive (EU) 2016/680 | LED | full citation |
| Regulation (EU) 2018/1725 | EUDPR | full citation |

Anything not in the dict keeps its full citation as `canonical_name`, so it is at
least resolvable. `head`/`tail` are remapped alongside the rename, or the edges
would be orphaned.

Prompted normalization alone was not trusted here — the dict is applied
deterministically because entity resolution failures are silent and expensive to
debug downstream.

### 4. The cross-regulation bridge works

`aia-art3-def37` ("special categories of personal data") is the test case for
linking the two regulations. It produces `INTERACTS_WITH` and `REFERENCES` edges
into all three foreign instruments, with articles namespaced
(`GDPR Art. 9(1)`, `LED Art. 10`, `EUDPR Art. 10(1)`). Article numbers collide
across regulations, so the namespacing is load-bearing, not cosmetic.

---

## Open questions

- **Is $2.50/$10.00 per 1M still current Command A pricing?** Everything above
  depends on it.
- **Is the v3 prompt worth its length?** It grew ~2,040 tokens for the ontology v3
  additions. The five few-shot examples are ~2,530 tokens — **47% of the system
  prompt and $7.01 of the run** — with Example 5 alone at ~940 tokens / $2.60.
  Trimming the examples is now the *only* lever on input cost (see closed question
  below), and it is a weak one: a realistic trim saves ~$2.25, and every problem
  Step 0 found was caused by the prompt saying too little, not too much.
- **Penalty handling is inconsistent between regulations.** `aia-art99-para4` emits
  7 `PENALIZED_UNDER` plus `SETS_PENALTY`; `gdpr-art83-para5` emits `SETS_PENALTY`
  but zero `PENALIZED_UNDER`, having produced 6 under v2. Needs one more look.
- **Confidence is coarse.** Decide whether five discrete values are enough to
  filter on before relying on it in retrieval.
- **Disjunction is unmodelled** (see finding 2).
- ~~**`INTERACTS_WITH` is sparse** — 3 edges across 28 chunks. It is the
  cross-regulation bridge every Phase 5 cross-reg question traverses. Count it
  corpus-wide in the post-run audit before assuming the bridge exists.~~
  **Answered: 130 edges corpus-wide.** But counting it was the wrong check — see the
  Step 4 note above. The bridge exists and points somewhere the query did not look.

**Closed by Run 3:** `PENALIZED_UNDER` untested (3a); sample bias in the
cost-per-chunk figure (3b).

**Closed — prompt caching is not available.** Command A does not support prompt
caching on Cohere's API, checked 2026-07-29:

- no `cache_control`-style parameter in the v2 Chat API;
- no cached-token line in Cohere's pricing — the billing model is input/output
  tokens only, and the `billed_units` distinction covers tokens Cohere adds under
  the hood, not caching;
- nothing in the changelog through the Command A+ release (May 2026).

This matters more than a normal closed question: ~96% of input tokens are the same
system prompt sent 1,108 times, and input is 65% of the run. **That $15.13 is
structurally unavoidable on this architecture.** The two levers that would have
touched it are both gone — caching does not exist, and batching was declined on
attribution-integrity grounds (see Run 3c). What remains is trimming the few-shot
examples, worth ~$2.25 realistically.

Worth restating plainly in the README's cost section: the honest framing is not
"we optimised extraction cost" but "we measured it, found the one big lever
unavailable and the other unacceptable, and paid the $23."

---

## Reproducing

```bash
python -m src.ingest.extract                      # the 10 test chunks + cost report
python -m src.ingest.extract --chunk-id <id> ...  # targeted re-run
python -m src.ingest.extract --all                # full corpus — run 2026-07-29, 1107/1108
python -m src.ingest.audit                        # the numbers in this document
python -m src.ingest.audit --json                 # regenerate the tables above
```

Responses are cached on disk under `data/cache/extraction/`, keyed by a hash of
the chunk text **and** the model and system prompt. Re-running after a code fix
is free; editing the prompt correctly invalidates the cache and re-calls. Output
is upserted by `chunk_id`, so a targeted re-run does not delete untouched rows.

Outputs (all gitignored under `data/`):
`data/processed/extractions.jsonl`, `data/processed/failures.jsonl`.
