# Honest failure notes (RCA-style)

Record measured failure rates and debugging journeys here as they surface.

Each entry has the same shape: **what happened → why it mattered → what caught it →
what I changed → what I learned.**
A change is marked `DONE` only if code in this repo enforces it.
`OPEN` means I plan to do it but haven't yet. Doing something once by hand is not `DONE`.

## Measured rates

- Extraction Pydantic-validation failure rate: **0.09%** (1 of 1108 chunks, full
  corpus, ontology v3). Was 0.27% before raising `max_tokens`; every failure was
  output truncation, not a modelling error.
- Extraction edge-endpoint violation rate: **5.3%** of relationships (357 of 6767)
- Orphan-entity rate: **8.4%** of entities (628 of 7466)
- Vector retrieval, micro recall@10 (21 labeled queries, 51 gold references, exact search,
  1536 dims): **56.9%**; hit rate@10 **85.7%**. Per stratum: single-hop 75%, two-hop **30%**,
  aggregation 47%. Report with the caveat that the gold set is 3.6% of the corpus —
  see `docs/metrics/vector-index.md`.
- Entity-resolution false merges / misses: **0 false merges, 7 of 10 misses** at cosine 0.90,
  on 25 adversarial hand-labelled pairs. The classes are **not linearly separable** — see
  `docs/adr/adr-0009-entity-resolution.md`. Deterministic rules do the merging; the embedding
  pass returned one candidate over the whole corpus and it was a false merge.
- Graph load: **3,366 nodes / 6,680 relationships** in Neo4j from 1,107 chunks (6,658 extracted +
  **22 derived** cross-regulation bridges). Edges skipped as dangling **107 (1.6%)**; loaded but
  tagged as endpoint-violating **230 (3.4%)**; isolated nodes **112 (3.3%)**. Re-running the loader
  is a verified no-op (`tests/test_graph_writer.py::test_loading_twice_is_a_no_op`).
- Cypher templates broken on first contact with a real graph: **3 of 6** — one returned zero rows,
  one was a cartesian product, one silently missed 48 of 337 defined terms. Plus **1** row-count
  defect from parallel edges (24,428 rows where 169 were correct).
- Cypher templates broken by the Phase 3 provenance rewrite: **0 of 6** — all six row counts
  (60 / 169 / ≥1 / 1 / 4 / 1-path) unchanged, measured on the live graph before *and* after. The one
  defect the rewrite did contain was caught by a probe before any template was written; see the
  Phase 3 Step 1 entry.
- Graph load wall-clock: **2.7–3.1s warm, 9.4s cold** (3,366 nodes + 6,680 edges; the graph write is
  1.7s of that, the rest is Neo4j JVM/plan-cache warmup). See `docs/metrics/graph-load.md`.
- Eval-set metadata defects found on first mechanical check: **10 of 23 rows** declared graph edges
  their gold chunks do not carry, and **1 row's gold chunk did not contain its own answer**
  (`hn-001` pointed at AIA Art. 99(1), which states no figure). Golds themselves were sound — every
  gold chunk id existed and 16 spot-checked claims matched the source text. See
  `docs/metrics/eval-set.md`.
- Entity linking (23 eval questions, deterministic sweep, no API calls): **23 of 23 questions link to
  ≥1 graph node**; **76%** of scoreable rows link ≥1 node a gold chunk asserts; precision **52%**
  (**64%** excluding `Regulation` nodes, which questions name and chunks rarely assert). Recall
  against gold-chunk `entity_ids` is **10%** and is a **lower bound fixed by arithmetic** — that
  denominator averages 18.9 nodes per row against 3.6 links per question. See
  `docs/metrics/query-path.md`.
- Router misclassification rate: **4.5%** (1 of 22 gold-labelled rows) for the adopted
  deterministic router. Command R7B, measured on the same rows under the same pre-registered
  criterion, misclassified **55%** (12 of 22) and returned `both` for **0 of 23** questions under
  two different system prompts — below the majority-class constant (`always-vector`, 41% error).
  The 4.5% is **in-sample**: the rules were authored with the gold labels visible. See
  `docs/adr/adr-0012-router-model-vs-rules.md`.
- Citation-validation rejection rate: _TBD_
- LLM-judge agreement against a hand-verified 20% sample: _TBD_ (roadmap §5.3; `eval/judge.py` is a stub)
- Benchmark surprises (where the expected accuracy curve did not materialize): _TBD_

## Recurrence tracker

The entries below keep reaching the same conclusion in different words, so here it is as a count.
This table is the actual finding of this document: **the failure modes are not being retired, they
are changing address.** Every row is aggregated from write-ups further down — nothing here is new.

| Root cause | Times | Where |
|---|---|---|
| **A prompt few-shot example teaching the defect** | **3** | `RiskCategory` junk drawer (Example 2, since v1) · `LawfulBasis`/`PERMITS` collapse (ADR-0007) · `INTERACTS_WITH` collapsed to instrument level (ADR-0010) |
| **Confirmed the part I looked at, assumed it covered the whole** | **6** | 13 annexes silently dropped · two-layout assumption when there were four · `dangling_refs` had no mirror (`orphan_entities`) · `definition_of` probed with a term that happened to be in an Article · `Chunk` rejection measured on the AI Act file and reported as the corpus (586/694 → really 1,000/1,108) · **`_trim` verified against corpus names, reused on questions — no legal name ends in `?`** |
| **A count mistaken for a shape** | **3** | `INTERACTS_WITH` at 130 edges, 0 of them article-level · endpoint *types* recorded in no histogram anywhere · **Command R7B at 45% accuracy — a weak-but-working classifier by the count, and by the distribution a three-way classifier that emitted `both` 0 of 23 times** |
| **An interface neither side ever crossed** | **4** | `Chunk` vs the JSONL nobody validated against it · `schema.sql` vs a corpus nobody loaded · `entity_ids` vs a relationship nobody populated · **`chunker.main()` vs the corpus it produces — 1,016 rebuilt where 1,108 are stored, unnoticed for five phases** |
| **Verified once by hand, never encoded** | **3** | `aia-art9-para1` control · per-annex counts · production-key check (regressed `DONE` → `OPEN`) |
| **A metric that looked like success** | **2** | 0% validation failure on the pilot · 0 orphan reports because nothing looked for orphans |
| **A key derived from a field, and the field then dropped** | **1** | `section` folded into the annex chunk_id and never written as a column — 25 chunks, 11 ambiguous citation labels |
| **A container non-empty because of its shape, not its content** | **1** | `collect(DISTINCT {chunk: rel.source_chunk_id})` on a missed `OPTIONAL MATCH` returns `[{chunk: null}]`, not `[]` — one fake citation that passes every `if provenance:` check. **Caught by a probe before it was written**, which is the only reason the count is 1 and not a defect |

**What the shape of this table says.** The top row is the most expensive class — three defects, all
from the same 4,000-token artefact, none caught by a test, each found only by reading output. **The
few-shot examples are teaching material and have never been reviewed as such.** They are treated as
prompt filler when they are closer to executable specification.

The second row is the oldest and most persistent, and it is worth noticing that it now includes a case
from *this* document's own remedy: `definition_of` passed a non-empty test because the probe term was
chosen from the part that worked — the identical mistake as the five hand-checked articles in the
ingestion section, three phases later.

---

# Ingestion: turning the EU AI Act HTML into chunks

Four things went wrong across two passes. Two shipped, two were caught before they shipped.
The common thread is at the bottom.

Final state: **627 chunks, 50,818 words** — 519 article chunks and 108 annex chunks.

## 1. Pass 1 quietly dropped all thirteen annexes

**What happened.** The first parser produced 519 clean chunks for all 113 articles. Paragraph
numbers were right, sub-points were inlined properly, long paragraphs stayed whole. Every check
I ran passed, so I thought it was done.

It wasn't. The parser looked for article containers (`div.eli-subdivision#art_N`) and never
touched the annexes. I picked eight phrases from the annexes at random and searched the output:
0 of 8 were there.

**Why it mattered.** The annexes are where the answers live for the questions this system is
built for. Annex III says which AI systems are high-risk. Annexes VI and VII define the
conformity assessment procedures. Two of my benchmark questions end in an annex, and the graph's
`LISTED_IN` relationship (system type → annex) had nothing to point at.

The system could find Article 43 saying "follow the procedure in Annex VII" and have no idea
what Annex VII actually says. After the fix I measured it: the annexes are 6,741 words, or
**13% of the corpus.**

**What caught it.** Comparing my output against the source document section by section, instead
of only checking the articles I already had answers for. My five hand-checked articles all
passed. They were never going to catch this — I picked them myself, from the part that worked.

**What I changed.**
- `OPEN` — Add a coverage check to the build: list every top-level container in the source HTML
  and fail if any of them produces zero chunks. **Not built yet.** An earlier version of this
  file said it was. It wasn't, and that false claim sat here through an entire second pass
  without anyone noticing. See point 4.
  **Weakened again on 2026-08-02** — this check would also have passed the missing-definitions
  defect (see the last entry in this file): Article 3 produced one chunk where 68 were required.
  A zero-chunk check is the wrong shape for this failure mode three times running. What actually
  works is comparing the rebuild to the stored corpus, which
  `tests/test_chunks.py::test_chunker_reproduces_the_stored_corpus` now does.

**What I learned.** Checking that something is *correct* tells you nothing about whether
something is *missing*. Everything I verified was true. The 519 chunks really were correct. And
the verification still signed off on a corpus missing 13% of the law.

Sampling can only tell you about quality inside the area you chose to look at. It can never tell
you that you chose the wrong area. Those are two different checks, and only the second one would
have caught this.

## 2. The annexes use four HTML layouts, not two

**What happened.** Going into pass 2, I thought the annexes used two layouts: tables with the
point marker in a cell, and numbered `oj-ti-grseq-1` headings. I checked all thirteen containers
before writing code and found **four**, with several annexes mixing them:

| Layout | What it looks like | Annexes |
|---|---|---|
| A — 2-column table | `<td>` marker + `<td>` text | III, IV, V, IX, XII, XIII |
| A3 — **3-column** table | **empty** `<td>` + marker + text | I, VII, XI |
| B — heading | `<p class="oj-ti-grseq-1">1. Introduction</p>` | VII, X, XI, VIII |
| C — div block | `<div class="oj-enumeration-spacing">`, number **inside the text** | VI |

Three specific things I believed were wrong. Annex V is not one unnumbered template — it has 8
numbered points. Annex VI is not heading-based; it uses layout C, which I hadn't seen at all.
And the tables are not all 2-column.

**Why it mattered.** Building to the two-layout assumption would have mangled Annexes I, VI, VII
and XI — about a third of the annex content. Worse, it wouldn't have crashed. It would have
produced output that looked fine.

**What caught it.** Reading the source before writing the parser, and printing a summary of all
thirteen containers — child tags, table column counts, marker sequences — instead of reading two
of them and assuming the rest matched.

**What I changed.**
- `DONE` — The four layouts and which annex uses which are written into the module docstring of
  `src/ingest/annex_parser.py`, so the next person sees them before touching the code.

**What I learned.** "I looked at the source" is not the same as "I looked at all of it." I had
looked at the annexes — two of them — and assumed the rest followed. The full survey took ten
minutes and changed the design. Two examples would have given me confident, wrong code.

## 3. Two problems caught before they shipped

Both were found while designing, not by tests.

**Duplicate chunk IDs.** Annex VIII restarts its point numbering in each section (A: 1–13,
B: 1–9, C: 1–5). Annex XI does the same across Section 1 and 2. The planned ID format
`aia-annex{N}-point{M}` would have produced `aia-annex8-point1` three times.

`chunk_id` is what links pgvector and Neo4j together. Duplicates would have quietly broken both
— no error message, just wrong search results and graph edges attached to the wrong text.

Fixed by adding the section to the ID **only where a number actually repeats**
(`aia-annex8-sectionB-point1`). This is worked out per annex rather than hardcoded, because
Annex I also has sections but numbers straight through 1–20, so it doesn't need one.

**A source typo mistaken for a parser bug.** Article 1's title came out as ``"Subject matter`"``
and was reported to me as a parser bug. The backtick is in the source HTML at line 3027 — the
only one in the whole 1.2 MB file. The parser was correctly copying a typo that was already
there.

**What I changed.**
- `DONE` — `_clean_title` in `src/ingest/parser.py` cleans titles only. Body text stays exactly
  as written, so citations still quote the real sentence.

**What I learned.** Two things.

Whether an ID is unique depends on the *data*, not the ID format. I couldn't tell if the format
was safe until I looked at the actual numbering. No amount of thinking about the format would
have shown me the problem.

And before fixing an artifact, check whether the source already has it. A parser faithfully
copying a source typo and a parser inventing one look identical in the output, but they need
opposite fixes.

## 4. Pass 2 shipped 102 chunks when it should have shipped 108

**What happened.** The first full annex run gave 102 chunks. Annex XIII had collapsed its seven
lettered points `(a)`–`(g)` into one chunk.

The code handling lettered points with no parent heading opened a chunk for `(a)`, and then
`(b)` through `(g)` saw an open chunk and got appended into it. Fixed by tracking whether the
open chunk is allowed to absorb sub-points: yes when it was opened by a heading or a plain `N.`
marker (so Annex X's `(a)/(b)` correctly sit under *"1. Schengen Information System"*), no when
it came from the lettered fallback.

**Why it mattered.** Annex XIII lists the criteria for classifying a general-purpose AI model as
having systemic risk. Seven separate criteria that a search should be able to match one at a
time.

Here's the important part: as one chunk, the text was still **there and still readable**. So the
coverage check I proposed in point 1 — "every container produces at least one chunk" — **would
have passed.** Nothing was missing. It was just fused into an unusable blob.

**What caught it.** A per-annex count table, compared against counts I had worked out by hand
*before* running the parser. 102 vs 108 stood out immediately and pointed straight at one annex.
Nothing else in the run failed.

**What I changed.**
- `OPEN` — Turn the verification checks into real tests in `tests/`. Right now they only exist
  in scratch scripts: 627 lines, unique IDs, exact schema keys, the VIII/XI section splits, text
  hygiene, and expected per-annex counts. Both passes were checked by hand and neither left
  anything behind that would catch a regression.
  **Mostly closed:** `tests/test_chunks.py` now covers unique IDs, exact schema keys (round-trip,
  `extra="forbid"`), the VIII/XI section splits, and per-annex counts. **Text hygiene is still
  untested** — that is what remains of this item.
- `DONE` (2026-08-02) — `EXPECTED_ANNEX_COUNTS` in `tests/test_chunks.py:29` stores the counts as a
  fixture, cross-checked against `EXPECTED_SHAPES["annex"]` so the two tables cannot drift.
  One honest caveat recorded in the test itself: the numbers are transcribed from today's verified
  corpus, not from an independent hand count, so it catches a *regression* from here — it does not
  re-verify the parser against the source HTML the way the original 102-vs-108 check did.

**What I learned.** The safeguard I designed after failure 1 would not have caught failure 2 —
and I would have assumed it did. "Every container produces at least one chunk" and "every
container produces the *right number* of chunks" are different checks, and the weak one gives
you the confident feeling of the strong one.

Also: my hand-worked counts only helped because I wrote them down *before* the run. Afterwards,
102 would have looked just as reasonable as 108.

## The common thread

All four are the same mistake in different clothes: **I confirmed the part I was looking at and
assumed it covered the whole.** Five hand-checked articles stood in for 113. Two annexes stood in
for thirteen. "It's present" stood in for "it's usable."

The fix is not more careful checking. It's writing down what I expect *before* I run anything,
then comparing — because after the fact, whatever came out looks reasonable.

---

# Extraction: the ontology had no way to say "you may"

One failure, found on a 10-chunk test run before the full corpus. It produced legally false
facts at high confidence, and every single one of them passed validation.

Full decision record: `docs/adr/adr-0007-lawfulbasis-permits.md`.

## 1. Six lawful bases extracted as six obligations that don't exist

**What happened.** The first ontology had eight entity types and ten relationship types. Every
relationship expressed either a duty (`IMPOSES`) or a prohibition (`CLASSIFIED_AS` prohibited,
`EXEMPT_FROM`, `ENFORCED_BY`). None expressed permission.

Regulations make three basic moves. The ontology could represent two of them:

| Legal move | Representation | Covered? |
|---|---|---|
| You **must** | `IMPOSES` → `Obligation` | yes |
| You **must not** | `CLASSIFIED_AS` prohibited | yes |
| You **may, if** | — | **no** |

GDPR Article 6 is entirely the third move, so it had nowhere valid to land. The extractor put it
in the nearest available slot: the six lawful bases — consent, contract, legitimate interests and
the rest — came out as six `Obligation` entities wired to Article 6(1) by `IMPOSES`, each at
**0.95 confidence**.

**Why it mattered.** Article 6(1) imposes no duties. It says processing is lawful if *at least
one* basis applies — permissive, alternative conditions. The graph was asserting six simultaneous
mandatory duties that do not exist, and would have answered *"what must a controller do under
Article 6?"* with six inventions.

The same distortion hit the Article 9(2) derogations, turning them into thirteen phantom
obligations, and produced `ENFORCED_BY` edges on "Union or Member State law" that the text never
states. One root cause behind all of it.

**What caught it.** Running extraction on ten deliberately varied chunks first, and *reading the
output* instead of trusting the aggregate metric. The metric said the run was clean: **0% Pydantic
validation failure.** Every record was schema-valid. Every record was also wrong.

**What I changed.**
- `DONE` — Added `LawfulBasis` (9th entity type) and `PERMITS` (11th relationship type) — the
  permissive counterpart to `IMPOSES`. Both are locked into the `Literal` types in
  `src/ingest/extract.py`, so anything outside the ontology fails validation instead of entering
  the graph. `tests/test_schemas.py` covers both.
- `DONE` — The extraction system prompt in `src/ingest/extract.py` carries an explicit
  disambiguation rule ("lawful/permitted if a condition holds" → `LawfulBasis` + `PERMITS`, never
  `Obligation` + `IMPOSES`) plus a few-shot example built from GDPR Art. 6(1).
- `DONE` — Re-tested the affected chunks alongside a **control chunk** (`aia-art9-para1`, a
  genuine obligation) to confirm the new permissive rule fixed the false duties without bleeding
  into real ones.
- `DONE` (2026-08-02) — `tests/test_extraction_behaviour.py` asserts the behaviour, not just the
  schema: `gdpr-art6-para1` yields **zero** `Obligation` (and still yields `LawfulBasis`, so
  deleting the type would not pass it), with `aia-art9-para1` as the control that must still yield
  one. Runs off `data/processed/extractions.jsonl`, no API calls. This closes the case where a
  prompt regression would be schema-valid and silent — the hand-check from the v3 re-run now leaves
  something behind.

**Known limitation, left in place deliberately.** `PERMITS` records *that* a basis makes something
lawful, but not that the bases are **alternatives** ("at least one of"). The graph shows six
`PERMITS` edges without encoding that satisfying one suffices. Modelling n-ary one-of constraints
in a property graph is a research-grade problem, so it is flagged rather than solved — and the
vector path still carries the exact disjunctive wording for any query that needs it.

## What I learned

**Validity is not correctness.** A 0% validation-failure rate measures whether the model filled
the shape. It says nothing about whether the shape can represent the domain. The cleanest metric
in the run was produced by the broken part.

**A missing type doesn't leave a gap — it produces the nearest wrong answer.** The absence of
`PERMITS` didn't yield empty fields or low confidence. It yielded confident, well-formed,
plausible-looking obligations. An ontology hole shows up as fluent wrong output, never as an
error.

**Confidence scores describe fit to the schema, not truth.** 0.95 on six fabrications. The model
was correctly certain it had picked the best available type; the type set was the problem.

**Design ontologies by opposites.** For every relationship type, ask what its inverse is and
whether the corpus contains it. `IMPOSES` had no `PERMITS`. Ten minutes of that question against
a permission-heavy regulation like the GDPR would have found this before any code ran.

**Vary test chunks by legal function, not by source.** The ten chunks caught this because they
spanned duties, permissions, definitions and derogations — not because they came from different
articles. Ten obligation-shaped chunks would have scored 0% failure and taught me nothing.

This is the ingestion common thread wearing different clothes. There I confirmed the part I was
looking at and assumed it covered the whole; here I confirmed the *shape* of the output and
assumed it covered the meaning. Same shortcut, one level up.

---

# Extraction, second pass: the same hole, twice more, found on purpose this time

Two pre-flight probes before paying for the full corpus. Both found things. Full decision record:
`docs/adr/adr-0008-definedterm-right-penalty.md`.

The probes existed because of one property of the code: `cache_key()` hashes the system prompt, so
an ontology fix found *after* the full run invalidates all 1,108 cached responses and costs a
second full run. That turns "read the output before paying" from good practice into arithmetic —
$0.28 of probes against a $23 purchase that is awkward to redo.

## 1. `RiskCategory` had quietly become the junk drawer

**What happened.** Of six distinct `RiskCategory` values across the stored chunks, exactly one —
`high-risk` — was a risk category. The others were `biometric data`, `special categories of
personal data`, `specific categories of personal data`, and `making available on the market`.

The ontology had no type for a term of art, so every definiendum landed in the nearest wrong slot.
And the system prompt's **own Example 2 taught it**, typing `biometric data` as `RiskCategory`. The
bad example had been sitting in the prompt since v1, teaching the mistake on every call.

**Why it mattered.** AIA Art. 3 is 94 definition chunks. Left alone, a large fraction of the corpus
would have been mistyped, and the `RiskCategory` label would have meant nothing — "what is
high-risk?" would have answered "making available on the market".

**What caught it.** Aggregating the *distinct values* of one entity type across all stored rows,
rather than reading extractions chunk by chunk. Every individual extraction looked fine. The type
histogram in the metrics doc had said `RiskCategory: 8` for weeks and nobody asked which eight.

## 2. The same right, modelled two different ways, one paragraph apart

`gdpr-art21-para2` produced `Obligation: allow data subjects to object` + `IMPOSES`.
`gdpr-art21-para5` produced `LawfulBasis: right to object by automated means` + `PERMITS`.

Same right. Two types. Entity resolution compares within a type, so it could never have merged
them — the fragmentation would have been permanent and invisible.

**Why it mattered.** GDPR Chapter III is *entirely* data-subject rights, roughly 80 chunks. There
was no `Right` type, so every one of them would have been split across `Obligation` and
`LawfulBasis` according to phrasing.

**What caught it.** The random sample happening to draw two paragraphs of the same article. A
sample stratified by article would have drawn one and seen nothing wrong.

## 3. Two false facts that passed every check

```
EXEMPT_FROM  AIA Art. 5 -> AIA Art. 99   (0.90)
ENFORCED_BY  take necessary steps ... -> AIA   (tail is a Regulation, not an Authority)
```

The first says the AI Act's most severely punished provision is exempt from penalties. The source
says "other than those laid down in Articles 5", which routes Art. 5 breaches to a *higher* tier
(Art. 99(3), EUR 35 000 000 / 7 %).

`Literal` validates the type *string*. It has no way to see that an edge's ends are the wrong kind
of thing. Both were schema-valid.

**What I changed.**
- `DONE` — `ALLOWED_ENDPOINTS` in `src/ingest/extract.py` declares head/tail types for all 13
  relationships; `endpoint_violations()` checks every edge after parsing and the count is printed
  in the run report. Covered by `tests/test_schemas.py`, including both real errors above as
  regression cases.
- `DONE` — Violations are **counted and printed, never dropped.** A dropped edge is
  indistinguishable from one that was never extracted, which is precisely how the v1 ontology hole
  stayed hidden. Detection beats silent repair.
- `DONE` — `orphan_entities()`, the mirror of `dangling_refs()`. `aia-art99-para4` declared nine
  cited Articles and connected none of them; `dangling_refs()` looks only for edges with no entity,
  so it passed clean and reported nothing.

## 4. Article names were sub-numbered about half the time

`GDPR Art. 21(2)` and `Art. 49(5)` came out correct; `Art. 13`, `Art. 65` and `AIA Art. 99` came
out bare — same construct, opposite behaviour, roughly 50/50. Bare and sub-numbered names become
two nodes, which severs every cross-reference into that paragraph, and `REFERENCES` is the
cross-reference backbone the whole graph path depends on.

The paragraph number was in the chunk metadata the entire time, passed to the model in the header
and unused.

**What I changed.**
- `DONE` — An explicit normalisation rule pins the chunk's own article to its paragraph or
  definition number, and both worked examples were corrected to match. Measured after: **0 bare
  self-article names across 27 chunks**, from ~50%.
- `DONE` — `granularity_miss()` reports the rate every run, so a regression shows up as a number
  rather than as a quietly fragmenting graph.

## What I learned

**A bad few-shot example is worse than no example.** Example 2 taught `biometric data` as a
`RiskCategory` on every single call. Prompt examples are training data with none of the review that
training data gets.

**Aggregate the values, not just the counts.** `RiskCategory: 8` looked healthy in the metrics
table. `RiskCategory: {high-risk, biometric data, making available on the market, ...}` was
obviously broken on sight. Same data, one `Counter` apart — and the healthy-looking version had
been sitting in the doc for weeks.

**Two samples of the same thing beat two samples of different things.** The rights bug was only
visible because the draw happened to include two paragraphs of Article 21. Consistency failures are
invisible to any sample that touches each construct once, and "cover everything once" is exactly
what a well-designed sample usually optimises for.

**A schema that constrains types does not constrain meaning.** `Literal` made invented types
impossible and false statements easy. Every wrong edge here was schema-valid; three passes of
Pydantic validation reported 0% failure while asserting that Article 5 carries no penalty.

**The pattern is now three-for-three.** Missing `LawfulBasis` produced phantom obligations. Missing
`DefinedTerm` produced phantom risk categories. Missing `Right` produced both, inconsistently.
Every time, the ontology hole showed up as fluent, confident, well-formed wrong output — never as
an error, never as a gap, never as low confidence. **Design ontologies by opposites** was the
lesson from ADR-0007 and it was right; I just did not run it a second time after adding `PERMITS`.

---

# The full corpus run: four ways to lose a run, three of them mine

Final state: **1107 of 1108 chunks extracted (99.9%)**, 7,466 entities, 6,767 relationships, at
roughly **$24** against a $23.13 estimate. It took four attempts.

## 1. One bad chunk killed 215 chunks of work

**What happened.** At chunk 215 the API returned `422 NO_VALID_RESPONSE_GENERATED`. The run died
with a traceback.

**Why it mattered.** `extract_chunk` catches `(ValidationError, JSONDecodeError, TypeError)` — the
three ways a *response* can be wrong. It does not catch the API refusing to produce one. That
exception went straight through `main()` and ended everything.

Worse: results were written only after the loop finished. So the crash left `extractions.jsonl` at
**29 rows after 215 chunks of completed work.** The disk cache meant no money was lost, and that is
exactly what made the gap easy to miss — the *expensive* resource was protected, so the fragility
of the *cheap* one never came up.

**What I changed.**
- `DONE` — API errors are caught per chunk, recorded to `failures.jsonl`, and the run continues.
  No retry: `temperature=0` with a fixed seed means the identical request fails identically.
- `DONE` — `flush()` writes every `FLUSH_EVERY` (25) chunks and on `KeyboardInterrupt`.

**What I learned.** I protected the resource that costs money and forgot the one that costs time.
A cache that makes crashes free to *recover from* quietly removes the pressure to make them rare.

The fix proved itself immediately: the next run was killed at chunk 906 and left **893 rows on
disk** instead of 29.

## 2. The key was a trial key, and I was told twice

**What happened.** The 422's response headers carried `x-endpoint-monthly-call-limit: 1000` and
`x-trial-endpoint-call-limit: 20`. The corpus needs 1108 calls.

I had flagged "confirm the key is production" in the plan and again in the pre-run summary, then
started the run without checking. Verifying it takes one request and reading two headers.

**Why it mattered.** It cost a wasted run and a full diagnostic detour. It also hid: the 20/min
trial limit never triggered a 429, because sequential extraction runs at ~10/min. **The rate limit
I had built retry logic for was not the limit that was going to stop me.**

**What I changed.**
- ~~`DONE`~~ → `OPEN` — I wrote here that "production keys return no trial headers, and that absence
  is now the check." That was a check I ran **once, by hand**. No code enforces it. By this file's
  own rule that is not `DONE`, and marking it so was the same false claim the ingestion section
  opens with. It regressed within a day — see "Entity resolution", point 5.

**What I learned.** Writing a risk down is not mitigating it. The check was in the plan in two
places and neither made it into the sequence of commands I actually ran.

## 3. `failures.jsonl` is destroyed by the next full run

`write_jsonl` keeps rows whose `chunk_id` is not in `touched`. On `--all`, `touched` is every chunk,
so every prior failure row is dropped and replaced by this run's.

For *extractions* that is correct. For *failures* it means the raw response and validation error —
the only evidence of what went wrong — are gone the moment you re-run, before anyone reads them.
I recovered the count from console logs; the detail was already unrecoverable.

- `OPEN` — Append failures with a run timestamp instead of replacing them, or write them to a
  per-run file. Not built.

## 4. Three chunks were truncated, and truncation does not look like truncation

**What happened.** Three chunks failed validation with `Unterminated string`, `Expecting ','`, and
`Expecting value` — at character 14165, 16450 and 16330. Three different JSON errors, one cause:
the response hit `max_tokens` and stopped mid-object.

Nothing in the error says "too long." The `truncated at max_tokens: 3` counter in the run report is
what matched them up, and it only existed because it was added for an unrelated reason.

**What I changed.**
- `DONE` — `MAX_TOKENS` 4096 → **8192**, which fixed two of the three. It is not in `cache_key`, so
  this re-called 3 chunks rather than 1108.
- `DONE` — 8192 is Command A's hard ceiling; 16384 and 32768 both return HTTP 400. Written into the
  constant so nobody re-tests it.

**Still failing: `gdpr-art70-para1`.** The corpus's largest chunk — 864 tokens, 33 lettered
sub-points, the EDPB's full task list. Its extraction does not fit in 8192 output tokens at all.
This is a **chunking** failure surfacing at extraction: ADR-0003 chose paragraph-level chunking, and
this is one paragraph that is too large for the model's entire output budget. `rechunk_definitions.py`
already sets the precedent for splitting oversized units, but re-chunking now would change
`chunk_id`s and the corpus count, which ripples into both stores.

- `OPEN` — Split oversized paragraphs at the chunker, the way definitions already are. Deferred
  because the chunk_id contract is now load-bearing.

The text is still in the vector store, so the vector path can answer from it. Only the graph path
loses those edges — and they are EDPB task assignments, exactly the "which authority does what"
content the graph is meant to be good at.

**What I learned.** A hard model limit is a *corpus design* constraint, not a parameter to tune. I
found the ceiling by raising the number until the API said no, which is the right way to find it and
the wrong time to be finding it — that probe costs one request and belongs in Phase 1 planning,
next to the token-count histogram I had already built.

## What I learned overall

**Every one of these was in the plan except the truncation.** Retry hardening, key verification,
resumability — all written down, none of them load-bearing until they failed. The plan was not
wrong; it just was not a checklist, and I treated it as prose.

**The failure modes moved down the stack as the obvious ones got fixed.** Ontology (v1) → prompt
(v2/v3) → error handling → credentials → model limits. Each layer looked fine until the one above
it stopped failing loudly enough to hide it.

---

# Entity resolution: the sophisticated stage was the one that didn't work

Final state: **3,485 → 3,366 nodes**, all of it done by deterministic rules. The Embed v4 stage the
roadmap called "the hard part" produced one suggestion across the whole corpus, and acting on it
would have been a legally false merge. Full decision record:
`docs/adr/adr-0009-entity-resolution.md`.

## 1. The design had no stage for the problem the data actually had

**What happened.** The plan's pipeline was normalize → exact match → embedding similarity → merge.
The audit had already found **66 names carrying more than one type** (`AI system` is both
`DefinedTerm` and `SystemType`; `Member State` spans three). Resolution compares *within* a type.
None of those 66 are reachable by any stage in that pipeline.

**Why it mattered.** They are not near-misses a better threshold would catch — they are invisible.
Every one would have entered Neo4j as two or three disconnected nodes for one concept, and every
multi-hop query through them would have silently returned partial results.

Most were self-inflicted: adding `DefinedTerm` in ADR-0008 fixed the `RiskCategory` junk drawer and
created this. Art. 3 *defines* "AI system", so the definition chunk types it one way and the other
1,100 chunks type it another.

**What caught it.** The corpus audit, not the resolution code. `src/ingest/audit.py` counts names
carrying more than one type because the extraction pass needed that number; entity resolution
inherited a problem already sitting in a report.

**What I changed.**
- `DONE` — A type-reconciliation stage: pooled majority vote per normalized name, logged with the
  vote and the rule that settled each one. 64 names reconciled.

**What I learned.** A fix at one layer becomes an input problem at the next. `DefinedTerm` was
correct and I still owe the graph a stage that did not exist before I added it.

## 2. Stage order decided the answer, and I nearly got it backwards

Taken on its own, `Member State` resolves to `Authority` (14 `DefinedTerm`, 5 `Authority`,
1 `ActorRole`). `member state` resolves to `ActorRole` (67, 3, 1). **Same concept, two types, purely
because of capitalisation.**

Reconciling types before case-folding would have frozen that split permanently — and it would have
looked fine, because each decision is individually defensible against its own vote.

Caught by checking the two forms against each other before writing the pipeline, rather than after.
Pooling the votes after the fold gives `ActorRole` for both.

- `DONE` — Case-fold runs first; the ordering constraint is written into the module docstring, since
  nothing in the code makes it obvious that swapping two stages changes the output.

## 3. A rule that was right in principle overrode 18 votes with 1

**What happened.** `DefinedTerm` is the ontology's designated catch-all, so "a specific type beats
the catch-all" is a principled rule. Applied literally it produced:

```
international organisation   {DefinedTerm: 18, ActorRole: 1, Authority: 1}  ->  Authority
```

Drop the catch-all, and what remains is a 1-1 tie broken by type priority. Eighteen votes discarded
to settle an argument between two. "International organisation" is a defined term in GDPR
Art. 4(26); it is not an authority.

**What caught it.** The decision log. I had made `reconcile_types` record every reconciled name with
its vote *and the rule that fired*, for explainability — and that column is what made a wrong answer
legible. The winner alone (`Authority`) looks unremarkable next to `advisory forum` and
`certification body`; `[catch-all dropped + tie -> priority]` next to an 18-vote majority does not.

**What I changed.**
- `DONE` — The catch-all only loses to a type the corpus actually attests: it survives when it holds
  an outright majority. That correctly keeps `systemic risk`, `adequacy decision`, `appropriate
  safeguards`, `post-market monitoring system` and `law enforcement purpose` as defined terms.

**What I learned.** A rule can be sound and still be wrong at the tails. "Specific beats generic" is
right when the specific type is *attested*; it is noise amplification when it is one stray mention.
I would not have found this by reasoning about the rule — only by printing what it decided.

## 4. The threshold could not be tuned, because the classes do not separate

The roadmap says: tune the cosine threshold on ~30 hand-labelled pairs, starting at 0.90. I built 25
pairs from the regulations themselves — the AI Act and GDPR define these terms, so the labels are
citations, not opinions. The result:

| | pair | cos |
|---|---|---|
| **must NOT merge** | `supervisory authority` / `lead supervisory authority` (GDPR Art. 56) | **0.753** |
| **must merge** | `data protection officer` / `dpo` | **0.423** |

The classes overlap by 0.33. **No threshold exists.** Two structural reasons:

- **Legal modifiers create new entities and embeddings read them as synonyms.** "lead",
  "prospective", "downstream", "joint", "notifying" each define a separate role. Compositional
  similarity is exactly wrong here.
- **Embeddings are hopeless at abbreviations.** `dpo`/`data protection officer` scores below every
  must-not-merge pair in the set.

At 0.90 the measured result is **0 false merges of 15, 7 misses of 10** — and every miss belongs to a
class a deterministic rule handles better and cheaper (hyphens, plurals, abbreviations).

**What I changed.**
- `DONE` — Hyphen folding in `normalize()` (`law-enforcement authority` was the highest-scoring pair
  in the whole set at 0.966 — no reason to pay an API call for a character class).
- `DONE` — Plural folding that only merges when **both forms are attested in the corpus**. Blind
  `s`-stripping breaks `premises`, `analysis`, `business`.
- `DONE` — `SIMILARITY_THRESHOLD` stays 0.90, the measured zero-false-merge point, and the embedding
  pass emits **candidates for review, never automatic merges**.

## 5. The one thing embeddings suggested was a false merge

Run at 0.90 over all 236 role-like nodes, the embedding pass returned exactly one pair:

```
0.9137  real time remote biometric identification system ~ remote biometric identification system
```

AIA Art. 5(1)(h) **prohibits** real-time remote biometric identification in publicly accessible
spaces for law enforcement. Post-remote RBI is high-risk under Annex III — permitted with
obligations. Art. 3 defines the two separately. Merging them collapses a prohibition into a
permission: the ADR-0007 failure again, arriving through the resolution stage instead of the
ontology.

**What I learned.** The roadmap calls entity resolution "the hard part" and budgets the expensive
model for it. On a corpus of *defined* legal terms the hard part is knowing when **not** to merge,
and that is a job for rules with citations behind them. I was ready to tune a threshold; the useful
work was proving a threshold could not exist.

## 6. My own key check regressed inside a day

**What happened.** The previous section records `DONE` for "production keys return no trial headers,
and that absence is now the check." Halfway through this stage an embed call came back carrying
`x-endpoint-monthly-call-limit: 1000`.

The key had changed underneath me. The production key was `CO_API_KEY` (53 chars) set in the shell
environment; only `COHERE_API_KEY` (40 chars, trial) is in `.env`. `get_client()` reads
`os.getenv("CO_API_KEY") or os.getenv("COHERE_API_KEY")`, so when the environment variable stopped
being present, **it silently downgraded to the trial key and kept working.**

**Why it mattered.** Not much this time — extraction had already completed under the production key,
and the embedding work needs three calls. But the failure mode is: a long run starts, quietly uses a
1,000-call/month key, and dies partway with a limit error that looks like a new problem. Which is
exactly what happened the first time, one section above.

**What caught it.** Reading response headers on an unrelated 400 error. Nothing was watching.

**What I changed.**
- `OPEN` (half done, 2026-08-02) — `get_client()` **now reports which variable it resolved**, and
  the `CO_API_KEY or COHERE_API_KEY` fallback moved into `settings.cohere_api_key` so the extractor
  and the embedder can no longer resolve to different keys. Still `OPEN`: **refusing** a `--all` run
  on a key that returns trial headers. That needs a live probe of the response headers, which is a
  different piece of work from naming the variable.

**What I learned.** I marked a manual check `DONE` and it regressed before the next stage finished.
A verification that is not in the code is not a verification, it is a memory — and this file has now
caught me making that exact mistake twice, in consecutive sections.

**Known limitation, left in place deliberately.** Alias-based merging is not applied. 53% of mentions
carry extractor aliases and 88 alias→canonical pairs are real merges — including `the board` →
`european artificial intelligence board`, correct per AIA Art. 65. The same list also yields
`law enforcement agency` ← `law enforcement purposes`, which is nonsense. Candidate-quality, not
merge-quality, and the whole lesson of this stage is not to auto-apply that.

## What I learned

**The cheap stage did the work and the expensive stage did none of it.** Deterministic rules merged
119 nodes; Embed v4 proposed one merge and it was wrong. The roadmap's cost intuition was inverted
for this corpus, and only measuring showed it.

**Explainability caught a bug that correctness checks would not have.** The decision log was built to
make the graph traceable. It found a wrong rule instead. Every reconciled name still looked plausible
in the output; only the *reason* was visibly absurd.

**Two of the six items here are recurrences.** The false merge is ADR-0007's prohibition/permission
collapse arriving through a different stage; the key check is the same false `DONE` this file called
out in the ingestion section. The failure modes are not being retired, they are changing address.
---

# Graph load (Step 4): two Cypher templates written against a graph nobody had loaded

**What happened.** The six templates in `src/query/cypher_templates.py` were written during
scaffolding, months before a graph existed. Before loading, I simulated all six in Python against the
resolved entities and edges. Two were broken.

`cross_regulation` required `(:Article)-[:INTERACTS_WITH]-(:Article)` and returned **zero rows**. All
130 `INTERACTS_WITH` edges point at the *instrument*: `Article→Regulation` 108, `Annex→Regulation` 11,
`Regulation→Regulation` 10, `Right→Regulation` 1. The AI Act ↔ GDPR bridge was there; the template was
asserting a shape the corpus never produced.

`obligations_for_system` had a second, unconnected `MATCH (a:Article)-[:IMPOSES]->(o:Obligation)` — a
cartesian product crossing the matched system with all 1,219 `IMPOSES` edges.

**Why it mattered.** These are the cross-regulation and risk-classification questions — the two the
graph path exists to showcase. `cross_regulation` would have failed silently as "no results found,"
which reads as a corpus gap rather than a query bug. And `obligations_for_system` **returned rows**,
which is exactly why it survived being written: nobody counts 1,219 rows to check they are the right
ones.

**What caught it.** Simulating the templates before loading, prompted by the Step 4 note *"a template
returning zero rows means the graph shape does not match what Phase 3 assumes — you want to know that
now, not in week 3."* Not a test, not the loader — reading the query against the data.

Two related things the metrics doc could not have caught:

- **`INTERACTS_WITH` at 130 looked like the check passing.** Run 3 flagged it as sparse (3 edges) and
  said to count it corpus-wide before assuming the bridge exists. The count came back healthy and the
  worry was closed — but the count was never the risk. The *endpoint types* were, and no histogram in
  the metrics doc records an endpoint type.
- **A third defect only appeared once loaded.** Edges are stored one per asserting chunk, so
  `high risk ai system -[:CLASSIFIED_AS]-> high risk` is **124 parallel edges**. That inflated
  `obligations_for_system` from 169 rows to **24,428**. The simulation used Python sets and so could
  not see it; the fix is `RETURN DISTINCT` on every node projection.

**A third defect, found by chasing a discrepancy rather than by a test.** After loading, my simulation
said `definition_of` covered 337 terms and the live graph said 286. The 51-term gap was the template:
its tail was pinned to `(a:Article)`, but `DEFINED_IN`'s endpoint contract is Article **or** Annex, and
**48 terms are defined only in an Annex** (AIA Annex IV defines `computational resources`, Annex VIII
defines `status of the ai system`; the other 3 are endpoint violations). Those terms returned zero rows.

**This one had already passed a non-empty test.** The probe parameter was `provider`, which happens to
be defined in an Article. A template can be 86% correct and look perfectly healthy — and the only
reason this surfaced is that two numbers which should have matched didn't, and the gap was worth
chasing rather than rounding off. Fixed to `(a:Article|Annex)`, with an annex-defined term added to the
parametrized template cases so the probe set can no longer be accidentally unrepresentative.

**What I changed.**
- `cross_regulation` matches `:Entity` on both ends; `obligations_for_system` hangs its obligation leg
  off the system as an `OPTIONAL MATCH`; `definition_of` accepts `:Article|Annex`. `DONE`.
- `RETURN DISTINCT` on all five node-projecting templates, with the reason in the module docstring.
  `DONE`.
- All six templates are now asserted non-empty against the live graph in
  `tests/test_graph_writer.py`, plus an exact-row-count assertion (169) that would fail if `DISTINCT`
  were ever dropped. `DONE`.
- A shared `:Entity` label on every node, so the previously label-free matches in `definition_of` and
  `path_between` hit an index instead of scanning. `DONE`.

**Loader policies, decided in writing rather than by accident** (`src/ingest/graph_writer.py`):
241 endpoint violations load **tagged** `endpoint_violation: true`; 107 dangling edges are **skipped**
and listed in `data/processed/graph-load-report.json`; 112 isolated nodes **load**. The first follows
this repo's existing rule that a dropped edge is indistinguishable from one never extracted. The
second is the exception, and for a specific reason: an undeclared endpoint has no type, therefore no
label, and an untyped node would give the unlabeled `path_between` shortest paths through nodes that
do not really exist.

**`OPEN`: the templates return nodes, never relationships.** `source_chunk_id` is on every edge and is
the join key to pgvector, but no template projects it, so citation validation currently has no way to
say which paragraph asserted an edge it used. Phase 3 needs to fix this and it is not a one-line
change.

**`OPEN`: `gdpr-art70-para1` still has no edges in the graph.** The 864-token EDPB task-list paragraph
never extracted, so "which authority does what" has no graph path. Already recorded above as a
chunking problem; the graph now makes the consequence concrete.

**What I learned.** **A count is not a shape.** Every number I had about `INTERACTS_WITH` was healthy —
130 edges, spanning 146 distinct nodes, up 43× from the probe. The one fact that mattered, that not a
single one of them ends at an Article, was not in any table I had built, because I had been auditing
*how much* was extracted and never *what it connects to*. The Step 0 lesson was "read the output, not
the aggregate"; this is the same lesson one level up — read the output *against the query that will
consume it*.

And **the failures got quieter as they got worse.** `cross_regulation` returned zero rows and announced
itself. The cartesian product returned 1,219 wrong rows and looked like a working query — caught only
because the number was too large to be plausible. `definition_of` returned *correct* rows for 86% of
terms and nothing at all for the rest, which no non-empty assertion can detect and no implausible
number betrays. The only thing that caught the quietest one was two counts that should have agreed and
didn't.

**Consequence for how I test queries:** "returns rows" is a smoke test, not a check. What actually
found things here was comparing a query's coverage against the edges that ought to satisfy it — so the
template tests now assert coverage (334 defined terms reachable, 169 exact rows), not just non-emptiness.

---

# The cross-regulation bridge existed under the wrong name for a whole phase

**What happened.** `INTERACTS_WITH` is the AI Act ↔ GDPR bridge — the roadmap calls it "your best demo
material" and specifies it as *article → article across regulations*. The loaded graph had **130 of
them and not one connected two articles.** Every single edge terminated at a `Regulation` node.

The extractor was not failing to see the link. In **12 of 12** chunks that cite a specific foreign
article it identified that article correctly, created the entity, and emitted a `REFERENCES` edge to
it — then emitted `INTERACTS_WITH` pointing at the instrument instead:

```
aia-art26-para9   REFERENCES      AIA Art. 26(9) → GDPR Art. 35   ← the bridge, present
                  INTERACTS_WITH  AIA Art. 26(9) → GDPR           ← collapsed to the instrument
```

**Why it mattered.** `cross_regulation` is one of six templates and the whole point of the graph path.
It returned zero rows, and the eval set's cross-regulation stratum had nothing to traverse.

**What caught it.** Not the metrics. `INTERACTS_WITH` was flagged as thin in Run 3 (3 edges across 28
chunks), the post-run audit counted it corpus-wide at 130, and the worry was closed on that number.
**The count was never the risk.** It was caught by reviewing the eval set's declared `ontology_edges`
against the graph, which forced the question "130 edges between *what*?" — and no histogram anywhere
recorded endpoint types.

**Root cause: the system prompt's own few-shot example teaches the collapse.** `extract.py`'s example
builds the `GDPR Art. 4(14)` entity, emits `REFERENCES` to it, then emits `INTERACTS_WITH → GDPR`. The
model was being shown exactly the behaviour that was later called a bug. **This is the third time a
defect has been traced to a demonstration in the prompt** — the `RiskCategory` junk drawer was Example
2 since v1, and the `LawfulBasis` collapse was ADR-0007. The examples are teaching material and are not
being reviewed as such.

**What I changed.** The article-level link was already in the graph under another type, so it is derived
rather than re-extracted: for every `REFERENCES X→Y` where both ends are `Article`/`Annex`, both carry a
known instrument prefix, and those prefixes differ, emit `INTERACTS_WITH X→Y` tagged `derived: true`.
**22 bridges — 19 `Article→Article`, 3 `Annex→Article`.** Zero API cost, no cache invalidation. `DONE`.

Also widened `ALLOWED_ENDPOINTS["INTERACTS_WITH"]`'s head to include `Annex` — validation-only, and it
cleared 11 pre-existing endpoint violations (241 → 230). `DONE`.

**A bug in the fix, caught by the number not matching.** The first implementation checked only whether
the *head* carried an instrument prefix. A tail with no prefix then counted as "a different regulation,"
and the pass produced **38 bridges instead of 22** — 16 of them invented between an article and a bare,
un-namespaced `article 35`. It was caught only because 38 disagreed with the 22 measured during
planning. Both endpoints must be namespaced; there is now a test for it.

**`OPEN` — ontology v4, recorded and not executed.** Fixing the few-shot example is the real repair, and
it invalidates all 1,108 cache entries for a ~$24 re-extraction. The derived pass recovers the same
edges for free, so the prompt fix waits until something else forces a re-run. **The risk of deferring:
the derivation is a patch at load time, so any new corpus extracted with this prompt has the same hole.**

**What I learned.** **A count is not a shape** — and I had already written that sentence in this file
after Step 4, about this exact relationship type, and still had not measured the thing that mattered.
Writing the lesson down is not the same as applying it. What finally worked was letting a *different
artefact* interrogate the graph: the eval set declares which edges each question needs, and checking
those declarations against reality asked a question no self-audit had asked.

**And the fix needed the same scrutiny as the bug.** A derivation rule that invents edges is exactly the
kind of change that can quietly manufacture support for whatever you were hoping to show. The only
reason the 16 fabricated bridges did not ship is that a number measured beforehand disagreed with the
number produced afterward.

---

# Three defects found while planning Phase 2, written down before they are fixed

These were found on 2026-07-31 while scoping the vector index, and they are recorded now rather than
when they are fixed. The reason is in this file's own header: an `OPEN` item that exists only in
someone's memory of a planning conversation is not tracked, it is forgotten. All three are in Step 5's
path and none is fixed yet.

> **All three closed 2026-07-31** during Step 5, and a fourth of the same family was found while
> closing them (see the section after this one). Numbers in `docs/metrics/vector-index.md`.

## 1. `Chunk` rejects 586 of 694 AI Act rows — **actually 1,000 of 1,108**

`src/schemas.py` declares `article: str | None` and `paragraph: str | None`. The chunker writes
integers (`"article": 1`). Pydantic v2 does not coerce int→str, so `Chunk.model_validate` **fails on
586 of 694 AI Act rows**.

> **Correction (2026-07-31).** The real figure is **1,000 of 1,108 rows — 90% of the corpus.** The
> "586 of 694" above counted the AI Act file only. GDPR articles are integers too, so **all 414 GDPR
> rows failed as well** and were never checked. The heading was written after measuring one file and
> describing it as the defect.
>
> This is the third time in this document the same thing has happened — the `INTERACTS_WITH` count
> that measured edges without measuring their endpoints, the histograms transcribed from a partial
> run, and now this. Confirming the part in front of you and reporting it as the whole is not a
> mistake this project has made once.

The 108 that pass are the annex chunks — and they pass only because Pydantic silently discards
`annex`, `annex_title`, `point` and `token_count` as extra keys. So the model "accepts" them by
throwing away exactly the fields that identify them.

**Why it matters.** `embed_chunks(chunks: list[Chunk])` is the declared Step 5 entry point. It cannot
be handed the real corpus at all. The failure is total and has been latent since the model was written.

**What caught it.** Constructing `Chunk` from actual corpus rows while planning, rather than reading
the model and the JSONL separately and assuming they matched. Nothing in the pipeline had ever done
this: `chunker.py` writes raw dicts via `json.dumps` and never constructs a `Chunk`, so the type has
never been exercised against the data it describes.

- ~~`OPEN` (since 2026-07-31)~~ → **`DONE` (2026-07-31)** — Field types corrected against the corpus,
  a `shape` validator rejects any row matching none of the three shapes, `extra="forbid"` makes a new
  chunker key fail loudly instead of vanishing, and `tests/test_chunks.py` round-trips **all 1,108
  rows** and compares the dump back to the source dict — so "accepted by discarding fields" fails too,
  which a validate-and-count would have waved through.

## 2. `schema.sql` would lose provenance for 202 of 1,108 chunks

`src/index/schema.sql` declares `article` and `paragraph` only. The corpus has **11 distinct keys in 3
disjoint shapes**: 906 paragraph rows, **108 annex rows** (`annex`, `annex_title`, `point`) and
**94 definition rows** (`definition`). Loading as written drops five keys and leaves the annex and
definition chunks unidentifiable — 18.2% of the corpus.

`article` is also declared `TEXT` where the data is integer, the same mismatch as defect 1.

**Why it matters.** Annex III is the high-risk list and Annex VI/VII are the conformity procedures —
the chunks a citation most needs to name precisely. They would be in the vector store as anonymous
text.

- ~~`OPEN` (since 2026-07-31)~~ → **`DONE` (2026-07-31)** — 11 explicit nullable columns with the
  corpus's real types, a `shape` column with a CHECK constraint, and a stored `citation_label`.
  `create_indexes()` is split from `ensure_schema()` so HNSW is built after the bulk load.
  `tests/test_embedder.py` asserts per-shape provenance survives the load rather than trusting it.
  Note `ensure_schema()` is the path that matters: the docker initdb hook fires only on an empty
  volume, so editing `schema.sql` on a container that has already started does nothing at all.

## 3. `entity_ids TEXT[]` is declared and never populated

`schema.sql` has had an `entity_ids TEXT[] DEFAULT '{}'` column since scaffolding. **No code anywhere
writes it and no code reads it.** It is a plan recorded as a schema and never built — the same pattern
as `docs/concepts/ontology.md`, a file referenced by ADR-0007 that has never existed in the worktree or
in git history.

The graph↔vector join runs through `chunk_id`, which is load-bearing on both sides and does work. So
`entity_ids` is not a gap in the design; it is a vestige of a different one.

- ~~`OPEN` (since 2026-07-31)~~ → **`DONE` (2026-07-31)** — Populated by inverting
  `resolve_corpus()["nodes"][*]["chunk_ids"]`, storing the resolved `canonical_name` so a value is
  usable directly as a Cypher parameter. **7,465 references across 1,107 of 1,108 chunks, 0 of which
  name a node absent from the graph.** The one chunk with an empty array is `gdpr-art70-para1`, the
  paragraph that never extracted — a self-consistency check that passed without being arranged.

**What I learned from all three together.** Every one of them is a **contract that both sides agreed to
and neither ever tested** — a Pydantic model describing a JSONL nobody validated against it, a SQL
schema describing a corpus nobody loaded, a column describing a relationship nobody populated. They
survived because each side was individually reasonable. This is the same shape as the
`cross_regulation` template written against a graph nobody had loaded, and it suggests the rule: **an
interface between two components is untested until something has actually crossed it**, and writing
both sides carefully is not a substitute.

---

# The fourth defect of the same family: a field the chunker computed and threw away

Found 2026-07-31 while loading the vector index, by an assertion that citation labels are unique.

**What happened.** `Chunk.citation_label` renders the user-facing locator — `AIA Art. 9(2)`,
`AIA Annex III(4)` — from the chunk's own fields. A test asserted the 1,108 labels are distinct. It
failed at 1,092: **25 chunks were sharing 11 labels.**

All of them were Annex VIII and Annex XI. Those two annexes are divided into sections whose point
numbering **restarts per section**, so Annex VIII has three different "point 1" — Section A is
registration by providers of Annex III high-risk systems, Section B by providers of Art. 6(1)
systems, Section C by deployers. `AIA Annex VIII(1)` named all three at once.

**Why it mattered.** These are registration duties that attach to *different actors*. A citation
resolving to the wrong one is not a formatting problem; it attributes an obligation to a party that
does not carry it. Annex VIII is also the annex that bridges to GDPR Art. 35.

**What caught it.** A uniqueness assertion over the whole corpus — not code review. The chunk ids
were correct all along (`aia-annex8-sectionA-point1`), which is exactly why nothing had noticed:
`annex_parser.py` computes the section, `chunker.py` passes it to `make_annex_chunk_id()`, and then
does not write it to the record. Every consumer that reads *fields* rather than parsing *ids* saw
ambiguity; every consumer that read ids saw none. The information was present, correct, and
unreachable.

`make_annex_chunk_id()` even carries a docstring saying the section exists because "without it those
ids would collide and silently corrupt the vector-index/graph join". The author understood the
hazard exactly, fixed it in the id, and did not notice the same hazard in the record.

**What I changed.**

- `chunk_annexes()` writes `section` as a field. `Chunk.section` carries it; `citation_label` nests
  it the way the eval set already nests a sub-point, giving `AIA Annex VIII(A)(1)`.
- Backfilled the two JSONL files by **re-deriving the annex rows from the source HTML** and merging
  only the new key, refusing to write if any other field differed. **0 mismatches over 108 annex
  rows**, so the change adds a key and alters nothing else. Deliberately not parsed out of the
  chunk_id — inferring the data from a key derived from it cannot detect a wrong key.
- No re-extraction: `cache_key()` hashes only model + prompt + text, and `user_prompt()` iterates a
  fixed key tuple. Verified before writing, not assumed.
- `tests/test_chunks.py` asserts the 32 sectioned rows, three specific labels, and global uniqueness.

- `OPEN` (since 2026-07-31) — **The extractor was never told the section either.** `user_prompt()`'s
  key tuple has no `section`, so Command A saw three Annex VIII "point 1" chunks with identical
  metadata and no way to distinguish them. The graph's Annex VIII nodes are as ambiguous as the
  citation labels were, and unlike the labels this one costs ~$0.70 of re-extraction to fix.

**What I learned.** The three Phase-2 defects were untested *interfaces*. This one is narrower and
worse: a value that was computed correctly, used correctly in one place, and dropped in another,
inside a single function. The lesson is not "test your contracts" — it is that **deriving a key from
a field and then not storing the field leaves the key as the only copy of the data**, and keys are
not meant to be parsed. If `make_annex_chunk_id` needs an argument to stay unique, the record needs
that argument as a column.

And the check that found it cost one line. `assert len(set(labels)) == len(labels)` over a whole
corpus is the cheapest class of test there is, and it is the third time in this document that a
whole-corpus assertion has found something every targeted check passed clean.

---

# The corpus could not be rebuilt from the code that built it

**Found 2026-08-02**, closing the pre-Phase-3 housekeeping. Not a Phase-3 defect — a Phase-1 one
that survived five phases because nothing ever looked.

**What happened.** `chunker.main()` built `chunk(parse(...)) + chunk_annexes(parse_annexes(...))`.
It never called `chunk_definitions()`, which sits 60 lines above it in the same file and is fully
implemented and correct. So AIA Art. 3 fell through `paragraphs()`'s no-numbered-div fallback and
came out as a single 2,619-word blob `aia-art3-para1`, instead of `aia-art3-def1 … def68`. GDPR
Art. 4: one blob instead of 26.

Re-running the documented command sequence produced **1,016 chunks. The corpus on disk has 1,108.**
The 92 missing rows are exactly the 94 `definition` chunks, less the two blobs that replaced them.

**Why it mattered.** Everything downstream is keyed on `chunk_id`. The 94 definition chunks are
extracted, resolved, loaded into Neo4j, embedded in pgvector, and cited by the eval set —
`aia-art3-def37` is a `TEST_CHUNK_IDS` pilot member and a gold chunk. Rebuilding the corpus from the
current code would have silently dropped every one of them and invalidated ~$24 of extraction whose
cache keys are hashed on chunk *text* that would no longer exist in that form.

It was also a live trap for the one deferred item: the fix for the annex `section` defect is
"re-extract 25 chunks", and anyone starting that by regenerating the corpus first would have
destroyed 94 others.

**What caught it.** Verifying a change I had already convinced myself was cosmetic. The task was to
stop `main()` writing a hardcoded `chunks.jsonl` and derive the filename instead — a two-line change.
The verification step was "run it and confirm the output is byte-identical." It was not: 627 rows
where 694 were expected. Had I checked only that the *filename* was now correct, which is all the
change was about, this would still be sitting there.

**What I changed.**
- `DONE` — `main()` now calls `parse_definitions()` + `chunk_definitions()` and splices the rows in
  source order via `splice_definitions()`, so `aia-art3-def1` follows `aia-art2-para12`. Both files
  now rebuild **byte-identical** to the stored corpus. The run summary prints the three shapes
  separately (`518 paragraph + 68 definition + 108 annex`) rather than one total, because a single
  total is what hid this.
- `DONE` — `tests/test_chunks.py::test_chunker_reproduces_the_stored_corpus` re-derives both files
  into `tmp_path` and compares bytes. This is the check that was missing, not another assertion
  about the corpus.
- `DONE` — `README.md` now shows both invocations with their required argument. The old line was
  `python -m src.ingest.chunker` with no path, which exits 2 — so the documented pipeline could not
  have been run end-to-end by anyone, which is presumably why nobody noticed the output was short.

**What I learned.** Every other test in `test_chunks.py` reads `data/processed/*.jsonl` and asserts
against it — 1,108 rows, all shapes, per-annex counts, unique citation labels, full round-trip. They
are good tests and all of them passed, because **they all validate the artefact against itself.**
None of them could see that the artefact was no longer reachable from the code that claims to
produce it.

This is the "interface neither side ever crossed" pattern again, but the two sides are further apart
than before: not a model and a file, but *a generator and its own output*. The stored corpus had
quietly become a hand-maintained asset. And note which safeguard would have missed it — the coverage
check still `OPEN` at the top of this file, "every top-level container produces at least one chunk,"
passes cleanly here. Article 3 produced a chunk. It produced one instead of 68. That is the third
time in this document that a presence check has been asked to do a counting check's job.

---

# Phase 3 Step 1: the templates survived, and the defect was in the fix

Written 2026-08-03, projecting relationship provenance out of the six Cypher templates.

**What happened.** Nothing broke. All six templates were rewritten to return the `source_chunk_id` of
every edge they traverse, and all six row counts came back identical — 60 / 169 / 1 / 1 / 4 /
1-path-2-hops, measured on the live graph before the change and again after. Against this document's
own base rate of **3 of 6 broken on first contact**, that deserves an explanation rather than a
victory lap, and the explanation is dull: the graph was brought up and the templates were run *first*,
so the rewrite was aimed at observed behaviour instead of assumed behaviour. That is precisely the
remedy the Step 4 entry above prescribed, applied on purpose.

**The one real defect was in the fix, and it was found by probing rather than by testing.** The
natural way to carry provenance is a map per edge — chunk id, `derived` flag, direction — collected
per row:

```cypher
collect(DISTINCT {chunk: pu.source_chunk_id, derived: pu.derived}) AS penalty_chunks
```

On a leg that matched, this is correct. On a **missed `OPTIONAL MATCH`** it returns

```
[{'chunk': None, 'derived': None}]
```

where collecting the bare property returns `[]`. `collect` drops nulls, but a map literal is never
null — only its values are. The result is a provenance list of length 1 containing a citation to
nothing, and **every reasonable check passes it**: it is a non-empty list, `if provenance:` is true,
"every row carries provenance" holds. Only citation validation two steps later would have caught it,
as a chunk id that resolves to no chunk — assuming it did not simply skip the null.

This matters here more than it would elsewhere because `enforcement_chain`'s OPTIONAL leg is the
*common* case, not the edge case: only **4 of 216** enforced obligations also carry
`PENALIZED_UNDER`. The failure mode would have been the default.

**What caught it.** A 40-line throwaway script run against the live graph before a single template was
edited, which checked the two Cypher claims the plan was reasoning about but had not observed: that
aggregating inside `collect()` leaves the row count alone, and what happens to a map literal on a
null leg. The first claim was confirmed and produced a bonus — the naive projection returned exactly
**24,428** rows, reproducing the historical defect number live. The second claim came back the
opposite of the way it was written down.

**What I changed.**

- `DONE` Every `OPTIONAL` leg collects the bare property (`collect(DISTINCT rel.source_chunk_id)`),
  never a map. Only `cross_regulation` uses the map form, and only because its single leg is
  mandatory — stated as rule 1 in the `cypher_templates` module docstring, next to the measured
  output, so the next person to "unify the provenance shape" reads why first.
- `DONE` `test_an_empty_optional_leg_collects_to_nothing` asserts `penalty_chunks == []` on an
  obligation discovered from the graph to have no `PENALIZED_UNDER`. The obligation is *discovered*,
  not hard-coded, because a hard-coded one that later gains a penalty edge turns the test green for
  the wrong reason — the `definition_of` mistake three phases ago, which passed because its probe
  term happened to be in an Article.
- `DONE` `test_aggregating_provenance_is_what_holds_the_row_count` runs the naive projection beside
  the real template and asserts **24,428 and 169** together, so the *reason* the count holds is under
  test and not just the count. A future "simplification" back to a projected column fails with both
  numbers in the message.
- `DONE` `derived` is surfaced only on the two templates that can traverse an ADR-0010 bridge, and
  `test_derived_is_confined_to_interacts_with` asserts `{INTERACTS_WITH: 22}` exactly — the narrow
  scope is licensed by a test rather than by a paragraph.
- `DONE` `validate()` in `src/query/graph_query.py` checks template name **and** the exact declared
  parameter set before a driver is opened, with `test_run_template_validates_before_it_opens_a_driver`
  passing raw Cypher as a template name. ADR-0002 was a prose commitment for three phases; it is now
  a function with a test.

**What I learned.** This document's recurring lesson is that a presence check gets asked to do a
counting check's job. This is the same lesson with the subject changed: **a non-empty container is not
evidence of content.** `[{chunk: null}]` is `[]` wearing a costume, and the costume is convincing
enough that the test I would naturally have written — "every row carries provenance" — would have
passed on it, which is why that test is in the file *alongside* the one that checks the empty case
rather than instead of it.

The second thing is cheaper to state and harder to do: the plan for this step contained a wrong claim,
in writing, that was corrected by twenty minutes of probing a live database before any code was
written. The claim was reasonable and it was wrong. **A design written against a database nobody
queried is still a design written against a database nobody queried, even when the person writing it
knows that is a failure mode of this project** — the fix is not knowing, it is running the query.

---

# Phase 3 Step 2: the linker's defects were in the punctuation, and the metric's were in the denominator

Written 2026-08-03, building `link(question) -> list[str]` — the first code in this repo that turns a
question into a Cypher parameter.

**What happened.** Two defects, both of which returned plausible results, and one bad measurement that
would have been reported as a finding.

The linker is a longest-match sweep over `normalize()` plus an alias index. A throwaway prototype run
against all 23 eval questions *before* the module was written linked 23 of 23 and looked healthy. Two
things in it were wrong, and both were visible only in the output detail, never in the counts:

1. **`_trim` peels `" .,;:"` and not `?`.** No node name ends in a question mark, so the corpus-side
   normaliser never needed one. Every question does. `"...does it require a notified body?"` failed to
   match `notified body`, fell back to the bare token `notified`, and linked the obligation
   **`notify use of real time remote biometric identification system`** — an alias hit on a completely
   unrelated node. The question still linked five entities and still "worked".
2. **`normalize()` deletes apostrophes**, which turns a possessive into a plural that no fold covers.
   `"the GDPR's highest fine tier"` normalised to `gdprs`, matched nothing, and `ag-003` linked **no
   instrument at all** — a GDPR question that never reached the GDPR node, while still returning
   `['infringement']` and counting as linked.

**The fix for the second had a defect of the same family.** Applied to multi-token spans, the
possessive strip let `"the controller's"` — stripped to the alias `the controller` — shadow
`"controller's representative"` one token to the right, because the sweep commits leftmost-longest. A
fix that only ever *added* matches at a given position could still *remove* one at the next position.
Caught by a test written from the corpus rather than from the question set: 122 surfaces end in `s`
and land on a different node if the letter comes off.

**What caught it.** Printing the span that produced each link, not just the link. Every one of these
is invisible in `link()`'s output and obvious in `link_detailed()`'s. The counts — 23 of 23 linking,
~3.6 nodes per question — were identical before and after both fixes.

**The measurement was worse than the code.** The plan specified gold = the gold chunks' `entity_ids`,
explicitly to avoid new hand-labelling. Measured before use, that set averages **18.9 nodes per row**
and reaches **75** for `ag-001`, against a linker that emits 3.6. Recall against it is capped far below
1.0 by arithmetic, so the 10% it reports is not a fact about the linker. Worse, the five rows scoring
zero are all penalty questions, and their gold sets are article-citation nodes plus one very long
penalty name (`administrative fine up to eur 20 000 000 or 4 % of total worldwide annual turnover`) —
strings no question contains. The linker links what a question *says*; the gold set holds what a chunk
*asserts*; on those rows the vocabularies are disjoint. `ag-003`'s links are the correct reading of
the question and score 0.

**What I changed.**

- `DONE` `?!` stripped at the linker boundary, in `_surfaces()`, **not** in `_trim` — widening the
  corpus normaliser would move node keys, which is ADR-0009's Correction in reverse.
  `test_a_trailing_question_mark_does_not_truncate_a_span` asserts both halves: that `notified body`
  links, and that nothing starting `notify use of` does.
- `DONE` The possessive form is tried *after* the plain one and on single-token spans only.
  `test_the_possessive_form_is_only_ever_a_fallback` asserts the ordering directly rather than
  sampling behaviour, because ordering is the guarantee that makes it safe.
- `DONE` The plural fold is rebuilt over alias surfaces as well as canonical names — 18 merges to 124
  — using the resolver's own `_plural_map` rather than a second copy of the rule. Two tests license
  it: `deployers` folds (`ag-001` needs it), and `premises`/`analysis`/`business`/`bias`/`practices`
  do not, because their singulars are unattested. A third asserts no fold swallows a node that owns
  its own name.
- `DONE` Precision is the reported headline, recall is printed with its denominator explained **in the
  CLI output**, not only in the doc — the number and its caveat travel together or the caveat is lost.
- `DONE` The instrument split is computed, not asserted: 14 of 36 misses are `Regulation` nodes, so
  precision is 52% as specified and 64% excluding them, and both are printed.
- `OPEN` A hand-labelled per-question entity set. The penalty-row finding above is the argument for it;
  the plan deferred it to avoid hand-labelling and that trade is now measured rather than assumed.
- `OPEN` Leftmost-longest *cover* instead of leftmost-longest match. Would fix the shadowing case and
  multi-word possessives (`"the European Commission's report"`) together. No eval row exercises it.

**What I learned.** The recurrence tracker's oldest row is "confirmed the part I looked at, assumed it
covered the whole." This is that row applied to punctuation: `_trim` was verified against the corpus,
correctly, and the corpus is a set of legal names that never end in `?`. The defect was not in `_trim`
— it was in reusing a component **at a boundary its inputs had never crossed**, which is the same
shape as `Chunk` vs the JSONL nobody validated against it and `schema.sql` vs the corpus nobody
loaded. Reuse carries the original's assumptions along with its code, and questions are not corpus
text.

The second lesson is about measurement, and it is new here: **a denominator can be wrong in a way that
looks like a result.** A 10% recall figure is a perfectly plausible bad-linker number. Nothing in the
output distinguishes it from a bad linker except computing what the denominator actually contains
first — which took one command and would not have been run if the plan's metric had been adopted as
written.

---

# Phase 3 Step 3: the model used two of its three labels, and the rule I was told was strong fired never

Written 2026-08-03, building the query router — the stage the roadmap most specifically predicted the
answer for.

**What happened.** Command R7B, few-shot prompted with two examples of each of `graph`, `vector` and
`both`, returned `both` for **0 of 23** questions. Not rarely. Never. It answered `vector` 15 times and
`graph` 8 times, with zero unparseable outputs and zero API errors — a clean, confident, well-formed
run that had silently stopped being a three-way classifier.

**Why it mattered.** 9 of the 22 scored rows are gold `both`, so the missing class capped R7B at 13/22
before a single judgement was made. It finished at **10/22 (45%)**, below the majority-class constant
`always-vector` at **13/22 (59%)**. A model that cannot beat "always guess the most common label" is
not a cheap classifier, it is an expensive coin.

**What caught it.** Not the accuracy number — 45% on a three-way task reads as a weak-but-working
classifier, which is exactly what I would have written down. It was printing the **distribution of raw
outputs** next to the accuracy. One `collections.Counter` over the artifact showed `{'vector': 15,
'graph': 8}` and the shape of the failure was immediately a different thing from its size.

This is the recurrence tracker's "a count mistaken for a shape", third occurrence, and the first where
the count was of a *model's* behaviour rather than the data's. `INTERACTS_WITH` at 130 edges with 0 of
them article-level is the same defect: an aggregate that looks like partial success sitting on top of
a distribution with a whole category missing.

**What I changed.** I gave the model a fair second attempt, and the rule for what counts as fair is
worth stating because it is easy to cheat here. The collapse is visible from the **output distribution
alone** — no gold label is needed to notice a three-way classifier using two classes — so repairing it
is a legitimate fix rather than fitting to the answers. Rewriting the prompt as an ordered decision
procedure that tests `both` **first** and tells the model it is the most common case produced: `both`
still **0 of 23**, accuracy still 10 of 23, the gate failure unchanged, and one answer newly
unparseable. Recorded in ADR-0012, including the rejected prompt verbatim.

**The second finding, and it is about my own plan.** The plan for this step asserted: *"A question that
links to zero nodes has no graph path available; that alone is a strong rule."* It fires on **zero of
23 rows**, because Step 2 had already measured the link rate at **23 of 23** and written it into
`docs/metrics/query-path.md` — the number that falsified the rule was in the repo, in the file the plan
cites, before the plan was written. The rule that actually carries those rows is a different one: a
question can link to five real nodes and still have nothing to traverse from, because `Regulation`,
`DefinedTerm`, `Authority` and `Penalty` are not parameters any template declares.

- `DONE` R1 is kept as a genuine request-path guard and `test_r1_is_inert_on_this_eval_set` asserts the
  inertness, so a linker regression makes it load-bearing **loudly** rather than silently.
- `DONE` The adoption criterion — accuracy, a ≤1-row tie band, and a hard gate on
  `graph_traversable: false` rows — was written into the module docstring **before** the sweep ran.
  Both the tie band and the gate ended up load-bearing, and deciding either afterwards would have been
  indistinguishable from picking the winner first.
- `DONE` Two constant arms (`always-vector`, `always-both`) are in the table permanently. Without them
  R7B's 45% is "worse than rules"; with them it is "worse than not classifying", which is the true
  statement and the one that decided the ADR.
- `DONE` The rules' 95% is labelled **in-sample** everywhere it appears — module docstring, CLI output,
  metrics doc, ADR — because the rules were authored with all 23 gold labels visible and R7B saw none
  of them. A test asserts no few-shot example is an eval question; nothing can assert the rules were
  not fitted, so it is disclosed instead.
- `DONE` `failures.jsonl`'s lesson (§3 above) is finally acted on for one file: `decision_log.append`
  opens `"a"`, flushes and fsyncs per row, and a test writes two runs and asserts the first survives.
- `OPEN` Everything here is in-sample. The 100-row eval set is the only thing that resolves it, and the
  honest prior is that 95% falls.
- `OPEN` `th-004` is a recorded rules miss, unrepaired. The obvious fix moves `oos-002` the wrong way,
  and one row is not enough evidence to add a rule.

**What I learned.** I have been writing "measure the thing before building on it" in these notes for
five phases, and I still only measured the *aggregate*. The distribution was one line of code away the
whole time and it is the line that produced the finding. An accuracy figure tells you how often a
classifier is wrong; only the output distribution tells you whether it is a classifier.

The smaller lesson is sharper because it is self-inflicted: **the plan asserted a rule was strong and
the number disproving it was already in the repo, in the file the plan cited two sections earlier.**
Deriving a design from a document is not the same as checking it against the document, and the gap
between those is where the last three of these entries have lived.
