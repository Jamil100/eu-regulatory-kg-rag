# ADR 0014: Cap graph statements with the constant; the budget is not the lever, prompt collapse is

- **Status:** Accepted
- **Date:** 2026-08-05
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 3 — Step 6, context assembly, generation, citation validation

## Context

`docs/metrics/query-path.md` §Open named this "the largest open item on this
path":

> The graph path emits 2,886 statements over 9 questions — ~320 per question —
> and Step 6 cannot put that in a prompt. Gold coverage is there; precision is
> not. Nothing in this step ranks statements, because there is no score to rank
> them by.

Five arms were built and measured against each other and against an uncapped
ceiling, the way ADR-0012 and ADR-0013 did it: `uncapped`, `first` (the
constant), `roundrobin`, `anchor` (free, deterministic) and `rerank` (the only
one that spends). The adoption criterion was fixed before any arm was written:
**gold retention against `oracle_primary`**, with ADR-0004's ±2-chunk resolution
binding, as it has bound every measurement on this eval set since.

## Decision

**`ADOPTED_BUDGET = "first"`, `DEFAULT_BUDGET_N = 50`.**

It is not the arm with the best retention. It is third of four. It is adopted
because it is the only arm that produced a scored answer on all 23 rows.

## The pre-registration, computed before any arm existed

`python -m src.answer.answer_path --prereg` — $0.00, no API key, Neo4j and
Postgres only. Computed by `preregister()` and summed by `scoreboard()`, with **no
literal of any oracle anywhere in `src/`**; the published values live in
`tests/test_answer_path.py` as assertions. That is the direct remedy for the
recurrence Step 5 logged against itself, where the ceiling and oracle were
computed by scripts in a temp directory and transcribed into `src/`, so
`test_rules_reaches_the_oracle` compared a constant to itself.

### The three oracles are equal, and the step plan predicted they would not be

| oracle | routed (10 rows, 35 gold) | scored (9 rows, 32 gold) |
|---|---|---|
| `oracle_provenance` — every chunk any returned row asserts | 25 | **24** |
| `oracle_shown` — chunks rendered into a statement's own text | 25 | **24** |
| `oracle_primary` — `chunk_id` alone, what a `Citation` carries | 25 | **24** |

The step plan's second premise was that ADR-0013's headline was "scored on a set
that cannot reach a citation": `path_to_prose` sets `chunk_id = chunks[0]`, the
lexicographically smallest of a statement's provenance, and the rest survive only
as label text. That description of the code is correct. The inference from it is
**false**, and only a measurement could have said so.

With 389 to 642 statements per routed row, every gold chunk anywhere in the
provenance union is also the lexicographic minimum of *some* statement's own
provenance. The `ContextDoc` boundary never cost a gold chunk. `24 of 32` was
reachable by a citation all along.

The 9-row figure reproduces ADR-0013's `24 of 32` **exactly**, through a
different code path — `graph_search` → `ContextDoc.provenance` here, against
`provenance_of` over raw rows there. That agreement is the cross-check that the
two steps describe the same graph; `test_the_scored_subset_reproduces_adr_0013s_twenty_four_of_thirty_two`
holds it. The statement counts reconcile the same way: 3,310 over 10 routed rows
here, 2,886 over 9 scored rows in `query-path.md`, and the difference is exactly
`3h-002`'s 424.

### Distribution, and what the budget is capping

| row | calls | statements | distinct provenance | ~tokens |
|---|---|---|---|---|
| `3h-001` | 3 | 642 | 218 | 14,439 |
| `th-001` | 2 | 641 | 217 | 14,425 |
| `3h-002` | 2 | 424 | 140 | 9,281 |
| `ag-001` / `xr-002` / `th-004` | 2–3 | 389 | 162 | 8,803 |
| `th-002` | 2 | 316 | 146 | 7,268 |
| `ag-002` | 1 | 109 | 48 | 2,290 |
| `xr-001` | 2 | 10 | 7 | 223 |
| `th-003` | 1 | 1 | 1 | 22 |

3,310 statements, ~74,357 tokens uncapped; mean **22.3**, max **63** tokens per
statement.

**The token argument for N=50 does not survive its own measurement.** 14,439
tokens is the worst row against Command A's 256k context, so capacity was never
the constraint. N=50 is retained anyway, because it is what was pre-registered
and because the argument that survives is a different one — 642 near-identical
statements is not evidence — but the honest record is that the number was chosen
against a constraint that turned out not to bind. Adjusting it after seeing the
retention curve is exactly what pre-registration exists to prevent, so it was not
adjusted. The curve at other N is published below and in §Open.

### Discriminating power, stated before measuring

At N=50, **2 of 10 rows carry 6 of 35 gold and no arm can differ on them**
(`th-003` with 1 statement, `xr-001` with 10). `first` and `roundrobin` are
identical by arithmetic on the two one-call rows (`ag-002`, `th-003`). So 8 of 10
rows discriminate at all, and the discriminating denominator is published beside
the total rather than behind it.

## Retention: four of five arms decided for $0.00

Retention is a pure function of which statements survive the cap, so it needs no
generation. This is a **reduction of the plan's spend table, not a shortcut**:
the plan's own adoption criterion is retention, and measuring it without paying
for generation measures it on all 10 routed rows at every N rather than on one N.

Gold retention, of 35 over the 10 routed rows:

| N | `uncapped` | `anchor` | `first` | `roundrobin` |
|---|---|---|---|---|
| 5 | 25 | 3 | 3 | 2 |
| 25 | 25 | 3 | 3 | 3 |
| **50** | **25** | **10** | **8** | **4** |
| 100 | 25 | 18 | 17 | 14 |

Three readings, in descending order of importance:

1. **The budget costs 15 of 25 reachable chunks; the best-to-worst spread
   between capped arms is 6.** The cap is the lever. Which statements you keep
   inside it is nearly noise by comparison, and no amount of smarter ranking
   closes a gap that size.
2. **`anchor` is +2 over the constant, which is exactly ADR-0004's declared
   resolution.** The house rule (`reranker.RESOLUTION_CHUNKS`, and the CAUTION
   `reranker._report` prints) reads `|delta| <= 2` as *no measured difference*.
   +2 at N=50 and +1 at N=100 do not clear it. Anchor did not earn an adoption.
3. **`roundrobin` is −4, which does clear it.** The one arm with a structural
   reason to beat the constant — `obligations_for_role('provider')` renders 210
   statements, so a first-N cap can spend the whole budget inside one call — is
   the arm that measurably loses to it. Interleaving spreads the budget evenly
   across calls, and the calls are not evenly worth reading. A publishable
   negative result, kept here rather than deleted, per ADR-0012's precedent.

## Generation: the finding is not about the budget at all

One arm per invocation, 23 rows each, routes replayed from the eval set's gold
labels and passages replayed from `eval/rerank-eval.jsonl` so the vector half
costs $0.00.

| arm | rows scored | gold cited (own denom.) | gold cited (common) | cost | p50 |
|---|---|---|---|---|---|
| `first` | **23 of 23** | 28 of 51 | 24 of 36 | $0.1437 | 3,762 ms |
| `roundrobin` | 23 of 23 | 23 of 51 | 23 of 36 | $0.1416 | 4,490 ms |
| `anchor` | 22 of 23 | 28 of 47 | 24 of 36 | $0.1329 | 4,583 ms |
| `rerank` | 22 of 23 | 25 of 40 | 25 of 36 | $0.2060 | 4,617 ms |
| `uncapped` | 22 of 23 | 35 of 47 | 25 of 36 | $0.3947 | 4,846 ms |

### The denominator trap, and it is unresolvable on this eval set

Each arm excludes its own `MAX_TOKENS` rows and **they are not the same rows**.
So "28 of 51" and "28 of 47" were never measured on the same population, and
reading them as a tie is the defect `template_selector._report` shipped once as
`Ceiling 10 of 9`.

Imposing a common denominator fixes the comparison and destroys it. The rows
every arm scored are 21, carrying 36 gold — because the excluded rows are
`ag-001` and `th-001`, two of the three largest graph rows, which is **exactly
where a budget can bite**. On that set all five arms land within 2 chunks of each
other, inside the resolution, and the table says nothing.

Both denominators are published, neither is preferred, and the retention curve —
which has no exclusions, because it needs no generation — is what the adoption
rests on.

### What actually breaks: `<co>` markup leaking into the answer

Three rows truncated, and all three share one cause. Command A emits its own
citation training format — `<co>…</co>` — into the answer **text**, and once it
starts it does not stop before `max_tokens`.

| arm / row | documents | markup begins at | structured citations | gold cited |
|---|---|---|---|---|
| `anchor` / `th-001` | 55 | **13%** | 7 | **0** |
| `uncapped` / `th-001` | 646 | 94% | 119 | 0 |
| `rerank` / `ag-001` | 50 | 97% | 37 | 4 |

The three are not equally bad and averaging them would hide the one that
matters. `uncapped` and `rerank` produce near-complete answers whose final bullet
gets wrapped and then runs out of tokens. **`anchor` collapses at 13% into
malformed `<co<co<co` repetition and cites no gold at all.**

**It is not a document count, and `ag-001` isolates that cleanly.** Both `rerank`
and `first` sent it **exactly 50 documents**, drawn from the same 389 statements.
`rerank` truncated; `first` returned `COMPLETE` with 50 citations and the same 4
gold chunks. Same row, same count, same candidate pool — only the selection
differs. `th-001` says the same thing from the other direction: 55 documents
collapse under `anchor`, and 405 documents are clean under `first` (probed live:
`COMPLETE`, 16 citations).

The one thing that tracks it is **document monotony**. Profiling `th-001`'s
50-document sets by sentence frame:

| arm | documents sharing the dominant frame |
|---|---|
| `anchor` | **48 of 50** (`applies to`) |
| `roundrobin` | 29 of 50 |
| `first` | 28 of 50 (`imposes:`) |
| `uncapped` | 332 of 641 |

Every arm that *ranks* concentrates near-duplicates, because near-duplicates are
what the graph path produces in bulk and a ranker's job is to put the most
relevant ones first. The unranked constant does not. That is the whole result:
**ranking graph statements is actively harmful here, and the reason is a
generation failure rather than a ranking failure.**

The step plan predicted a related effect from the other end — that a
cross-encoder would score 210 renderings of `<obligation> applies to provider.`
near-identically, making `reranker.py:211`'s `index` tiebreak the real selector.
**That is falsified.** Measured on `th-001`'s 641 statements: spread of **0.222**
across the top 50 (0.696 → 0.474), 7% of the top 60 sharing a score to 4 dp, and
exactly **one** document within 1e-4 of the boundary. The cross-encoder
discriminates fine. It discriminates *toward* the monotony that breaks
generation.

## Signature corrections

Recorded as corrections rather than applied silently, the treatment ADR-0011 and
ADR-0013 used.

**`ContextDoc.provenance: list[str]`.** The ≤3 chunks a statement's own text
named. Previously computed in `path_to_prose`, rendered into the text as labels,
then dropped. It is what makes the two-level citation fan-out possible — one
Cohere citation → N `DocumentSource`s → up to `MAX_PROVENANCE` chunks each — and
what makes the three oracles computable at all. It did **not** rescue a broken
number, because the number was not broken; see above.

**`Citation` gains `citation_label`, `source`, `document_id`.** All three were in
hand at construction and thrown away. `citation_label` is the string
`eval-questions.jsonl` grades on; reconstructing it downstream would be a second
code path producing it, which `retriever.py:170` and `graph_path.py:15-20` both
refuse. All three default, so the original four-field construction stays valid.

`Chunk` is byte-unchanged and `test_chunk_is_byte_unchanged_by_step_6` holds it
there.

## D2 reinterpreted: honoured as an ordering rule, not a deletion rule

`plan-phase-3-router-and-query-path.md:544-547` says "dedupe by chunk_id across
graph + vector paths". That was written when the stub was `path_to_prose(paths)
-> list[Chunk]` (`:99-101`), where one statement *was* one chunk and `chunk_id`
*was* a key. Step 0 broke that deliberately and ADR-0011 recorded it. Same
species as Step 4's "recall@5-after vs recall@10-before" — a plan instruction
invalidated by an earlier step of the same plan.

Today: two graph statements routinely share `chunks[0]` (124 chunks assert one
classification), so deduping graph documents on it deletes real statements; and a
GRAPH/PASSAGE pair sharing a chunk id carries two different texts, so dropping
the passage discards the only statute prose in the prompt. So: dedupe **within**
source, report the cross-path overlap, collapse nothing.

## Consequences

- **Route `graph` stays free of generation-side ranking cost.** `rerank` is not
  adopted, so no rerank call is made on the graph half. `budget_rerank` stays in
  the module behind `ADOPTED_BUDGET`, measurable on the committed artifact's
  denominator without rebuilding anything — the treatment ADR-0013 gave
  `select_by_model`.
- **The citation-validation rejection rate is 0 of 23**, and so are the two
  checks that *can* fail. See `docs/metrics/answer-path.md` for why the first is
  near-tautological and the other two are not.
- **`max_tokens=800` is now a load-bearing constant**, not a guard. Three of five
  arms hit it. Raising it does not fix the `<co>` leak; it buys more markup.
- **The graph path's headline moves nowhere.** ADR-0013's `24 of 32` stands
  as a *retrieval* number and is now confirmed citable. What a cited answer
  actually delivers at N=50 is **8 of 35** on the routed rows, and the gap
  between those two numbers is this step's finding.

## What this does not claim

- **Nothing here is answer accuracy.** A gold chunk appearing in a citation is
  not the answer being right. Phase 5's judge closes that gap.
- **The arms were compared at one N.** N=50 was pre-registered and not moved. The
  retention curve says N=100 roughly doubles every capped arm, and that was not
  swept.
- **n=23, and 8 rows discriminate.** Every figure is in-sample and one question
  moves a stratum by tens of points.
- **The `<co>` characterisation is a correlation over 5 arms × 23 rows plus 4
  hand probes.** Monotony tracks it and count does not; that is not the same as
  having isolated the cause.

---

## Amendment, 2026-08-16 — re-measured at 100 rows, and the trade is now priced

**Status of the decision: unchanged. Status of the evidence for it: weaker, and
now the leading suspect in a negative benchmark.**

Re-measured by `python -m src.answer.answer_path --prereg` over 52 routed rows
(151 gold chunks), against the 10 routed rows (35 gold) this ADR was decided on:

| arm, gold retained at N=50 | 23 rows | **100 rows** |
|---|---|---|
| `uncapped` | 25 of 35 | **61 of 151** |
| `anchor` | 10 | **26** |
| **`first` (adopted)** | 8 | **20** |
| `roundrobin` | 4 | **17** |

Two things changed and one did not.

**`anchor` now beats `first` outside the resolution.** The gap was +2 at N=50 and
+1 at N=100 — at or inside ADR-0004's ±2-chunk resolution, which is why this ADR
concluded "anchor did not earn the adoption". It is now **+6 at both N**, outside
the resolution at both. `test_anchor_now_beats_the_adopted_constant_outside_the_resolution`
records the inversion.

**The budget bites far harder than it did.** 33 of 52 routed rows now carry more
than 50 statements; at 23 rows, none of 10 did. The graph path emits a mean of 233
statements per routed row and a maximum of 670, against 12,121 in total.

**The adoption rationale is untouched.** `first` was adopted because it was the
only arm that produced a scored answer on every row — `anchor` and `uncapped` ran
to `MAX_TOKENS` on `th-001`. A budget that retains more gold and then truncates
the answer retains nothing. Nothing re-measured here contradicts that.

### Why this now matters more than it did

The Phase 5 benchmark (`docs/metrics/benchmark.md`) returned a **negative result**:
the hybrid lost to both vector baselines. The `hybrid-oracle` arm rules the router
out as the cause (its cost is −1 answer over 93 rows). This ADR's constant is the
next suspect on the list, because the hybrid entered that benchmark **retaining 20
of the 61 gold chunks its own graph path had already found**.

That is not a claim that `first-50` caused the null — the benchmark's own diagnosis
is that ~45% of answers are `partially_correct` for every arm including the pure
vector ones, which no graph budget can explain. But it is the one confound that can
be settled cheaply: **re-run the `hybrid` arm at `uncapped` and see whether anything
moves.** One arm, about $1. It is listed as the highest-value follow-up in
`benchmark.md` §Open.

Re-running the five-arm budget comparison at 100 rows would also settle whether
`anchor` still truncates, which is the only reason it lost. Neither was done here,
because doing it after seeing the benchmark's result — rather than before — is how
a post-hoc budget change gets mistaken for a finding.
