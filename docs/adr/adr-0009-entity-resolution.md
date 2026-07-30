# ADR 0009: Resolve entities deterministically; treat embedding similarity as candidates, not merges

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 1 — entity resolution, after the full-corpus extraction

## Context

The full corpus produced **7,466 entity mentions across 3,485 distinct (type, name) nodes**. The
roadmap's design for this stage is: normalize → exact match → Embed v4 cosine above a tuned
threshold (starting at 0.90) → merge, tuned on ~30 hand-labelled pairs.

Two things about the actual data changed that design.

### 1. Type collisions are invisible to resolution, so they need their own pass

Resolution compares within a type. The audit found **66 names carrying more than one type** — `AI
system` is both `DefinedTerm` and `SystemType`, `Member State` spans `ActorRole`, `Authority` and
`DefinedTerm`. These are not resolution failures that a better threshold would catch; they are
outside what resolution can see at all.

Most were a side effect of adding `DefinedTerm` in ADR-0008: Art. 3 *defines* "AI system", so the
definition chunk types it one way and every other chunk types it another.

**Order is load-bearing.** Taken alone, `Member State` resolves to `Authority` (14 DefinedTerm,
5 Authority, 1 ActorRole) while `member state` resolves to `ActorRole` (67 ActorRole, 3 Authority,
1 DefinedTerm). Reconciling types before case-folding would freeze one concept into two types that
can never merge. Pooling the votes after the fold gives `ActorRole` for both.

### 2. No cosine threshold separates "same entity" from "legally distinct entity"

25 pairs were hand-labelled from the regulations themselves — not from intuition — and embedded with
`embed-v4.0` (`input_type="clustering"`):

| Should merge | cos | | Must NOT merge | cos |
|---|---|---|---|---|
| `law enforcement authority` / `law-enforcement authority` | 0.966 | | `supervisory authority` / `lead supervisory authority` | **0.753** |
| `notified body` / `notified bodies` | 0.914 | | `provider` / `prospective provider` | 0.684 |
| `supervisory authority` / `supervisory authorities` | 0.907 | | `notified body` / `conformity assessment body` | 0.675 |
| `national public authority` / `public authority` | 0.794 | | `union body` / `union agency` | 0.652 |
| `commission` / `european commission` | 0.614 | | `importer` / `distributor` | 0.637 |
| `european data protection board` / `edpb` | 0.543 | | `controller` / `joint controller` | 0.569 |
| `data protection officer` / `dpo` | **0.423** | | `board` / `european data protection board` | 0.431 |

**The classes overlap by 0.33 and are not linearly separable.** The cause is structural, not a
tuning problem:

- **Legal modifiers create new entities, and embeddings read them as near-synonyms.** "lead",
  "prospective", "downstream", "joint", "notifying" each define a *separate* role in the AI Act or
  GDPR. `supervisory authority` vs `lead supervisory authority` sits at 0.753 — above four pairs that
  genuinely should merge.
- **Embeddings are hopeless at abbreviations.** `dpo` and `data protection officer` score 0.423,
  lower than any must-not-merge pair. An acronym is not semantically near its expansion.

At the roadmap's 0.90 threshold: **0 false merges out of 15, but 7 of 10 true merges missed.** High
precision, poor recall — and every missed merge falls into a class a deterministic rule handles
better (hyphens, plurals, abbreviations).

## Decision

**Four deterministic stages, applied automatically, in this order:**

1. **`normalize()`** — case-fold, strip accents/quotes, collapse hyphens to spaces, expand
   abbreviations. Seeded from `FOREIGN_INSTRUMENTS` in `extract.py` so there is one mapping, not two
   that drift.
2. **Plural folding, attested-only** — merge a plural into its singular *only where both forms occur
   in the corpus*. Blind `s`-stripping breaks `premises`, `analysis`, `business`; requiring the
   singular to be attested makes the rule data-driven rather than grammatical, so it cannot invent a
   merge the corpus does not support. Merged 18 pairs.
3. **Type reconciliation** — pooled majority vote per normalized name, with:
   - `DefinedTerm` is the ontology's catch-all, so a specific type beats it — **unless the catch-all
     holds an outright majority.** Without that guard, `international organisation` (18 DefinedTerm,
     1 ActorRole, 1 Authority) elects `Authority` on a 1-1 tiebreak, overriding 18 votes.
   - Ties broken by a lexical authority hint (`authority|body|board|office|agency|panel|…`), then by
     a fixed type priority so the result is reproducible.
   Every reconciled name is logged with its vote and the rule that settled it.
4. **Exact match** on the resulting (type, normalized name) key.

**Embedding similarity is generated as candidates for review, never auto-applied**
(`--embed` flag). `SIMILARITY_THRESHOLD` stays at 0.90, the measured zero-false-merge point.

The embedding pass is restricted to `ActorRole`, `Authority`, `SystemType`, `RiskCategory`.
`Obligation` is excluded deliberately: it is **94% unique** (1,141 distinct names for 1,215
mentions), so there is nearly nothing to merge, and obligations differ by qualifiers that change
what the law requires — a false merge there silently rewrites an obligation.

## Consequences

**3,485 → 3,366 nodes (−119, 3.4%)**, concentrated exactly where it should be:

| Type | Before | After | Merged |
|---|---|---|---|
| DefinedTerm | 549 | 484 | 65 |
| ActorRole | 105 | 78 | 27 |
| Authority | 101 | 86 | 15 |
| LawfulBasis | 136 | 132 | 4 |
| SystemType | 73 | 69 | 4 |
| Obligation | 1182 | 1179 | 3 |
| Article | 1169 | 1169 | 0 |

The overall 3.4% is small because `Article` (1,169) and `Obligation` (1,179) are 70% of all nodes and
neither is compressible — Articles are already deterministic and namespaced, Obligations are unique
by nature. Within the role vocabulary the compression is real: **ActorRole −26%, Authority −15%**.

**The embedding pass, run at 0.90 over all 236 role-like nodes, returned exactly one candidate:**

```
0.9137  [SystemType] real time remote biometric identification system
                   ~ remote biometric identification system
```

**That pair must not merge.** AIA Art. 5(1)(h) *prohibits* real-time remote biometric identification
in publicly accessible spaces for law enforcement; post-remote RBI is high-risk under Annex III, not
prohibited, and Art. 3 defines the two separately. Merging them would collapse a prohibition into a
permission — the same class of error as the ADR-0007 `LawfulBasis` bug.

So the measured outcome is: **the embedding stage produced one suggestion on this corpus, and acting
on it would have introduced a legally false merge.** The roadmap calls entity resolution "the hard
part"; on a corpus of defined legal terms the hard part turns out to be knowing when *not* to merge,
and the deterministic rules carry it.

**Accepted limitations:**

- **Alias-based merging is not applied.** 53% of mentions carry extractor aliases, and 88 alias→
  canonical pairs are real merge candidates including a valuable one (`the board` →
  `european artificial intelligence board`, correct per AIA Art. 65). But the same list contains
  `law enforcement agency` ← `law enforcement purposes`, which is plainly wrong. Candidate-quality,
  not merge-quality. Left for review.
- **Qualifier variants stay separate** (`national public authority` vs `public authority`, 0.794).
  Genuinely ambiguous, and they sit *below* the must-not-merge maximum, so no threshold reaches them
  safely.
- **False-merge/miss rates are measured on 25 adversarial hand-labelled pairs, not a random sample.**
  The pairs were chosen to be hard, so 0 false merges at 0.90 is a strong signal but not a rate.

---

## Correction (2026-07-30, during Step 4) — `normalize()` mangled sub-numbered article keys

`normalize()` ended with `.strip(" .,;:()[]")`, which stripped a closing paren regardless of whether
it had a partner. Every sub-numbered article key lost it: `AIA Art. 1(1)` resolved to
**`aia art. 1(1`**, and **1,026 of the 3,366 nodes carried an unbalanced paren.**

This was invisible to every check in this ADR because it is not a *merge* error. Resolution grouped
exactly the right names; it just gave the group a damaged key. It surfaced only when the Cypher
templates were read against the resolved data during Step 4 — every template matches
`{canonical_name: $param}`, so the query side and the Phase-3 entity linker would have had to
reproduce the mangled form forever.

**Fixed** by peeling a bracket only while it is unmatched (`_trim()`), which keeps `aia art. 1(1)`
and still strips a stray trailing `)`.

**Proved merge-neutral before re-applying**, because a change to node keys is a resolution change
unless demonstrated otherwise. Running both versions end-to-end over all 3,411 raw names:

| | before | after |
|---|---|---|
| distinct keys after normalize + plural folding | 3,366 | **3,366** |
| merge groups (raw names grouped per key) | — | **byte-for-byte identical** |
| type assignments | — | **identical** |
| cross-type collision decisions | 64 | **64** |
| keys with unbalanced parens | 1,026 | **0** |

So every number in this ADR stands; only the key spelling changed. The equivalence is now a test
(`tests/test_graph_writer.py::test_the_paren_fix_did_not_change_which_names_merge`) rather than a
one-off measurement, and a balanced-bracket assertion guards against regression.

**Also added: `display_name`.** `canonical_name` is lowercased and de-hyphenated, which is right for
a key and wrong for prose — the graph would have cited `high risk` and `aia art. 1(1)`. Each node now
carries the most frequent raw surface form (`high-risk`, `AIA Art. 1(1)`), ties broken by length then
lexically so it is reproducible. `resolved-entities.json` goes from 5 keys to 6.

**What I learned.** The four stages were each measured for what they *merged*, and the output was
correct on that axis. Nobody checked whether the resulting identifier was well-formed, because the
identifier was not what the stage was for. The bug lived for a whole stage in the one field every
downstream consumer keys on.
