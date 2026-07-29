# Honest failure notes (RCA-style)

Record measured failure rates and debugging journeys here as they surface.

Each entry has the same shape: **what happened → why it mattered → what caught it →
what I changed → what I learned.**
A change is marked `DONE` only if code in this repo enforces it.
`OPEN` means I plan to do it but haven't yet. Doing something once by hand is not `DONE`.

## Measured rates

- Extraction Pydantic-validation failure rate: _TBD_
- Entity-resolution false merges / misses: _TBD_
- Router misclassification rate: _TBD_
- Citation-validation rejection rate: _TBD_
- Benchmark surprises (where the expected accuracy curve did not materialize): _TBD_

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
- `OPEN` — Store the expected counts as a test fixture, not just as prose in this file, so a
  silent collapse or explosion breaks the build.

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
- `OPEN` — The tests only assert that the two new types are *accepted by the schema*. Nothing
  checks extractor **behaviour** — that a permission-bearing chunk yields no `Obligation`. A
  regression in the prompt would be schema-valid and silent, exactly like the original bug. Needs
  a fixture-based test on `gdpr-art6-para1` + the control chunk.

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