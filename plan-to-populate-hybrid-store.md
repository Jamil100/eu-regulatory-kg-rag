# Next steps: from a 10-chunk pilot to a populated hybrid store

**Status:** ~~revised plan, incorporating pre-flight probes, retry hardening, and audit additions.~~
~~**Step 0 complete (2026-07-28).** Next action is Step 1, pending the cost decision at the end of Step 0.~~
~~**Steps 0–4 complete (2026-07-30).** The Neo4j graph exists: 3,366 nodes, 6,658 relationships,
idempotent load, all six Cypher templates returning rows. **Next action is Step 5** (vector index),
starting with the `schemas.py` dependency inversion, plus the eval question set on the parallel track.~~
**Steps 0–5 complete (2026-07-31). The exit criterion is met** — roadmap weeks 1–2 done: a populated
Neo4j graph (3,366 nodes / 6,680 edges) *and* a populated pgvector index (1,108 chunks, both
dimension arms, recall measured, ADR-0004 accepted).

**Next action is Phase 3** — `src/query/` and `src/answer/`, which are still stubs. Two things are
now waiting on it specifically: no Cypher template projects a relationship, so `source_chunk_id` is
unreachable from any query; and vector retrieval scores **30% on two-hop and 47% on aggregation**
questions, which is the gap the graph path exists to close and the baseline Phase 5 will be measured
against. The eval question set (23 of a target 100) remains the parallel track.

## Context

Ingestion and extraction code are done and well-debugged. The corpus is 1,108 chunks (694 AI Act +
414 GDPR) in `data/processed/chunks-ai-act.jsonl` and `chunks-gdpr.jsonl`. But
`data/processed/extractions.jsonl` holds only 10 rows — the hand-picked `TEST_CHUNK_IDS` pilot in
`src/ingest/extract.py:55`. Everything downstream of extraction (`entity_resolution.py`,
`graph_writer.py`, `index/embedder.py`, all of `query/` and `answer/`) is a stub raising
`NotImplementedError`.

The graph does not exist yet. Nothing after Phase 1 can be built or measured until it does.

**Why there is a Step 0 before the run.** `cache_key()` (`extract.py:257`) hashes
`MODEL + SYSTEM_PROMPT + chunk_text`. Editing the system prompt invalidates all 1,108 cache entries,
so any ontology fix discovered *after* the full run costs a second full run. Two probes costing
~$0.20 buy insurance on a ~$15 purchase that is awkward to redo. This is also the exact failure
pattern `docs/failure-notes.md` names as the common thread: *"I confirmed the part I was looking at
and assumed it covered the whole."*

~~**Recomputed cost** (from the `token_count` field already in the chunk files — corpus sum 79,774~~
~~tokens, mean 72, median 50; implied fixed prompt overhead ~3,040 tokens/call including v2 additions):~~

| ~~Output assumption~~ | ~~Full-run cost~~ |
|---|---|
| ~~Scaled linearly with chunk size (390 tok/chunk)~~ | ~~$12.94~~ |
| ~~Half-scaled (602 tok/chunk)~~ | ~~$15.29~~ |
| ~~Pilot rate (813 tok/chunk, worst case)~~ | ~~$17.63~~ |

~~$16.66 is a fair upper bound.~~ Note **3.37M of 3.45M input tokens (97.7%) are the system prompt
resent 1,108 times** — ~~input alone is ~$8.60 of the run.~~

> **Superseded by measurement (Step 0b/0c).** These were projections. The measured figure on a
> representative sample is **$14.43 under ontology v2** and **$23.13 under ontology v3** — input
> $15.13, output $8.00. Input is 65% of the run and ~96% of it is the fixed prompt, but that cost is
> **structural**: prompt caching does not exist for Command A, and batching was declined to protect
> `source_chunk_id`. See the cost decision at the end of Step 0.

**Cost lever consciously declined:** because input is 97.7% repeated system prompt, batching 3–4
chunks per call would amortize it and roughly halve the run cost. Not doing it — batching muddies
`source_chunk_id` attribution, which is load-bearing for citation validation, and risks
cross-contamination between chunks in a single completion. Recording the trade-off explicitly in
`docs/metrics/extraction-cost-and-findings.md`: **attribution integrity was chosen over ~$7.**

---

## ~~Step 0 — Two cheap probes before paying (~$0.20, ~1 hour)~~ ✅ DONE — actual $0.93

### ~~0a. Exercise `PENALIZED_UNDER`~~ ✅

~~It is the only relationship type with 0 uses across all 10 pilot rows~~
~~(`docs/metrics/extraction-cost-and-findings.md`), because no sample chunk mentions penalties.~~
~~**Untested type = possible ontology hole of exactly the `LawfulBasis` shape** — same risk profile,~~
~~same cause (no sample exercised it).~~

~~Penalty chunks confirmed present: `aia-art99-para1` … `aia-art99-para8`, and `gdpr-art83-para1` …~~
~~`gdpr-art83-para6`.~~

```
python -m src.ingest.extract --chunk-id aia-art99-para1 --chunk-id aia-art99-para4 --chunk-id gdpr-art83-para5
```

~~`--chunk-id` is `action="append"` (`extract.py:540`) and `write_jsonl` upserts by `chunk_id`, so~~
~~this adds rows without disturbing the existing 10.~~

~~Read the output. Confirm `PENALIZED_UNDER` edges appear with `Obligation` head → `Article` tail. If~~
~~the fines instead land as `Obligation` + `IMPOSES`, or an `Authority` gets an invented edge, that is~~
~~a prompt fix — far cheaper now than after the full run.~~

~~Then add these ids to `TEST_CHUNK_IDS` so the pilot permanently covers the type.~~ Done —
`TEST_CHUNK_IDS` is now 14 ids, annotated by legal function.

### ~~0b. Random-sample probe on typical text~~ ✅

~~Pilot chunks average **2.08× the corpus mean length** (150 vs 72 tokens) and were deliberately chosen~~
~~to be hard. The corpus median is 50 tokens — **half the corpus is short procedural and definitional~~
~~text whose extraction behaviour is entirely unmeasured.**~~

~~Draw ~15 chunks with a seeded random sample across both regulations, run them, read the output.~~
~~Check two things:~~

- ~~**Cost calibration** — an honest per-chunk figure replacing the acknowledged sample bias (already~~
  ~~flagged under "Open questions" in the metrics doc).~~
- ~~**Quality on short text** — does a 30-token chunk produce a sane 2–3 entity extraction, or does it~~
  ~~over-extract and invent structure the text does not state? Rule 1 of the system prompt forbids~~
  ~~adding outside knowledge; short chunks are where that pressure is highest.~~

~~A one-off script in the scratchpad is fine; no need for a permanent `--sample` flag.~~

### ~~0c. Record the result~~ ✅

~~Append a "Run 3 — pre-flight probes" section to `docs/metrics/extraction-cost-and-findings.md` with~~
~~the measured per-chunk cost and whether `PENALIZED_UNDER` fired. **If either probe forces a prompt~~
~~change, re-run 0a/0b before proceeding** — the point is to make prompt edits while they are cheap.~~
Both probes forced a prompt change, so both were re-run under v3 as required.

---

## Step 0 — outcome

**What was done.** Ran the two probes (3 penalty chunks, then a seeded random 15-chunk sample at
0.91× corpus mean length), read the output rather than the aggregate metric, found four problems,
fixed them as ontology v3, and re-ran both probes to verify. 28 chunks are now extracted and stored.
Recorded in `docs/adr/adr-0008-definedterm-right-penalty.md`, a "Run 3" section in the metrics doc,
and a new failure-notes entry. Test suite went 8 → 18.

**Why it was necessary.** `cache_key()` hashes the system prompt, so any ontology fix found *after*
the full run invalidates all 1,108 cached responses and costs a second full run. Probing first is
arithmetic, not caution — and three of the four problems were only visible by reading output that
every automated check had already passed clean.

**Findings.**

- **`PENALIZED_UNDER` works** — 7 edges on `aia-art99-para4`, 6 on `gdpr-art83-para5`, correctly
  typed. The one untested relationship type is now exercised and permanently in the pilot set.
- **`RiskCategory` was a junk drawer.** Of 6 distinct values, only `high-risk` was a real risk
  grading; the rest were defined terms (`making available on the market`, `biometric data`). The
  system prompt's **own Example 2 had been teaching this since v1**. AIA Art. 3 alone is 94
  definition chunks.
- **The same right was modelled two ways one paragraph apart** — `Obligation` in
  `gdpr-art21-para2`, `LawfulBasis` in `-para5`. Entity resolution compares within a type, so the
  fragmentation would have been permanent and invisible. GDPR Ch. III is ~80 rights chunks.
- **Two legally false facts passed validation**, including `EXEMPT_FROM: AIA Art. 5 → AIA Art. 99`
  at 0.90 — asserting the AI Act's most severely punished provision carries no penalty. `Literal`
  validates the type *string*; it cannot see that an edge's ends are the wrong kind of thing.
- **`dangling_refs()` had a blind spot.** It finds edges with no entity. Nothing found entities with
  no edge, so `aia-art99-para4` declaring nine cited Articles and connecting none of them reported
  clean. `orphan_entities()` is now its mirror.
- **Article names were sub-numbered ~50% of the time**, from metadata that was passed to the model
  and ignored. Bare and sub-numbered names become two nodes, severing cross-references.

**Fixed (ontology v3):** 12 entity types (+`DefinedTerm`, `Right`, `Penalty`), 13 relationships
(+`GRANTS`, `SETS_PENALTY`), plus `ALLOWED_ENDPOINTS` endpoint checking, `orphan_entities()`, and an
article-granularity rule. Measured after: `RiskCategory` mistyping **eliminated**, both Art. 21
paragraphs now `Right`+`GRANTS`, penalty amounts captured with magnitude intact, false
`EXEMPT_FROM` **0**, bare article names **0 of 27**, `REFERENCES` density 12 → 50. The
`aia-art9-para1` control (a genuine duty) is **unchanged** — the new types did not bleed into real
obligations.

**Two estimates I got wrong.** Step 0 cost **$0.93, not ~$0.20** (both probes ran twice). And the
full-run estimate moved **$14.43 → $23.13 (+60%)**, against the ~+$1.70 quoted when the thorough
ontology was chosen — the v3 prompt is ~2,040 tokens longer and the model emits more per chunk.

**Still open, carried into Step 1:** `gdpr-art83-para5` gained its `Penalty` but lost 6
`PENALIZED_UNDER` edges (the AIA equivalent kept both); `INTERACTS_WITH` is at 3 edges across 28
chunks, thin for the cross-regulation questions that depend on it; endpoint violations persist at
~1 per 13 chunks (detected, not prevented, on purpose).

**Cost decision — resolved.** Prompt caching was the one lever that could have touched the $15.13 of
input cost, and **Command A does not support it on Cohere's API** (verified 2026-07-29: no
`cache_control`-style parameter in the v2 Chat API, no cached-token line in pricing, nothing in the
changelog through the Command A+ release in May 2026). Batching was already declined to protect
`source_chunk_id` attribution. Trimming the few-shot examples — 47% of the prompt, $7.01 — is the
only lever left, and realistically recovers ~$2.25 while risking the constructs Step 0 just paid to
validate. **Accept $23.13 and run.** Every problem Step 0 found came from the prompt saying too
little; shortening it to save 10% is the wrong direction.

---

## ~~Step 1 — The full extraction run~~ ✅ DONE — 1107/1108 (99.9%), ≈$24

> **Outcome.** Took four attempts: an uncaught API error killed the run at chunk 215, then two
> external kills at 906 and 1012. No work lost — the disk cache preserved every paid call, and the
> newly-added `flush()` preserved the output file (893 rows survived the second kill, versus 29 after
> the first crash). Final: **1107 chunks, 7,466 entities, 6,767 relationships, 0.09% validation
> failure rate.** Estimate held to within ~4%.
>
> **Fixes forced by the run:** API errors caught per-chunk rather than killing the run; incremental
> flush every 25 chunks; `MAX_TOKENS` 4096 → 8192 (Command A's hard ceiling — 16384 and 32768 both
> HTTP 400). Added `src/ingest/audit.py` for the Step 2 numbers.
>
> **`gdpr-art70-para1` did not extract** — 864 tokens, 33 sub-points, JSON exceeds 8192 output
> tokens at any setting. A chunking constraint, not a tuning one. Text remains in the vector path.
>
> **Two `OPEN` items** in `docs/failure-notes.md`: `failures.jsonl` detail is destroyed by the next
> `--all`, and oversized paragraphs need splitting at the chunker.

### ~~1a. Harden `call_model` first (do not skip)~~ ✅

`call_model` (`extract.py:290`) calls `client.chat` with no retry wrapper. `tenacity` is already a
declared dependency in `pyproject.toml` and unused here. 1,108 sequential calls will likely hit a
rate limit.

The cache makes a crash resumable, so this is not fatal — but the asymmetry is stark: **~5 lines of
`tenacity` retry with exponential backoff versus repeated manual restarts across a 1.5–3 hour
window.** Wrap it, then start the run and walk away.

Also confirm your Cohere key's actual rate limits before starting rather than assuming — trial keys
cap around 1,000 calls/month and you need 1,108, so a **production key is required**.

### 1b. Run

```
python -m src.ingest.extract --all
```

Expect 1.5–3 hours sequential. Start it and work the eval-set parallel track while it runs.

~~**Note on the six mixed-version rows:** chunks extracted under the v1 prompt had their cache keys~~
~~rotated by the v2 prompt change, so `--all` will re-call the API for them rather than replay stale~~
~~v1 results, and `write_jsonl` upserts by `chunk_id`. No action needed — just expect six "already~~
~~done" chunks to cost money again. This is correct behaviour.~~

> **Superseded.** All 28 stored rows are now v3 and cache-consistent, so `--all` will replay them
> free and pay only for the remaining ~1,080. The mixed-version problem no longer exists.

**Deliverable:** `data/processed/extractions.jsonl` at ~1,108 rows; `failures.jsonl` with the real
Pydantic validation failure rate.

---

## ~~Step 2 — Post-run audit~~ ✅ DONE — every check run, `_TBD_` filled

> **Outcome.** `src/ingest/audit.py` runs every integrity check at corpus scale, read-only and with
> no API calls. Measured: validation failure rate **0.09%** (1 of 1108), dangling refs **54**,
> orphan entities **628 (8.4%)**, endpoint violations **357 (5.3%)**, bare self-articles **0**, type
> collisions **66**, confidence **10 distinct values 0.6–0.96**, `INTERACTS_WITH` **130**. The
> `_TBD_` at `docs/failure-notes.md:12` is filled and the metrics status line is flipped.
>
> **Two corrections made later, during Step 4.** The histograms in the metrics doc had been
> transcribed by hand mid-run and undercounted by 110 entities and 142 relationships against their
> own headline totals — regenerated from `--json`. And **counting `INTERACTS_WITH` was the wrong
> check**: 130 looked like the worry resolved, but every one of those edges ends at a `Regulation`,
> not an `Article`, which is what actually broke `cross_regulation`. No histogram here records an
> endpoint type. That is the Step 2 gap worth remembering.

### ~~Original checklist~~ (all items done)

The pilot's 0% validation failure metric is precisely the one the failure notes call out as *"a 0%
failure rate looked like success and wasn't."* Repeat the integrity checks at corpus scale with a
read-only analysis script:

- **Measured validation failure rate** → fill the `_TBD_` in `docs/failure-notes.md:12`.
- **Entity-type and relationship-type histograms** across all rows. Any type at or near zero is
  either correct behaviour or an ontology hole — decide which, **in writing**.
- **`INTERACTS_WITH` edge count specifically.** This is the bridge Q8 and every cross-regulation
  question depends on. If the corpus yields only a handful, those questions have nothing to traverse
  and the graph path will fail on exactly the questions meant to showcase it.
- ~~**Verify whether the regex pre-seed pass was ever implemented.** `docs/concepts/ontology.md`~~
  ~~records the decision to pattern-match "Regulation (EU) 2016/679" etc. *before* the LLM pass — but~~
  ~~`def37`'s bridges in the v2 test came from the model, not a pre-seed. This may be a decision~~
  ~~recorded and never built. Check rather than assume.~~
  **Checked during Step 0: `docs/concepts/ontology.md` does not exist** — not in the worktree and
  never in git history. The dangling reference comes from `adr-0007`, which claims to supersede it.
  There is no recorded pre-seed decision to verify. What *does* exist is the `FOREIGN_INSTRUMENTS`
  dict applied deterministically in `normalize_instruments()`, which is the equivalent control. The
  remaining question is unchanged and still worth answering: is the model's organic bridging enough?
  `INTERACTS_WITH` sat at 3 edges across 28 chunks, so count it corpus-wide before assuming it is.
- **Dangling head/tail refs.** `dangling_refs()` (`extract.py:488`) already exists per extraction;
  aggregate across the corpus. These become orphan nodes at graph-load time.
- **Distinct `canonical_name` count per entity type.** Direct input to Step 3 — it quantifies how
  much resolution work there actually is.
- **Confidence distribution** — confirm it is still the coarse 5-value ordinal the pilot showed.

Update `docs/metrics/extraction-cost-and-findings.md` with real run figures and flip its status line
off "full corpus not yet run."

---

## ~~Step 3 — Entity resolution~~ ✅ DONE — 3,485 → 3,366 nodes; ADR-0009

> **Outcome.** Four deterministic stages (normalize → attested-plural folding → type reconciliation →
> exact match) reduce **3,485 (type, name) nodes to 3,366**. Compression lands where it should:
> **ActorRole −26%, Authority −15%, DefinedTerm −65 nodes**. Article and Obligation are ~70% of nodes
> and neither is compressible, which is why the headline figure is only 3.4%.
>
> **The plan's design was missing a stage.** Resolution compares within a type, so the 66 cross-type
> collisions were invisible to it. Type reconciliation had to run *after* case-folding: alone,
> `Member State` → Authority and `member state` → ActorRole, so reconciling first would freeze one
> concept into two permanently unmergeable types.
>
> **The threshold could not be tuned, because the classes do not separate.** 25 pairs hand-labelled
> from the regulations: `supervisory authority`/`lead supervisory authority` (must not merge) scores
> **0.753**, while `data protection officer`/`dpo` (must merge) scores **0.423**. Legal modifiers
> create new entities; embeddings read them as synonyms. At 0.90 — **0 false merges, 7 of 10 missed**,
> and every miss is a class a deterministic rule handles better.
>
> **The embedding pass over all 236 role-like nodes returned exactly one candidate, and it was a
> false merge**: `real time remote biometric identification system` ~ `remote biometric
> identification system` (0.914), which AIA Art. 5(1)(h) prohibits vs Annex III merely high-risk.
> Embeddings are therefore **candidates-for-review only** (`--embed`), never auto-applied.
>
> Output: `data/processed/resolved-entities.json`. Alias-based merging left unapplied — 88 real
> candidates, but the same list contains `law enforcement agency` ← `law enforcement purposes`.

### ~~Original plan~~ (superseded by ADR-0009)

The stub already fixes the design: normalize → exact match → Embed v4 cosine ≥
`SIMILARITY_THRESHOLD` (0.90) → merge or create.

- **`normalize()`** — lowercase, strip punctuation, singularize role names. Reuse the existing
  `FOREIGN_INSTRUMENTS` dict (`extract.py:71`) as the abbreviation-expansion seed; do **not** build a
  second mapping. It is already applied deterministically at parse time, so the graph should never
  see two nodes for GDPR/LED/EUDPR.
- **`resolve()`** — embed with `embed-v4.0` via `settings.model_embed`, compare **within entity type
  only** (never merge an `Obligation` into a `SystemType`).
- **Tune the threshold on ~30 hand-labeled pairs drawn from the Step 2 output**, not from
  imagination. Expect the roadmap's stated hard cases: `deployer` / `deployers` / `the deployer
  referred to in Article 26(1)`.
- **Record the tuning result as an ADR.**  `docs/adr/` currently has no decision record for it.

---

## ~~Step 4 — Graph writer~~ ✅ DONE — 3,366 nodes / 6,658 edges, idempotent, 46 tests

> **Outcome.** `src/ingest/graph_writer.py` loads the graph and re-running is a verified no-op.
> 12 labels + a shared `:Entity` label, 13 relationship types, per-label uniqueness constraints
> (Neo4j 5 Community has property uniqueness; `IS NODE KEY` is Enterprise). Suite 18 → 48.
> Full numbers in **`docs/metrics/graph-load.md`**.
>
> **Load cost: 2.7–3.1s warm, 9.4s cold** (median of 3 each). The graph write itself is 1.7s — 0.18s
> derivation + 1.5s of Bolt writes; the cold run's extra ~6s is JVM warmup and query-plan caching on
> Neo4j's side. An earlier draft of this note quoted "~10 seconds" from one cold measurement, which
> described the cost of *starting* Neo4j as though it were the cost of loading.
>
> **`build_graph()` is pure**, so every reported count is asserted without a database — the DB-backed
> tests skip cleanly when Bolt is unreachable rather than reddening CI.
>
> **The resolved JSON was not enough.** It holds nodes only, and edges carry *raw* head/tail strings;
> joining by raw name matches ~48% of endpoints, versus **98.4% (6,660 of 6,767)** through
> `resolve_corpus()["key"]`. The loader imports the resolver rather than reading the file.
>
> **Three of the six templates were wrong** — `cross_regulation` returned zero rows,
> `obligations_for_system` was a cartesian product, and `definition_of` pinned `:Article` on a tail
> that also accepts `:Annex`, silently missing 48 of 337 defined terms. Per-chunk edge provenance also
> inflated `obligations_for_system` to 24,428 rows until `RETURN DISTINCT`. Full write-up in
> `docs/failure-notes.md`.
>
> **A prerequisite the plan found only by checking:** `normalize()` was stripping the closing paren
> off every sub-numbered article, so 1,026 of 3,366 node keys read `aia art. 1(1`. Fixed and proved
> merge-neutral before re-applying; ADR-0009 has a Correction section. Nodes also gained
> `display_name` for prose (`high-risk`, not `high risk`).
>
> **Environment note:** Docker runs inside WSL2, not Docker Desktop, so compose must be invoked from
> a WSL shell. WSL2 localhost-forwarding does carry Bolt to Windows Python — `bolt://localhost:7687`
> works unchanged, no `.env` entry needed. **But WSL terminates the distro when no session is open,
> which stops the container**; keep a session alive or expect to `docker compose up -d neo4j` again.
>
> **`OPEN`:** no template projects a relationship, so `source_chunk_id` — the pgvector join key and
> the citation provenance — is not reachable from any query yet. Phase 3 must fix it.

### ~~Original plan~~ (executed, with the additions above)

```
docker compose up -d       # neo4j + pgvector, already configured
```

- **`MERGE` on `(type, canonical_name)`, never `CREATE`.** Every relationship carries
  `source_chunk_id` and `confidence` as properties — `chunk_id` is the join key to pgvector and is
  load-bearing.
- **Idempotency is a testable property, not a hope:** run the loader twice, assert node and
  relationship counts are identical.
- **Sanity-check against `src/query/cypher_templates.py`** — all six templates are already written
  and reference exact label names (`ActorRole`, `Obligation`, `Article`, `SystemType`,
  `RiskCategory`, `Authority`). Run each by hand in Neo4j Browser after loading. **A template
  returning zero rows means the graph shape does not match what Phase 3 assumes** — you want to know
  that now, not in week 3.

**Done when:** Neo4j Browser shows a connected graph, `obligations_for_role('deployer')` returns
sensible rows by eye, and re-running ingestion is a no-op.

---

## ~~Step 5 — Vector index (Phase 2)~~ ✅ DONE — 1,108 chunks, both arms, ADR-0004 accepted

> **Outcome.** 1,108 of 1,108 chunks embedded at 1536 **and** 512 dimensions and loaded into
> pgvector, idempotent (re-run → identical row count and content hash). `entity_ids` populated:
> **7,465 references across 1,107 chunks, 0 dangling** against the graph. Suite 63 → 74 (+ 13 Neo4j
> skips). Full numbers in **`docs/metrics/vector-index.md`**.
>
> **Total API cost: $0.024.** Extraction was ~$24 — the embeddings are three orders of magnitude
> cheaper, which is worth stating because the intuition runs the other way.
>
> **ADR-0004 resolved, but not by the rule it wrote.** 1536 scores 29/51 gold references, 512 scores
> 28/51. The rule ("adopt 512 if it loses <2% recall") passes at 1.96% — and it should not be read as
> a result: **the entire difference is one gold chunk in one query.** 512 was adopted on the
> unambiguous numbers instead: **3.0× smaller index** (2.9 vs 8.8 MB) and **~8× lower latency**
> (6.7 vs 53.4 ms p50).
>
> **The 8× is bigger than the 3× dimension ratio because the vectors are TOASTed** — `storage=e`,
> heap 1.7 MB against 12 MB of TOAST, so every comparison pays an out-of-line fetch.
>
> **HNSW earns nothing at 1,108 rows and the sweep it was built for has no signal.** The planner
> picks a Seq Scan every time (~6 ms exhaustive) and is right to. Forced onto the index, recall is
> *identical* at ef_search 40/100/200 and latency is *worse*. So every recall figure here is exact
> search — the ceiling belongs to the embedding, not the index. Left on the planner's default the
> sweep compares three identical plans and prints a flat line that reads like tuning.
>
> **The number that matters is per-stratum**, and it is the project's own thesis measured before the
> graph path exists: hard-negative 100%, single-hop 75%, cross-regulation 70%, three-hop 67%,
> **aggregation 47%, two-hop 30%**. Vector search handles one-paragraph questions and collapses on
> multi-hop — exactly ADR-0001's argument for a graph. Phase 5 now has a measured baseline to beat
> rather than an assumed one.
>
> **All three Phase-2 defects closed, and a fourth found while closing them.** `Chunk` rejected
> **1,000 of 1,108** rows, not the 586 of 694 recorded — that figure had counted the AI Act file
> only and all 414 GDPR rows failed too. `schema.sql` rewritten to 11 typed columns. `entity_ids`
> populated. And the uniqueness assertion on `citation_label` caught **`section` being computed by
> the annex parser, used to build the chunk_id, and then dropped from the record** — 25 chunks
> sharing 11 labels, so `AIA Annex VIII(1)` named the registration duties of three different actors.
> Backfilled by re-deriving from source HTML with every other field asserted byte-identical
> (0 mismatches); no re-extraction needed. Write-up in `docs/failure-notes.md`.
>
> **`OPEN`:** the extractor was never told the section either (`user_prompt()`'s key tuple has no
> `section`), so the graph's Annex VIII nodes carry the same ambiguity the labels did — ~$0.70 of
> re-extraction to fix. And `embedding_1536` is retained without a job, pending an eval set large
> enough to resolve a 2% difference.

### ~~Original plan~~ (executed, with the additions above)

**Prerequisite (do first):** invert the `schemas.py` dependency. It currently re-exports from
`extract.py`, which pulls `cohere` transitively into `src/api/app.py`. Move the ontology block into
`schemas.py` and have `extract.py` import it. ~10 minutes now; after `embedder.py` and the `query/`
modules start importing schemas, it is a much wider change.

- **`src/index/embedder.py`** — embed all 1,108 chunks with `embed-v4.0`,
  `input_type="search_document"`, load into pgvector.
- **`src/index/schema.sql` needs a look first.** It has `article` and `paragraph` columns only.
  Annex chunks (108) and Art. 3 definition chunks carry `annex`/`annex_title`/`point`/`definition`
  keys instead — see the key list `user_prompt()` iterates (`extract.py:246`). Decide on a generic
  metadata column or add the missing ones before loading, **or annex chunks lose their provenance in
  the vector store.**
- **Recall harness + the 1536 vs Matryoshka-512 experiment.** This settles
  `docs/adr/adr-0004-embedding-dimension-choice.md`, still marked *Proposed, pending measurement*.
  It needs the ~20 labeled queries from the parallel track — which is why that track starts now.

---

## Parallel track — the eval question set (start during Step 1)

~~**This is smaller than it looks.** Ten fully verified questions with gold answers, precise~~
~~paragraph-level sources, hop counts, and grading rules already exist in `eval-questions.md` (Q1–Q10,~~
~~all source-checked, including the refusal case and the optional hard-negative). The repo's~~
~~`eval/questions.jsonl` having 6 rows with 5 empty golds means **those were never transferred**.~~

~~So the work is **transfer 10, then expand** — not write 50 from scratch.~~

> **Checked during Step 0: `eval-questions.md` does not exist** — not in the worktree, never in git
> history. There are no ten verified questions to transfer. This is **write ~50 from scratch**, the
> ~2 evenings the roadmap calls non-negotiable. Plan accordingly; this was the single largest
> underestimate in the plan.

The global grading rule is still worth adopting as stated: *an answer counts as `correct` only if it
cites a source paragraph that was actually retrieved.*

~~Expand toward the roadmap's stratification (§5.3): 20 single-hop, 20 two-hop, 15 three-hop,
15 cross-regulation, 10 aggregation, 10 out-of-scope-must-refuse. The 6 rows currently in
`eval/questions.jsonl` cover one example of each shape, so they are a template — but 5 of the 6 have
an empty `gold` and need writing too.~~

> **Superseded (2026-07-31).** `eval/questions.jsonl` is gone; the set is now
> **`eval/eval-questions.jsonl`, 23 hand-written rows** carrying `source_chunk_ids` — the gold-passage
> field the old file never had, and the one that unblocks the ADR-0004 recall measurement (21 rows are
> scoreable). §5.3 is restated as 8 strata totalling 100: single-hop 20, two-hop 20, three-hop 15,
> cross-regulation 15, aggregation 10, out-of-scope 5, unanswerable 5, hard-negative 10.

Two notes:

- **Write gold answers from the actual text.** This is the manual work the roadmap calls
  non-negotiable, and it cannot be delegated to the model without destroying the benchmark's
  meaning. (The `eval-questions.md` history is the cautionary case: a drafted-from-memory Art. 26
  answer wrongly included the FRIA, caught only by reading the source.)
- **Draft during Step 1, sharpen after Step 4** — once you can browse the real graph you will see
  which multi-hop paths actually exist.

---

## Housekeeping (batch into any commit above)

- `README.md:19` links to `docs/roadmap.md`; the file is `docs/kg-rag-eu-ai-act-roadmap.md`.
- `README.md:43-45` setup commands are stale: `src.ingest.parser` → `src.ingest.chunker`,
  `src.ingest.extractor` → `src.ingest.extract`, and `src.index.embedder` currently exits with a TODO.
- `chunker.main()` writes a hardcoded `data/processed/chunks.jsonl`, renamed by hand after each run
  — derive the output name from the input instead.
- `extract.py:46` reads `os.getenv("MODEL_EXTRACT")` directly rather than importing `settings` from
  `src/config.py`.
- **`schemas.py` dependency inversion** — listed as a Step 5 prerequisite above; can land earlier.
- Two OPEN items in `docs/failure-notes.md` are now cheap and worth closing: a behavioural extractor
  test (assert `gdpr-art6-para1` yields zero `Obligation`, and the `aia-art9-para1` control still
  yields one) and per-annex count fixtures. Both run off the existing `data/cache/extraction/`
  responses at **zero API cost**. — **Still open.** Step 0 added endpoint/orphan checks and v3 schema
  tests (suite 8 → 18), but neither behavioural test was written. The `aia-art9-para1` control was
  verified *by hand* during the v3 re-run, which is exactly the "doing something once by hand is not
  `DONE`" case the failure notes call out.

---

## Verification

| Step | How you know it worked |
|---|---|
| ~~0~~ ✅ | ~~`PENALIZED_UNDER` edges present in `extractions.jsonl`; random-sample per-chunk cost recorded in the metrics doc~~ — both confirmed; plus ontology v3, ADR-0008, and new integrity checks |
| 1a | `call_model` wrapped in `tenacity` retry; production key confirmed |
| 1b | `extractions.jsonl` ≈ 1,108 lines; `failures.jsonl` line count is the real failure rate |
| ~~2~~ ✅ | ~~Type histograms, `INTERACTS_WITH` count, pre-seed status, dangling-ref count, and failure rate written into `docs/failure-notes.md` and the metrics doc — no more `_TBD_`~~ — all written; the three remaining `_TBD_`s are Phase 3/5 metrics, not Step 2's. Histograms regenerated during Step 4 after hand-transcription drift |
| 3 | `deployer` / `deployers` / `the deployer referred to in Article 26(1)` resolve to one node; threshold justified by 30 labeled pairs; ADR written |
| ~~4~~ ✅ | ~~Loader run twice → identical counts; all six Cypher templates return non-empty results in Neo4j Browser~~ — both confirmed, and both are now tests rather than manual checks; 2 of the 6 templates had to be fixed to get there |
| ~~5~~ ✅ | ~~`SELECT count(*) FROM chunks WHERE embedding IS NOT NULL` = 1,108; recall@10 measured at both 1536 and 512 dims; annex chunks retain provenance~~ — all three confirmed and now tests (`tests/test_embedder.py`), plus idempotency and the `entity_ids`→graph join. ADR-0004 *Accepted*; the recall difference between the arms turned out to be one gold chunk, so the decision rests on 3× storage and 8× latency |
| Eval | `wc -l eval/eval-questions.jsonl` ≥ 50, every row has a non-empty gold **and gold `source_chunk_ids`**, strata match §5.3, `pytest tests/test_eval_questions.py` green |

Run `pytest` after Steps 3–5; the suite is ~~currently 8 schema tests~~ **now 18 tests** in
`tests/test_schemas.py` and should grow with each stage.