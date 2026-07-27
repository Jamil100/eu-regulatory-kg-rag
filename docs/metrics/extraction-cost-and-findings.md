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

Status: **extraction validated on 10 chunks, full corpus not yet run.**

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

## Current extraction quality (all 10 stored rows)

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
- **Prompt caching** — ~87% of input tokens are a constant system prompt. Worth
  checking whether Cohere supports caching it for Command A.
- **`PENALIZED_UNDER` is untested.** Add a penalty provision to the sample.
- **Confidence is coarse.** Decide whether five discrete values are enough to
  filter on before relying on it in retrieval.
- **Disjunction is unmodelled** (see finding 2).
- **Sample bias** — the 10 chunks are deliberately hard. A random 10 would give a
  more honest cost-per-chunk, at the cost of one more paid run.

---

## Reproducing

```bash
python -m src.ingest.extract                      # the 10 test chunks + cost report
python -m src.ingest.extract --chunk-id <id> ...  # targeted re-run
python -m src.ingest.extract --all                # full corpus — not yet run
```

Responses are cached on disk under `data/cache/extraction/`, keyed by a hash of
the chunk text **and** the model and system prompt. Re-running after a code fix
is free; editing the prompt correctly invalidates the cache and re-calls. Output
is upserted by `chunk_id`, so a targeted re-run does not delete untouched rows.

Outputs (all gitignored under `data/`):
`data/processed/extractions.jsonl`, `data/processed/failures.jsonl`.
