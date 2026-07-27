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
