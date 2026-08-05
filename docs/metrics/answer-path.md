# Answer path metrics

Status: **`assemble → generate → validate` runs end to end on all three routes.
The graph-statement budget is measured, adopted and recorded in ADR-0014: the
constant wins, and not on retention. The three oracles the step was built to
separate came out **equal at 24 of 32**, reproducing ADR-0013 exactly and
falsifying the premise that the `ContextDoc` boundary was costing citations. What
does cost them is the cap — `first-50` reaches 8 of 35 gold against an uncapped
25 — and what breaks generation is neither: three of five arms truncate because
Command A leaks `<co>` citation markup into the answer, and every arm that
*ranks* graph statements triggers it. Citation-validation rejection **0 of 23**,
span defects **0 of 23**, uncited labels in prose **0 of 23**. All four refusal
rows behave correctly under the adopted arm.**
Phase 3 Steps 6 and 7 complete. **`POST /ask` is wired and measured live**: 23
of 23 questions served against real Neo4j and pgvector, **$0.1793 total, $0.0067
median per question, 3.4 s pooled p50**, every route non-zero and nothing
unpriced. The live figures land **1.47× (vector) and 1.17× (both)** above the
replayed sweep costs below, and **1.00× on `graph`** — which is the cross-check,
not a coincidence: route `graph` never runs the vector path, so there was nothing
for the replay to omit.

Sixth companion to `extraction-cost-and-findings.md`, `graph-load.md`,
`eval-set.md`, `vector-index.md` and `query-path.md`. That one measures what
*reads* the store; this one measures what turns what it read into a cited answer.
Split out because `query-path.md` is already 650+ lines and Step 7 still owes it
a table.

Regenerate with:

```bash
# The pre-registration. $0.00, no API key, containers up.
python -m src.answer.answer_path --prereg

# One arm per invocation. Needs an API key. ~$0.13-0.40 each.
python -m src.answer.answer_path --eval --refresh --budget first      -n 50
python -m src.answer.answer_path --eval --refresh --budget roundrobin -n 50
python -m src.answer.answer_path --eval --refresh --budget anchor     -n 50
python -m src.answer.answer_path --eval --refresh --budget rerank     -n 50
python -m src.answer.answer_path --eval --refresh --budget uncapped   -n 50

# Every number below, from the committed artifact. No DB, no key, no spend.
python -m src.answer.answer_path --eval

# Step 7: the same pipeline through the HTTP handler, per route.
python -m src.api.ask_eval --eval             # the live table, from the artifact
python -m src.api.ask_eval --eval --refresh   # re-run all 23 through /ask (~$0.18)
python -m src.api.ask_eval --question "..."   # one question through the endpoint
pytest tests/test_api.py
```

`scoreboard()` is pure and `eval/answer-eval.jsonl` is committed, so the last
command reproduces this document on a laptop with the containers down. That is
the property `tests/test_answer_path.py` exists to hold.

---

## Shape

Ten of 23 eval rows route to the graph (9 `both`, 1 `graph`); `3h-002` carries
`expected_fail` (ADR-0007), so **9 are scored** where a figure has to line up
with ADR-0013 and **10** where it does not. Both denominators appear below and
each is labelled. The other 13 rows route `vector` and never see a graph
statement.

| | |
|---|---|
| Routed rows / gold chunks | 10 / 35 (scored: 9 / 32) |
| Statements rendered, uncapped | **3,310** (2,886 over the 9 scored rows) |
| Distinct provenance chunks | 1 → 218 per row |
| Tokens per statement | mean **22.3**, max **63** |
| Whole uncapped graph half | ~74,357 tokens |
| Budget | `first`, N=50 (ADR-0014) |
| Passage slice | reranked top-5, `POST[5] = 27 of 51` (`test_reranker.py:48`) |

3,310 − 424 (`3h-002`) = 2,886, which is what `query-path.md` publishes. Two
metrics documents quoting two numbers that do not reconcile is how a metrics
directory stops being auditable, so the reconciliation is a test
(`test_the_statement_count_reconciles_with_query_path_md`).

---

## The three oracles

Computed by `preregister()` before any arm was written, summed by `scoreboard()`,
with **no literal of any oracle in `src/`** — the direct remedy for the
recurrence Step 5 logged against itself.

| oracle | routed (10 / 35) | scored (9 / 32) | what it permits |
|---|---|---|---|
| `oracle_provenance` | 25 | **24** | every chunk any returned row asserts |
| `oracle_shown` | 25 | **24** | chunks rendered into a statement's own text |
| `oracle_primary` | 25 | **24** | `chunk_id` alone — what a `Citation` carries |

**They are equal, and the step was built expecting them not to be.** The
reasoning was sound and the conclusion was wrong: `path_to_prose` really does set
`chunk_id = chunks[0]` and drop the rest, but with 389–642 statements per row
every gold chunk in the union is also the lexicographic minimum of *some*
statement's provenance. Nothing was lost at the boundary.

`24 of 32` reproduces ADR-0013 to the chunk, through a different code path.

Per row (routed denominator):

| row | gold | oracle | statements | distinct prov |
|---|---|---|---|---|
| `ag-001` | 11 | **11** | 389 | 162 |
| `th-001` | 4 | 4 | 641 | 217 |
| `th-002` | 3 | 2 | 316 | 146 |
| `xr-001` | 4 | 2 | 10 | 7 |
| `ag-002` | 2 | 2 | 109 | 48 |
| `3h-001` | 3 | 1 | 642 | 218 |
| `xr-002` | 2 | 1 | 389 | 162 |
| `th-003` | 2 | 1 | 1 | 1 |
| `3h-002` | 3 | 1 | 424 | 140 |
| `th-004` | 1 | **0** | 389 | 162 |

---

## Discriminating power, stated before the arms ran

At N=50, **2 of 10 rows carry 6 of 35 gold and no arm can differ on them** —
`th-003` (1 statement) and `xr-001` (10). `first` and `roundrobin` are identical
by arithmetic on the two one-call rows (`ag-002`, `th-003`).

So **8 of 10 rows discriminate**, and the discriminating denominator is published
here rather than left implicit, because a tie on a row where no arm *could* have
differed is arithmetic and not agreement.

---

## Gold retention by arm — the adoption, at $0.00

Retention is a pure function of which statements survive the cap, so it needs no
generation and no key. Of 35 gold over the 10 routed rows:

| N | `uncapped` | `anchor` | `first` | `roundrobin` |
|---|---|---|---|---|
| 5 | 25 | 3 | 3 | 2 |
| 25 | 25 | 3 | 3 | 3 |
| **50** | **25** | **10** | **8** | **4** |
| 100 | 25 | 18 | 17 | 14 |

**The cap costs 15 of 25 reachable chunks. The best-to-worst spread among capped
arms is 6.** The budget is the lever; what you keep inside it is nearly noise.

`anchor` is **+2** over the constant at N=50 and **+1** at N=100. ADR-0004
declared a ±2-chunk resolution for this eval set and `reranker.RESOLUTION_CHUNKS`
encodes it, so neither clears it — no measured difference. `roundrobin` is
**−4**, which does clear it: the one arm with a structural reason to beat the
constant measurably loses to it.

`rerank` is absent from this table because it is the one arm that needs a call to
produce an order; its retention is only observable through the sweep below.

---

## Generation

One arm per invocation. Routes replayed from the eval set's gold labels, passages
replayed from `eval/rerank-eval.jsonl`, so the vector half of the sweep costs
$0.00 and every arm sees identical passages.

| arm | rows scored | gold cited (own) | gold cited (common) | cost | p50 |
|---|---|---|---|---|---|
| **`first`** | **23 of 23** | 28 of 51 | 24 of 36 | $0.1437 | 3,762 ms |
| `roundrobin` | 23 of 23 | 23 of 51 | 23 of 36 | $0.1416 | 4,490 ms |
| `anchor` | 22 of 23 | 28 of 47 | 24 of 36 | $0.1329 | 4,583 ms |
| `rerank` | 22 of 23 | 25 of 40 | 25 of 36 | $0.2060 | 4,617 ms |
| `uncapped` | 22 of 23 | 35 of 47 | 25 of 36 | $0.3947 | 4,846 ms |

### Two denominators, neither of them good

**"Gold cited (own)" is not comparable across arms.** Each arm excludes its own
`MAX_TOKENS` rows and they are different rows, so 28 of 51 and 28 of 47 were
measured on different populations. Reading them as a tie is the defect
`template_selector._report` shipped once as `Ceiling 10 of 9`.

**"Gold cited (common)" is comparable and uninformative.** The 21 rows every arm
scored carry 36 gold, because the two excluded rows are `ag-001` and `th-001` —
two of the three largest graph rows, and *exactly where a budget can bite*. All
five arms then land within 2 chunks, inside the resolution.

There is no third option on this eval set. Both are published, neither is
preferred, and the adoption rests on the retention curve, which has no exclusions
because it needs no generation.

---

## What actually breaks: `<co>` markup in the answer text

Three rows truncated. All three share one cause: Command A emits its own citation
training format into the answer **text**, and once it starts it runs to
`max_tokens`.

| arm / row | documents | markup at | citations | gold cited |
|---|---|---|---|---|
| `anchor` / `th-001` | 55 | **13%** | 7 | **0** |
| `uncapped` / `th-001` | 646 | 94% | 119 | 0 |
| `rerank` / `ag-001` | 50 | 97% | 37 | 4 |

They are not equally bad. `uncapped` and `rerank` produce near-complete answers
whose last bullet gets wrapped and then runs out. **`anchor` collapses at 13%
into malformed `<co<co<co` repetition and cites no gold at all.**

**It is not document count, and `ag-001` isolates that.** `rerank` and `first`
both sent it exactly **50** documents from the same 389 statements. `rerank`
truncated; `first` returned `COMPLETE` with 50 citations and the same 4 gold
chunks. From the other direction, `th-001` collapses at 55 documents under
`anchor` and is clean at 405 under `first`.

What tracks it is **document monotony**. `th-001`'s 50-document sets, by sentence
frame:

| arm | share of the dominant frame |
|---|---|
| `anchor` | **48 of 50** (`applies to`) |
| `roundrobin` | 29 of 50 |
| `first` | 28 of 50 (`imposes:`) |
| `uncapped` | 332 of 641 |

Every arm that *ranks* concentrates near-duplicates, because near-duplicates are
what the graph path produces in bulk. The unranked constant does not.

**The reranker is not near-tied, which was the prediction.** The step plan
expected 210 renderings of `<obligation> applies to provider.` to score
identically, making `reranker.py:211`'s `index` tiebreak the real selector.
Measured on `th-001`'s 641 statements at 7 search units / $0.0140: spread
**0.222** across the top 50 (0.696 → 0.474), 7% of the top 60 sharing a score to
4 dp, and exactly **one** document within 1e-4 of the boundary. The cross-encoder
discriminates well. It discriminates toward the monotony that breaks generation.

---

## The three checks, one denominator

Adopted arm, 23 rows scored, no `MAX_TOKENS` exclusions:

| check | rate | |
|---|---|---|
| Citation-validation rejection | **0 of 23 (0%)** | near-tautological, see below |
| Span defect (`answer[start:end] != text`) | **0 of 23 (0%)** | real, and it can fail |
| Uncited label in prose | **0 of 23 (0%)** | real, and it is the one Phase 5 cares about |

**Why the first is near-tautological.** `generate()` only emits a `Citation` for
a source id it found in `AssemblyResult.by_id`, and `validate()` is handed those
same documents' chunk ids. Membership holds by construction. Publishing 0% as a
finding would be `failure-notes.md`'s *"A metric that looked like success"* for a
fourth time. `validate()`'s docstring names the four ways it can fire for real (a
`ToolSource`; an id the model generated rather than echoed; a duplicate id
collapsing the map; a citation surviving a regeneration against a rebuilt list),
and `generate` counts each of them into `dropped` so a silent zero and a
silently-swallowed twelve look different in the artifact.

**Why the other two are not.** `span_defects` compares generated text against
generated offsets and would fail on any `content_index` rebasing error —
`tests/test_generate.py` demonstrates one failing. `uncited_labels` scans the
prose for `AIA Art. …` / `GDPR Annex …` forms and checks them against what the
prompt actually showed; **a model copying a label out of a `+121 more` tail
would land here**, and none did. That is a result about `SYSTEM_PROMPT` rule 1,
on 23 rows.

**Regeneration never fired**, so the repair path has no live measurement. It is
exercised by a fake client that returns a bad citation first and a good one
second, including that the second request is not byte-identical to the first and
that both calls' cost is carried.

**`GenerationResult.dropped` is 0 across all five arms and all 115 rows.** Not a
citation was malformed, tool-sourced, or addressed to an id the model invented.
That is the number that would move before `validate()` ever could, which is why
it is recorded per row.

**`content_blocks` is 1 on every row.** So the `content_index` rebasing in
`generate._text_blocks` — the defect this step was most built around — never
fired against a real response. It is correct and it is pinned by
`test_two_text_blocks_have_their_offsets_rebased_into_the_joined_answer`, but on
this eval set it is insurance rather than a measured fix. It becomes live the day
a `thinking` block or a multi-part answer appears.

### Cross-path overlap: reported, never collapsed

**9 of the 10 routed rows have at least one chunk id present as both a GRAPH and
a PASSAGE document.** The phase plan's "dedupe by chunk_id across both paths"
would have deleted one of each pair on nearly every graph-routed question. They
are not duplicates — one is a rendered relationship, the other is the statute
text — and dropping the passage removes the only legislative prose from the
prompt. ADR-0014 records the reinterpretation; this is the number that says how
often it would have mattered.

---

## Refusal — n=4, split by `must_cite`, never averaged

Adopted arm. All four route `vector`, so refusal is a property of the reranked
top-5 plus `SYSTEM_PROMPT` alone and no budget arm can move it.

| id | stratum | `must_cite` | wanted | got |
|---|---|---|---|---|
| `oos-001` | out-of-scope | false | decline, **zero** citations | 0 citations, named the scope limit ✅ |
| `oos-002` | unanswerable | false | name the absence, **any** citation is wrong | 0 citations ✅ |
| `hn-001` | hard-negative | **true** | top tier, cited to a retrieved chunk | 2 citations, gold hit ✅ |
| `hn-002` | hard-negative | **true** | reject the premise, cited | 3 citations, gold hit ✅ |

Four of four under `first`. **This is n=4 and it is reported as n=4.** The
tension was stated as a hypothesis before the sweep — `oos-002` says any citation
is wrong while `hn-001`/`hn-002` say an uncited correct answer is only partial,
and one prompt has to produce both from the same route on adjacent questions —
and one prompt did. `plan-phase-3:622-624` flags averaging these into a single
"refusal rate" as a defect; `scoreboard()` emits them individually and
`test_the_four_refusal_rows_are_reported_individually_and_split_by_must_cite`
asserts no key ending in `refusal_rate` exists.

**And `oos-001` is 4 of 5, not 4 of 4.** The five arms sent it byte-identical
documents — it routes `vector`, so no budget touches it — and produced **five
different answers** at `temperature=0, seed=42`. Four declined with zero
citations. `uncapped` returned **11 citations** on a question the corpus cannot
answer. So the correct behaviour here is not a property of the prompt; it is a
property of the prompt about 80% of the time, measured once, on one row. This
extends `failure-notes.md`'s *"A determinism control that does not control"* row
from Command R7B to Command A.

---

## Cost

| item | calls | cost |
|---|---|---|
| Pre-registration (graph replay, rules selector) | 0 | $0.00 |
| Vector half, replayed from `rerank-eval.jsonl` + Postgres | 0 | $0.00 |
| `first` sweep, 23 rows | 23 | $0.1437 |
| `roundrobin` sweep | 23 | $0.1416 |
| `anchor` sweep | 23 | $0.1329 |
| `rerank` sweep (incl. ranking) | 23 + 10 | $0.2060 |
| `uncapped` sweep | 23 | $0.3947 |
| Live probes and threshold characterisation | ~10 | ~$0.21 |
| **Total** | **~158** | **~$1.24** |

Against the plan's ~$1.30 estimate.

Per question under the adopted arm, from the artifact. Sweep costs, so the vector
half is replayed at $0.00 for `graph`-routed rows and the embed+rerank round trip
a live `/ask` would pay is absent — Step 7 owes the live figure.

| route | n | cost, median (min–max) | p50 latency | documents |
|---|---|---|---|---|
| `vector` | 13 | $0.0043 ($0.0027–$0.0079) | 2,702 ms | 5 |
| `both` | 9 | $0.0099 ($0.0026–$0.0109) | 5,321 ms | 6–55 |
| `graph` | 1 | $0.0084 | 9,364 ms | 50 |

The graph half remains **$0.00** in API terms: `rerank` was not adopted, so route
`graph` never became non-free. Its 9.4 s p50 is one row (`ag-001`) and is Neo4j
plus a 50-document generation, not an API tier.

### The live figure Step 7 owed (2026-08-05)

23 questions through `POST /ask` against live Neo4j and pgvector, one call each,
on the adopted arm. Nothing replayed: every row paid its own embed, rerank,
graph traversal and generation, plus routing, connection checkout, serialisation
and the decision-log fsync. **23 of 23 served, 0 failures, 0 unpriced rows.**

| route | n | cost, median (min–max) | latency p50 (min–max) | citations, median |
|---|---|---|---|---|
| `vector` | 14 | **$0.0063** ($0.0047–$0.0082) | **3,996 ms** (1,299–24,767) | 2 |
| `both` | 8 | **$0.0116** ($0.0046–$0.0129) | **3,291 ms** (2,607–20,076) | 6 |
| `graph` | 1 | **$0.0084** | **7,463 ms** | 50 |
| pooled | 23 | $0.1793 total, $0.0067 median | p50 **3,419 ms**, p95 20,076 ms | 4 |

**The `graph` row is the cross-check, and it reconciles exactly.** Live
$0.008435 against the replayed table's $0.0084 — the same number, because route
`graph` never enters the vector path and the replay therefore had nothing to
leave out. Where the replay *did* leave something out, the gap is the embed and
rerank round trip it skipped: **vector 1.47×** ($0.0043 → $0.0063) and **both
1.17×** ($0.0099 → $0.0116). A replayed cost table understating the live bill by
half on the most common route is the reason `:341` recorded the debt instead of
publishing the sweep figure as a per-query cost.

**Latency moved the other way, and the reason is the same one.** `both` came in
at 3.3 s live against 5.3 s replayed and `graph` at 7.5 s against 9.4 s. A
replayed row is not a faster row; the two numbers were measured under different
machine load on different days at n=8 and n=1. Nothing here supports a claim
that `/ask` is faster than the sweep — only that these are not the same
measurement, which is why they are printed as two tables rather than one.

**No per-route p95 is published, and that was pre-registered.** The ns are 14,
8 and 1. A p95 over 8 observations is the maximum wearing a percentile's name
and over 1 it is the observation; `scoreboard()` does not compute one, and
`test_no_per_route_p95_is_published` makes that structural rather than
editorial. The pooled p95 is computed once, over all 23 rows, and labelled
pooled.

**The pooled p95 of 20 s is a fact about the API key, exactly as
`query-path.md:429-434` says.** Three rows cleared 10 s — `sh-006` 24.8 s,
`xr-002` 20.1 s, `hn-001` 12.5 s. Over the other **20** rows the p50 is
**3,291 ms** and the p95 is **7,463 ms**, which is the figure to quote for the
handler.

**Two of those three rows are the same ids that stalled in Step 4** — `hn-001`
and `sh-006`, at 83.5 s and 82.4 s then against 12.5 s and 24.8 s now. Step 6's
correction (`query-path.md:436-451`) retracted the "not retry backoff"
conclusion and said re-running a sweep is what would settle it. This is a
partial re-run and it does not settle it: the same rows are still the slowest,
but by a quarter of the margin, which is consistent with retry backoff *and*
with a less contended key. **n=1 per row.** Recorded as a recurrence to watch,
not as a finding.

**Client-observed minus server-reported is 6.8 ms at p50** (max 16.2 ms), so
`AskResponse.latency_ms` accounts for essentially the whole request and the
handler's clock is not hiding a serialisation cost. Both columns are in the
artifact; `test_the_served_latency_is_bounded_by_what_the_client_observed`
asserts the ordering holds on every row, which is what makes them two
measurements rather than two guesses.

**Route agreement with the gold labels is 22 of 23**, and the one disagreement
is `th-004` — ADR-0012's recorded miss, left unrepaired because the repair moves
`oos-002` the wrong way. So the served ns are **14 / 8 / 1** where the gold
labels are 13 / 9 / 1. Pinned by name in
`test_the_per_route_ns_reconcile_with_adr_0012s_recorded_miss`, whose first
version asserted the two agreed — a claim ADR-0012 had already measured to be
false.

---

## Defects found on the way in

| Defect | Symptom | Fix |
|---|---|---|
| `_call.retry.statistics` is permanently `{}` | `attempts` was 1 by construction at **all three** call sites since tenacity 8.2.3 — the wrapper runs `copy = self.copy()` and assigns the copy's statistics to `wrapped_f.statistics`, leaving `wrapped_f.retry` as a controller that never executes | `_call.statistics` at all three sites; `tests/test_generate.py` asserts 1/2/4 attempts against 0/1/3 injected failures |
| `citation_options={"mode": "ACCURATE"}` | a 400 from `command-a-03-2025`: the SDK enum is the union over every Cohere model, not a contract with one | probed all five modes live; pinned `ENABLED`, the model's full citation pass |
| The plan's `LABEL_RE` excluded `)` | `[^\s,;)]+` stops *inside* the parenthesis, matching `AIA Art. 9(1` — 40 of the 41 distinct gold labels, wrong by one character | rewritten to match `Art. N`, `Annex R` and nested `(x)(y)` parts; asserted `fullmatch` against every label in the committed eval set |
| Each arm published its own denominator | 28 of 51 against 28 of 47, never measured on the same rows | `scoreboard()` computes a common denominator and the report prints both, labelled |
| The CLI crashed after the answer printed | a `↳` glyph against a cp1252 Windows console — after the money was spent | ASCII only in `_print_answer` |
| **(Step 7)** A pool that fails to open leaves its workers running | `couldn't stop thread 'pool-1-worker-0' within 5.0 seconds` on every start with Postgres down — `reconnect_timeout` defaults to **300 s**, so the worker retries a database that is not there for five minutes and the interpreter cannot join it at exit | `reconnect_timeout=POOL_OPEN_TIMEOUT`, `connect_timeout` in `kwargs`, and an explicit `close()` on the failed pool |
| **(Step 7)** The live route test asserted a route for a question nobody routed | the question was hand-written for the test, asserted `graph`, and the rules router sent it to `vector` — correctly, since nothing in its shape fires R3 | the live tests read questions out of `eval/eval-questions.jsonl` **by id** and assert the gold label first, so the expectation is the router's own |
| **(Step 7)** The per-route reconciliation test asserted the router is perfect | `taken == gold` — which ADR-0012 had already measured to be false at `th-004`, so the test would have gone red on correct behaviour it documented itself | the known miss is pinned **by name**; an unnamed disagreement is what fails |

---

## Open

- **The `<co>` leak is characterised, not explained.** Monotony tracks it across
  5 arms × 23 rows plus 4 hand probes; document count does not. That is a
  correlation. What would settle it is a controlled sweep over synthesised
  document sets at fixed count and varying frame diversity, which is cheap and
  was not run.
- **`max_tokens=800` is load-bearing and untested at other values.** Raising it
  does not fix the leak — it buys more markup — but nothing here measures where
  the truncation would land at 2,000.
- **N was pre-registered at 50 and the curve says 100 is better.** Every capped
  arm roughly doubles: `first` 8 → 17, `anchor` 10 → 18. N was deliberately not
  moved after seeing that, because moving it is the tuning pre-registration
  exists to prevent. A step that can afford a sixth sweep should run N=100 and
  see whether the `<co>` collapse follows.
- **The token argument for N=50 did not survive measurement.** 14,439 tokens is
  the worst row against a 256k context, so capacity never bound. The argument
  that survives — 642 near-identical statements is not evidence — is a quality
  claim this eval set cannot test.
- **`th-004` reaches oracle 0.** Its one gold chunk is in no statement the graph
  path renders, so no budget, arm or N can ever cite it. It is a template-library
  limit, the same one ADR-0013 attributed to `REFERENCES` and `LISTED_IN`.
- **Retention is not accuracy.** A gold chunk in a citation is not a right
  answer. Phase 5's judge closes that gap, and `sh-003`'s live answer is the
  warning: it stated the GDPR Art. 9(1) prohibition and omitted the Art. 9(2)
  exceptions, which its own `grading_rule` calls *"legally misleading"* — with a
  clean citation and a clean span.
- **Refusal is n=4 and one of the four is 4-of-5 under repetition.** The 100-row
  set is what would make any of this a rate.
- **Nothing measured the `both` route's ceiling as a union.** `oracle_primary`
  (24 of 32 graph) and `POST[5] = 27 of 51` (vector) overlap on chunk ids by
  construction, and no figure here reports the size of that overlap.
- **The rejection rate is 0% and would stay 0% under most real defects**, because
  `generate` drops a bad citation before `validate` sees it. The number that
  would move is `GenerationResult.dropped`, which is recorded per row and is 0
  across all five arms — worth watching rather than publishing.
- **Two non-refusal rows came back with zero citations** (Step 7). `oos-001` and
  `oos-002` are refusals and 0 is correct for both. `sh-005` and `th-004` are
  not: they are a single-hop Annex III question and the two-hop row ADR-0012
  misroutes, and both produced prose with nothing attached. `ask-eval.jsonl`
  records the count and not the reason, because `AskResponse` carries no
  diagnostics — `answer_path --question` on those two ids is what would say
  whether the documents were wrong or the model declined to cite them.
- **`cost_usd: float | None` has a null arm that no live route can reach.**
  Every route prices today because `RERANK_PRICE_PER_SEARCH` is a number, and it
  is the one rate in this repo with no first-party source. The arm is exercised
  by monkeypatching the constant back to `None`
  (`test_cost_usd_serialises_as_null_when_a_component_is_unpriced`) rather than
  by waiting for the aggregator figure to be withdrawn.
- **The decision-log write is on the request path and is not separately timed.**
  `decision_log.append` does `flush()` + `os.fsync()` per row inside the
  handler's own clock. At a 3.4 s p50 it is not visible, which is the argument
  for keeping it; it is also the reason nothing here can say what it costs. A
  step that wants the number should time the `finally` block, not infer it.
- **`/ask` is measured single-threaded.** The sweep issues one request at a time
  through `TestClient`, so `max_size=4` on the pool, the shared `cohere.ClientV2`
  and the shared Neo4j driver have never been under concurrent load. Nothing
  here is evidence about throughput, and the pool exists on the argument that a
  `def` handler runs in a threadpool rather than on a measurement that it needs
  to.
- **The p95 is pooled over three routes with different work in them.** 20 s
  mixes a 5-document vector answer with a 50-document graph one. It is published
  because the per-route ns cannot carry a p95, not because pooling them is
  meaningful; the 100-row set is what would let each route have its own.
