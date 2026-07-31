"""Ontology-constrained entity/relationship extraction with Cohere Command A.

Reads chunks from the two JSONL files, calls Command A once per chunk with the
fixed ontology in the system prompt, validates the JSON response with Pydantic,
retries once on ValidationError, and writes one Extraction per line to
extractions.jsonl. Unparseable chunks land in failures.jsonl instead of
crashing the run.

Responses are cached on disk by content hash, so reruns after a bug fix cost
nothing for chunks whose text and prompt are unchanged.

Usage:
    python -m src.ingest.extract              # the pilot chunks + cost report
    python -m src.ingest.extract --chunk-id X # targeted re-run, repeatable
    python -m src.ingest.extract --all        # full corpus (~$15, see docs/metrics)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Literal

import cohere
import cohere.errors
import httpx
from cohere.core import ApiError
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

load_dotenv()

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
CHUNK_FILES = [
    ROOT / "data" / "processed" / "chunks-ai-act.jsonl",
    ROOT / "data" / "processed" / "chunks-gdpr.jsonl",
]
EXTRACTIONS_PATH = ROOT / "data" / "processed" / "extractions.jsonl"
FAILURES_PATH = ROOT / "data" / "processed" / "failures.jsonl"
CACHE_DIR = ROOT / "data" / "cache" / "extraction"

MODEL = os.getenv("MODEL_EXTRACT", "command-a-03-2025")

# 4096 truncated exactly 3 of 1108 chunks -- long enumerated task lists (AIA
# Art. 66, GDPR Art. 57 and 70) whose JSON was cut mid-string and then failed to
# parse. Truncation shows up as a JSON error, not as an obvious "too long", so
# the `truncated at max_tokens` counter in the run report is what identifies it.
# Not part of cache_key, so raising this re-calls only the chunks that failed.
#
# 8192 is Command A's HARD CEILING -- 16384 and 32768 both return HTTP 400
# ("max tokens must be less than or equal to 8192, the maximum output length for
# this model"). Do not re-test. Raising it fixed 2 of the 3; gdpr-art70-para1 is
# the corpus's largest chunk (864 tokens, 33 lettered sub-points) and its JSON
# does not fit in 8192 at all. See docs/failure-notes.md -- the remedy is at the
# chunker, not here.
MAX_TOKENS = 8192

# Write results to disk every N chunks. The cache protects the API spend on a
# crash; this protects the output file, which is otherwise only written once the
# whole run finishes.
FLUSH_EVERY = 25

# Cohere list price for Command A, USD per token. Stated here so the cost
# estimate is auditable -- check these against cohere.com/pricing.
PRICE_INPUT_PER_TOKEN = 2.50 / 1_000_000
PRICE_OUTPUT_PER_TOKEN = 10.00 / 1_000_000

# The chunks used to sanity-check the ontology before spending on the corpus.
# Chosen to span legal *functions*, not sources: duties, permissions, definitions,
# derogations, penalties, rights and the cross-regulation bridge. A type with no
# chunk exercising it is an untested type, which is how the LawfulBasis and
# DefinedTerm holes both stayed hidden.
TEST_CHUNK_IDS = [
    "aia-art9-para1",      # obligation (control -- must stay Obligation/IMPOSES)
    "aia-art26-para6",
    "aia-art43-para1",
    "aia-art8-para2",
    "aia-annex3-point1",
    "aia-art3-def37",
    "aia-art3-def39",
    "gdpr-art9-para1",
    "gdpr-art9-para2",
    "gdpr-art6-para1",     # permission -- must yield LawfulBasis/PERMITS, no Obligation
    "aia-art99-para4",     # penalty tiers -- PENALIZED_UNDER + SETS_PENALTY, no false EXEMPT_FROM
    "gdpr-art83-para5",    # penalty tiers, GDPR side
    "gdpr-art21-para2",    # right -- must yield Right/GRANTS, not Obligation
    "aia-art3-def10",      # definition -- must yield DefinedTerm, not RiskCategory
]

# Foreign instruments the regulations cite by full number. Command A is asked to
# short-name these in the prompt, but the mapping is applied deterministically
# after parsing too, so the graph never gets two nodes for the same instrument.
FOREIGN_INSTRUMENTS = {
    "regulation (eu) 2016/679": "GDPR",
    "directive (eu) 2016/680": "LED",
    "regulation (eu) 2018/1725": "EUDPR",
}

# --------------------------------------------------------------------------
# Schema -- the ontology is locked by Literal, so anything the model invents
# fails validation rather than silently entering the graph.
# --------------------------------------------------------------------------

EntityType = Literal[
    "Regulation",
    "Article",
    "Annex",
    "ActorRole",
    "Obligation",
    "RiskCategory",
    "SystemType",
    "Authority",
    "LawfulBasis",
    "DefinedTerm",
    "Right",
    "Penalty",
]

RelationType = Literal[
    "DEFINED_IN",
    "IMPOSES",
    "APPLIES_TO",
    "CLASSIFIED_AS",
    "LISTED_IN",
    "REFERENCES",
    "ENFORCED_BY",
    "PENALIZED_UNDER",
    "EXEMPT_FROM",
    "INTERACTS_WITH",
    "PERMITS",
    "GRANTS",
    "SETS_PENALTY",
]

# Which entity types may sit at each end of each relationship. The prompt states
# these too, but Literal only validates the type *string* -- it cannot see that
# ENFORCED_BY was pointed at a Regulation instead of an Authority. Checked after
# parsing so the violations are counted rather than silently entering the graph.
_PROVISION = {"Article", "Annex"}
_DEFINABLE = {
    "DefinedTerm", "ActorRole", "SystemType", "RiskCategory",
    "Obligation", "Right", "LawfulBasis", "Authority", "Penalty",
}
_PARTY = {"ActorRole", "SystemType", "Authority"}

ALLOWED_ENDPOINTS: dict[str, tuple[set[str], set[str]]] = {
    "DEFINED_IN": (_DEFINABLE, _PROVISION),
    "IMPOSES": (_PROVISION | {"Regulation"}, {"Obligation"}),
    # Deliberately wide on both ends: a duty, a provision, a classification, a
    # basis, a right or a system type can all "govern" a party -- and a duty can
    # equally concern a kind of data ("...applies to personal data"). The
    # obligations_for_role template filters on the :ActorRole label anyway, so a
    # wider tail adds no noise to the query it exists to serve.
    "APPLIES_TO": ({"Obligation", "Article", "Annex", "RiskCategory", "LawfulBasis",
                    "Right", "SystemType", "DefinedTerm"}, _PARTY | {"DefinedTerm"}),
    "CLASSIFIED_AS": ({"SystemType", "DefinedTerm", "ActorRole"}, {"RiskCategory"}),
    "LISTED_IN": ({"SystemType", "DefinedTerm", "Regulation", "Obligation", "Authority"},
                  _PROVISION),
    "REFERENCES": (_PROVISION, _PROVISION),
    # The tight ones -- these are where the probe found real errors.
    "ENFORCED_BY": ({"Obligation", "Regulation", "Article", "Right"}, {"Authority"}),
    "PENALIZED_UNDER": ({"Obligation"}, _PROVISION),
    "EXEMPT_FROM": ({"ActorRole", "SystemType", "Obligation"}, {"Obligation"} | _PROVISION),
    # Annex is a head here because an annex genuinely does interact with a foreign
    # instrument -- AIA Annex VIII points at GDPR Art. 35. Widened 2026-07-31: the
    # old {Regulation, Article} head flagged 11 real Annex->Regulation edges as
    # violations, and would have flagged the derived Annex->Article bridges too.
    # Validation-only, so this costs no re-extraction.
    "INTERACTS_WITH": (_PROVISION | {"Regulation"}, {"Regulation", "Article"}),
    "PERMITS": ({"Article", "Regulation", "Annex"}, {"LawfulBasis"}),
    "GRANTS": (_PROVISION | {"Regulation"}, {"Right"}),
    "SETS_PENALTY": (_PROVISION, {"Penalty"}),
}


class Entity(BaseModel):
    type: EntityType
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)


class Relationship(BaseModel):
    type: RelationType
    head: str
    tail: str
    source_chunk_id: str
    confidence: float = Field(ge=0.0, le=1.0)


class Extraction(BaseModel):
    chunk_id: str
    entities: list[Entity]
    relationships: list[Relationship]


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract a knowledge graph from single paragraphs of EU legislation (the EU \
AI Act and the GDPR). You work against a FIXED ontology. You may not invent \
types.

ENTITY TYPES (use exactly these 12 strings):
- Regulation: a whole legal instrument, e.g. "AIA", "GDPR", "Directive (EU) 2016/680".
- Article: one numbered article, namespaced by regulation, e.g. "AIA Art. 9", "GDPR Art. 9(1)".
- Annex: one annex of a regulation, e.g. "AIA Annex III", "AIA Annex VII".
- ActorRole: a role a person or organisation plays, e.g. "provider", "deployer", "controller".
- Obligation: a duty or prohibition, written as a short verb phrase.
- RiskCategory: a RISK classification and nothing else. Only these four and their close variants: "high-risk", "prohibited", "limited risk", "minimal risk". If the phrase does not grade how dangerous something is, it is NOT a RiskCategory.
- SystemType: a kind of AI or technical system, e.g. "emotion recognition system", "risk management system".
- Authority: a body with oversight, certification or enforcement power, e.g. "notified body", "supervisory authority".
- LawfulBasis: a ground that makes an activity lawful or lifts a prohibition (e.g. consent, contract, legitimate interests, a derogation). NOT an obligation -- it permits, it does not require.
- DefinedTerm: a term the text defines or uses as a term of art, when it is not one of the more specific types above. e.g. "biometric data", "personal data", "making available on the market", "substantial modification", "serious incident". This is the default home for a definiendum.
- Right: an entitlement held by a person or body, e.g. "right to object", "right of access", "right to erasure". A right is held BY someone; an obligation is owed BY someone.
- Penalty: a sanction with its magnitude, written so the amount survives, e.g. "administrative fine up to EUR 15 000 000 or 3 % of total worldwide annual turnover".

RELATIONSHIP TYPES (use exactly these 13 strings):
- DEFINED_IN: a term or entity is defined by the provision. head=DefinedTerm/ActorRole/SystemType/RiskCategory/Obligation/Right, tail=Article/Annex.
- IMPOSES: a provision or regulation creates a duty. head=Article/Annex/Regulation, tail=Obligation.
- APPLIES_TO: a duty, provision, classification, basis or right governs or is held by someone. head=Obligation/Article/RiskCategory/LawfulBasis/Right, tail=ActorRole/SystemType.
- CLASSIFIED_AS: something is assigned a risk classification. head=SystemType/DefinedTerm/ActorRole, tail=RiskCategory.
- LISTED_IN: an item appears in an enumerated list. head=SystemType/DefinedTerm/Regulation/Obligation, tail=Annex/Article.
- REFERENCES: one provision cross-refers to another. head=Article/Annex, tail=Article/Annex.
- ENFORCED_BY: a duty or regulation is supervised or enforced. head=Obligation/Regulation/Article, tail=Authority. The tail MUST be a body, never a Regulation.
- PENALIZED_UNDER: breaching a duty is sanctioned by a provision. head=Obligation, tail=Article.
- EXEMPT_FROM: someone or something is carved out of a duty or prohibition. head=ActorRole/SystemType/Obligation, tail=Obligation/Article.
- INTERACTS_WITH: two legal instruments or regimes operate together across a boundary. head/tail=Regulation/Article.
- PERMITS: head makes tail lawful/justified/allowed. Use for "processing is lawful if X", "the prohibition does not apply where Y", derogations, and exceptions. This is the permissive counterpart of IMPOSES -- never use IMPOSES for a permission. head=Article/Regulation, tail=LawfulBasis.
- GRANTS: a provision confers an entitlement. head=Article/Regulation/Annex, tail=Right.
- SETS_PENALTY: a provision fixes a sanction and its amount. head=Article/Annex, tail=Penalty.

PERMISSION vs OBLIGATION -- READ THIS BEFORE EXTRACTING
If the text says an activity is LAWFUL / PERMITTED / ALLOWED when a condition \
holds, model the condition as a LawfulBasis entity and connect it with PERMITS. \
Do NOT model it as an Obligation with IMPOSES. "Processing is lawful if at least \
one of the following applies" introduces LawfulBasis + PERMITS, not obligations.

A duty says someone SHALL DO something. A lawful basis says something MAY be \
done IF a condition holds. "the data subject has given consent" is not a duty to \
obtain consent -- it is a ground that makes processing lawful. Likewise, a \
provision saying a prohibition "shall not apply if" one of several conditions is \
met introduces LawfulBasis entities, each connected with PERMITS; do not turn \
those conditions into Obligations.

RIGHT vs OBLIGATION -- READ THIS TOO
If the text says a person or body HAS THE RIGHT TO / MAY EXERCISE / IS ENTITLED \
TO something, model it as a Right connected with GRANTS from the provision, and \
APPLIES_TO the holder. Do NOT restate it as an Obligation on the other party, \
and do NOT model it as a LawfulBasis. "the data subject shall have the right to \
object" is a Right named "right to object" -- not an Obligation "allow objections" \
and not a LawfulBasis. Model the same right the same way every time it appears, \
so separate paragraphs about one right converge on one node.

DEFINEDTERM vs RISKCATEGORY -- READ THIS TOO
RiskCategory is ONLY for how dangerous something is: high-risk, prohibited, \
limited risk, minimal risk. Every other term of art the text defines or leans on \
is a DefinedTerm. "biometric data", "personal data", "special categories of \
personal data", "making available on the market", "serious incident" are \
DefinedTerms, NOT RiskCategories. When a chunk defines a term ("'X' means ..."), \
the definiendum is normally a DefinedTerm unless it is clearly a SystemType, \
ActorRole, Authority or Right.

PENALTIES -- KEEP THE AMOUNT
When a provision fixes a fine, create a Penalty entity whose canonical_name \
carries the magnitude, and attach it with SETS_PENALTY from the provision. Then \
link each penalised duty to that provision with PENALIZED_UNDER. The amount must \
survive into the graph -- "administrative fine" alone is not enough.

A carve-out that routes a breach to a DIFFERENT penalty provision is NOT an \
exemption. "other than those laid down in Article 5" means Article 5 breaches are \
fined under another paragraph, not that Article 5 is exempt from penalties. Emit \
no EXEMPT_FROM edge for that -- say nothing rather than assert a false exemption. \
Reserve EXEMPT_FROM for text that actually relieves someone of a duty, and never \
put an Article on the head of EXEMPT_FROM.

RULES
1. Extract ONLY what THIS chunk's text states. Do not add facts you know about \
the regulation from elsewhere. If the text does not mention penalties, emit no \
PENALIZED_UNDER edge.
2. Every relationship's "source_chunk_id" MUST be the chunk_id you were given.
3. Every "head" and "tail" MUST exactly match the "canonical_name" of an entity \
you listed in "entities".
4. Every entity you list MUST appear in at least one relationship. If you cannot \
connect it to anything the text states, do not list it.
5. Respect the head/tail types given for each relationship above. An edge whose \
ends are the wrong type is worse than no edge.
6. When the text cites other provisions, emit a REFERENCES edge to each one. Do \
not list a cited Article as an entity and then leave it unconnected.
7. Do not invent an entity to fit a type. If the text says "direct marketing" or \
"profiling", do not manufacture "direct marketing system" or "profiling system"; \
name it as the text does, or leave it out.
8. "confidence" is your honest 0.0-1.0 confidence that the text really asserts \
that relationship. Use the full range: an edge stated explicitly deserves ~0.95, \
one you inferred from phrasing deserves ~0.5-0.7. Do not mark everything 1.0.
9. Return ONLY a JSON object. No prose, no explanation, no markdown fences.

CANONICAL NAME NORMALISATION
- Role names lowercase and singular: "deployer", not "Deployers".
- Obligations as short verb phrases: "keep automatically generated logs", not a \
sentence copied from the text.
- Articles namespaced by regulation, because numbers collide: "AIA Art. 9", \
"GDPR Art. 9". Keep the sub-number when the text cites one: "GDPR Art. 9(1)".
- THE ARTICLE THIS CHUNK BELONGS TO must carry its own sub-number, taken from the \
header you were given: use the "paragraph" field if present, otherwise the \
"definition" field. article: 13 + paragraph: 2 is "GDPR Art. 13(2)". article: 3 + \
definition: 12 is "AIA Art. 3(12)". Never bare "GDPR Art. 13" for a chunk that is \
one paragraph of it. This is load-bearing: bare and sub-numbered names become \
separate nodes, which breaks every cross-reference into that paragraph. Articles \
the chunk merely CITES keep whatever precision the text gives them.
- Annexes namespaced with Roman numerals: "AIA Annex III".
- Name external instruments by their short name, keeping the full citation in \
"aliases": Regulation (EU) 2016/679 is "GDPR", Directive (EU) 2016/680 is "LED", \
Regulation (EU) 2018/1725 is "EUDPR". An instrument with no known short name \
keeps its full citation as canonical_name.

OUTPUT SCHEMA
{
  "chunk_id": str,
  "entities": [{"type": EntityType, "canonical_name": str, "aliases": [str]}],
  "relationships": [{"type": RelationType, "head": str, "tail": str,
                     "source_chunk_id": str, "confidence": float}]
}

EXAMPLE 1
chunk_id: aia-art17-para1
text: Providers of high-risk AI systems shall put a quality management system in \
place that ensures compliance with this Regulation.
output:
{"chunk_id":"aia-art17-para1","entities":[{"type":"Article","canonical_name":"AIA Art. 17(1)","aliases":["Article 17(1)"]},{"type":"ActorRole","canonical_name":"provider","aliases":["providers"]},{"type":"SystemType","canonical_name":"high-risk AI system","aliases":[]},{"type":"RiskCategory","canonical_name":"high-risk","aliases":[]},{"type":"Obligation","canonical_name":"put a quality management system in place","aliases":[]}],"relationships":[{"type":"IMPOSES","head":"AIA Art. 17(1)","tail":"put a quality management system in place","source_chunk_id":"aia-art17-para1","confidence":0.96},{"type":"APPLIES_TO","head":"put a quality management system in place","tail":"provider","source_chunk_id":"aia-art17-para1","confidence":0.95},{"type":"APPLIES_TO","head":"put a quality management system in place","tail":"high-risk AI system","source_chunk_id":"aia-art17-para1","confidence":0.88},{"type":"CLASSIFIED_AS","head":"high-risk AI system","tail":"high-risk","source_chunk_id":"aia-art17-para1","confidence":0.9}]}

EXAMPLE 2 (a definition -- the definiendum is a DefinedTerm, NOT a RiskCategory)
chunk_id: aia-art3-def12
text: 'biometric data' means personal data resulting from specific technical \
processing relating to the physical characteristics of a natural person, as \
defined in Article 4(14) of Regulation (EU) 2016/679;
output:
{"chunk_id":"aia-art3-def12","entities":[{"type":"Article","canonical_name":"AIA Art. 3(12)","aliases":["Article 3(12)"]},{"type":"Article","canonical_name":"GDPR Art. 4(14)","aliases":["Article 4(14) of Regulation (EU) 2016/679"]},{"type":"Regulation","canonical_name":"GDPR","aliases":["Regulation (EU) 2016/679"]},{"type":"DefinedTerm","canonical_name":"biometric data","aliases":[]},{"type":"DefinedTerm","canonical_name":"personal data","aliases":[]}],"relationships":[{"type":"DEFINED_IN","head":"biometric data","tail":"AIA Art. 3(12)","source_chunk_id":"aia-art3-def12","confidence":0.97},{"type":"DEFINED_IN","head":"personal data","tail":"GDPR Art. 4(14)","source_chunk_id":"aia-art3-def12","confidence":0.85},{"type":"REFERENCES","head":"AIA Art. 3(12)","tail":"GDPR Art. 4(14)","source_chunk_id":"aia-art3-def12","confidence":0.95},{"type":"INTERACTS_WITH","head":"AIA Art. 3(12)","tail":"GDPR","source_chunk_id":"aia-art3-def12","confidence":0.8}]}

EXAMPLE 3
chunk_id: aia-annex3-point5
text: Access to and enjoyment of essential private services and essential public \
services and benefits: (a) AI systems intended to be used by public authorities \
to evaluate the eligibility of natural persons for essential public assistance \
benefits.
output:
{"chunk_id":"aia-annex3-point5","entities":[{"type":"Annex","canonical_name":"AIA Annex III","aliases":["Annex III"]},{"type":"SystemType","canonical_name":"benefit eligibility evaluation system","aliases":["AI systems intended to be used to evaluate the eligibility of natural persons for essential public assistance benefits"]},{"type":"RiskCategory","canonical_name":"high-risk","aliases":[]},{"type":"Authority","canonical_name":"public authority","aliases":["public authorities"]}],"relationships":[{"type":"LISTED_IN","head":"benefit eligibility evaluation system","tail":"AIA Annex III","source_chunk_id":"aia-annex3-point5","confidence":0.95},{"type":"CLASSIFIED_AS","head":"benefit eligibility evaluation system","tail":"high-risk","source_chunk_id":"aia-annex3-point5","confidence":0.85},{"type":"APPLIES_TO","head":"benefit eligibility evaluation system","tail":"public authority","source_chunk_id":"aia-annex3-point5","confidence":0.7}]}

EXAMPLE 4 (permission, NOT obligation -- note there is no IMPOSES edge here)
chunk_id: gdpr-art6-para1-example
text: Processing shall be lawful only if and to the extent that at least one of \
the following applies: (a) the data subject has given consent to the processing \
of his or her personal data for one or more specific purposes; (c) processing is \
necessary for compliance with a legal obligation to which the controller is subject;
output:
{"chunk_id":"gdpr-art6-para1-example","entities":[{"type":"Article","canonical_name":"GDPR Art. 6(1)","aliases":["Article 6(1)"]},{"type":"ActorRole","canonical_name":"data subject","aliases":[]},{"type":"ActorRole","canonical_name":"controller","aliases":[]},{"type":"LawfulBasis","canonical_name":"consent of the data subject","aliases":["consent"]},{"type":"LawfulBasis","canonical_name":"compliance with a legal obligation","aliases":[]}],"relationships":[{"type":"PERMITS","head":"GDPR Art. 6(1)","tail":"consent of the data subject","source_chunk_id":"gdpr-art6-para1-example","confidence":0.95},{"type":"PERMITS","head":"GDPR Art. 6(1)","tail":"compliance with a legal obligation","source_chunk_id":"gdpr-art6-para1-example","confidence":0.95},{"type":"APPLIES_TO","head":"consent of the data subject","tail":"data subject","source_chunk_id":"gdpr-art6-para1-example","confidence":0.9},{"type":"APPLIES_TO","head":"compliance with a legal obligation","tail":"controller","source_chunk_id":"gdpr-art6-para1-example","confidence":0.9}]}

EXAMPLE 5 (a right, and a penalty that keeps its amount)
chunk_id: gdpr-art77-para1-example
text: Without prejudice to any other remedy, every data subject shall have the \
right to lodge a complaint with a supervisory authority. Infringements of the \
obligations of the controller pursuant to Articles 25 and 32 shall be subject to \
administrative fines up to 10 000 000 EUR, or in the case of an undertaking, up \
to 2 % of its total worldwide annual turnover.
output:
{"chunk_id":"gdpr-art77-para1-example","entities":[{"type":"Article","canonical_name":"GDPR Art. 77(1)","aliases":["Article 77(1)"]},{"type":"Article","canonical_name":"GDPR Art. 25","aliases":[]},{"type":"Article","canonical_name":"GDPR Art. 32","aliases":[]},{"type":"ActorRole","canonical_name":"data subject","aliases":[]},{"type":"ActorRole","canonical_name":"controller","aliases":[]},{"type":"Authority","canonical_name":"supervisory authority","aliases":[]},{"type":"Right","canonical_name":"right to lodge a complaint with a supervisory authority","aliases":["right to lodge a complaint"]},{"type":"Obligation","canonical_name":"comply with data protection by design obligations","aliases":[]},{"type":"Obligation","canonical_name":"comply with security of processing obligations","aliases":[]},{"type":"Penalty","canonical_name":"administrative fine up to EUR 10 000 000 or 2 % of total worldwide annual turnover","aliases":[]}],"relationships":[{"type":"GRANTS","head":"GDPR Art. 77(1)","tail":"right to lodge a complaint with a supervisory authority","source_chunk_id":"gdpr-art77-para1-example","confidence":0.96},{"type":"APPLIES_TO","head":"right to lodge a complaint with a supervisory authority","tail":"data subject","source_chunk_id":"gdpr-art77-para1-example","confidence":0.95},{"type":"ENFORCED_BY","head":"right to lodge a complaint with a supervisory authority","tail":"supervisory authority","source_chunk_id":"gdpr-art77-para1-example","confidence":0.85},{"type":"SETS_PENALTY","head":"GDPR Art. 77(1)","tail":"administrative fine up to EUR 10 000 000 or 2 % of total worldwide annual turnover","source_chunk_id":"gdpr-art77-para1-example","confidence":0.95},{"type":"PENALIZED_UNDER","head":"comply with data protection by design obligations","tail":"GDPR Art. 77(1)","source_chunk_id":"gdpr-art77-para1-example","confidence":0.9},{"type":"PENALIZED_UNDER","head":"comply with security of processing obligations","tail":"GDPR Art. 77(1)","source_chunk_id":"gdpr-art77-para1-example","confidence":0.9},{"type":"APPLIES_TO","head":"comply with data protection by design obligations","tail":"controller","source_chunk_id":"gdpr-art77-para1-example","confidence":0.9},{"type":"APPLIES_TO","head":"comply with security of processing obligations","tail":"controller","source_chunk_id":"gdpr-art77-para1-example","confidence":0.9},{"type":"REFERENCES","head":"GDPR Art. 77(1)","tail":"GDPR Art. 25","source_chunk_id":"gdpr-art77-para1-example","confidence":0.95},{"type":"REFERENCES","head":"GDPR Art. 77(1)","tail":"GDPR Art. 32","source_chunk_id":"gdpr-art77-para1-example","confidence":0.95}]}
"""


def user_prompt(chunk: dict) -> str:
    """The per-chunk message. Article/annex metadata is included because the
    text alone often does not say which article it belongs to."""
    header = [f"chunk_id: {chunk['chunk_id']}", f"regulation: {chunk['regulation']}"]
    for key in ("article", "article_title", "paragraph", "annex", "annex_title", "point", "definition"):
        if chunk.get(key) is not None:
            header.append(f"{key}: {chunk[key]}")
    return "\n".join(header) + f"\ntext: {chunk['text']}\n\nReturn the JSON object now."


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def cache_key(chunk_text: str) -> str:
    """Hash the chunk text together with the model and prompt, so that editing
    the prompt invalidates the cache instead of returning stale extractions."""
    material = "\n".join([MODEL, SYSTEM_PROMPT, chunk_text])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cache_load(key: str) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cache_store(key: str, record: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Cohere call
# --------------------------------------------------------------------------


def get_client() -> cohere.ClientV2:
    api_key = os.getenv("CO_API_KEY") or os.getenv("COHERE_API_KEY")
    if not api_key:
        sys.exit("CO_API_KEY (or COHERE_API_KEY) is not set. Put it in .env or the environment.")
    return cohere.ClientV2(api_key=api_key)


# Transient failures worth waiting out. Deliberately does NOT include
# BadRequest/Unauthorized/Forbidden/NotFound/UnprocessableEntity -- those are
# permanent, and retrying them six times just delays a run that cannot succeed.
RETRYABLE_ERRORS = (
    cohere.errors.TooManyRequestsError,   # 429 -- the expected one across 1108 calls
    cohere.errors.ServiceUnavailableError,  # 503
    cohere.errors.InternalServerError,    # 500
    cohere.errors.GatewayTimeoutError,    # 504
    httpx.TransportError,                 # connect/read timeouts, protocol errors
)

# Transport-level retries, counted separately from the validation retry in
# extract_chunk. They mean different things: this one is "the API was busy", the
# other is "the model returned something that failed the schema".
_transport_retries = 0


def _note_retry(retry_state) -> None:
    global _transport_retries
    _transport_retries += 1
    exc = retry_state.outcome.exception()
    sleep = getattr(retry_state.next_action, "sleep", 0.0)
    print(
        f"\n    [transport retry {retry_state.attempt_number}] "
        f"{type(exc).__name__} -- waiting {sleep:.1f}s",
        end="",
        flush=True,
    )


@retry(
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    wait=wait_exponential_jitter(initial=2, max=90),
    stop=stop_after_attempt(6),
    before_sleep=_note_retry,
    reraise=True,
)
def call_model(client: cohere.ClientV2, messages: list[dict]) -> tuple[str, int, int, str]:
    """One Command A call. Returns (text, input_tokens, output_tokens, finish_reason).

    Retries transient API failures with exponential backoff + jitter. A 1108-call
    sequential run will hit rate limits; without this the run dies and has to be
    restarted by hand. The disk cache makes a restart free in API terms, but not
    in wall-clock terms.
    """
    res = client.chat(
        model=MODEL,
        messages=messages,
        temperature=0,
        seed=42,
        max_tokens=MAX_TOKENS,
        response_format={"type": "json_object"},
    )
    text = "".join(block.text for block in (res.message.content or []))
    tokens = res.usage.tokens if res.usage else None
    return (
        text,
        int(tokens.input_tokens or 0) if tokens else 0,
        int(tokens.output_tokens or 0) if tokens else 0,
        str(res.finish_reason),
    )


def strip_fences(text: str) -> str:
    """The prompt forbids markdown fences, but strip them defensively."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    return text.strip()


def normalize_instruments(data: dict) -> int:
    """Rewrite full EU citations to their short names on Regulation entities.

    The prompt asks for this, but doing it deterministically as well stops
    "Regulation (EU) 2016/679" and "GDPR" becoming two nodes for one instrument.
    head/tail are remapped too, or the rename would orphan the edges.
    """
    renames: dict[str, str] = {}
    for entity in data.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("type") != "Regulation":
            continue
        name = str(entity.get("canonical_name", ""))
        short = FOREIGN_INSTRUMENTS.get(name.strip().lower())
        if not short or short == name:
            continue  # unknown instrument keeps its full citation
        aliases = entity.get("aliases") or []
        if name not in aliases:
            aliases.append(name)
        entity["canonical_name"], entity["aliases"] = short, aliases
        renames[name] = short

    for rel in data.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        for end in ("head", "tail"):
            if rel.get(end) in renames:
                rel[end] = renames[rel[end]]
    return len(renames)


def parse(raw: str, chunk_id: str) -> tuple[Extraction, int]:
    """Validate a raw response into an Extraction.

    Rewrites any source_chunk_id the model got wrong, since that field is a
    provenance fact we already know and must not let the model corrupt.
    Returns the extraction and the number of ids repaired.
    """
    data = json.loads(strip_fences(raw))
    data["chunk_id"] = chunk_id  # never trust the echo
    normalize_instruments(data)
    repaired = 0
    for rel in data.get("relationships") or []:
        if isinstance(rel, dict) and rel.get("source_chunk_id") != chunk_id:
            rel["source_chunk_id"] = chunk_id
            repaired += 1
    return Extraction.model_validate(data), repaired


def extract_chunk(client: cohere.ClientV2, chunk: dict, stats: dict) -> Extraction | None:
    """Extract one chunk: cache lookup, call, validate, retry once, else fail.

    Returns None if both attempts failed; the failure is recorded in stats.
    """
    chunk_id = chunk["chunk_id"]
    key = cache_key(chunk["text"])
    cached = cache_load(key)

    if cached is not None:
        stats["cache_hits"] += 1
        raw, in_tok, out_tok = cached["raw"], 0, 0
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt(chunk)},
        ]
        raw, in_tok, out_tok, finish = call_model(client, messages)
        stats["input_tokens"] += in_tok
        stats["output_tokens"] += out_tok
        stats["api_calls"] += 1
        if finish.upper().endswith("MAX_TOKENS"):
            stats["truncated"] += 1

    # First validation attempt.
    try:
        extraction, repaired = parse(raw, chunk_id)
        stats["source_id_repairs"] += repaired
        if cached is None:
            cache_store(key, {"chunk_id": chunk_id, "raw": raw})
        return extraction
    except (ValidationError, json.JSONDecodeError, TypeError) as first_error:
        if cached is not None:
            # A cached response that no longer validates means the code changed,
            # not the model. Re-call rather than retrying against stale text.
            stats["cache_hits"] -= 1
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(chunk)},
            ]
            raw, in_tok, out_tok, _ = call_model(client, messages)
            stats["input_tokens"] += in_tok
            stats["output_tokens"] += out_tok
            stats["api_calls"] += 1
        error_text = str(first_error)

    # Retry once, showing the model its own output and the validation error.
    stats["retries"] += 1
    repair_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(chunk)},
        {"role": "assistant", "content": raw},
        {
            "role": "user",
            "content": (
                "Your previous response failed schema validation with this error:\n\n"
                f"{error_text}\n\n"
                "Fix it and return ONLY the corrected JSON object. Use only the 9 "
                "entity types and 11 relationship types from the ontology, and set "
                f'every "source_chunk_id" to "{chunk_id}".'
            ),
        },
    ]
    retry_raw, in_tok, out_tok, _ = call_model(client, repair_messages)
    stats["input_tokens"] += in_tok
    stats["output_tokens"] += out_tok
    stats["api_calls"] += 1

    try:
        extraction, repaired = parse(retry_raw, chunk_id)
        stats["source_id_repairs"] += repaired
        cache_store(key, {"chunk_id": chunk_id, "raw": retry_raw})
        return extraction
    except (ValidationError, json.JSONDecodeError, TypeError) as second_error:
        stats["failures"].append(
            {
                "chunk_id": chunk_id,
                "raw_response": retry_raw,
                "first_error": error_text,
                "error": str(second_error),
            }
        )
        return None


# --------------------------------------------------------------------------
# Corpus IO and reporting
# --------------------------------------------------------------------------


def load_chunks() -> list[dict]:
    chunks = []
    for path in CHUNK_FILES:
        with path.open(encoding="utf-8") as fh:
            chunks.extend(json.loads(line) for line in fh if line.strip())
    return chunks


def write_jsonl(path: Path, rows: list[dict], touched: set[str]) -> None:
    """Upsert `rows` into a JSONL file keyed by chunk_id.

    Rows for chunk_ids in `touched` are replaced by this run's results (and
    dropped if this run produced none, so a fixed chunk clears its old failure);
    rows for every other chunk_id are preserved in their original order.
    """
    kept = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            kept = [
                row
                for line in fh
                if line.strip()
                for row in [json.loads(line)]
                if row.get("chunk_id") not in touched
            ]
    with path.open("w", encoding="utf-8") as fh:
        for row in kept + rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def dangling_refs(extraction: Extraction) -> list[str]:
    """head/tail values that don't match any declared entity. Not fatal, but a
    quality signal -- these become orphan nodes at graph-load time."""
    names = {e.canonical_name for e in extraction.entities}
    return sorted(
        {n for rel in extraction.relationships for n in (rel.head, rel.tail) if n not in names}
    )


def orphan_entities(extraction: Extraction) -> list[str]:
    """Declared entities that appear in no relationship.

    The mirror image of dangling_refs, which only looks for edges with no entity.
    An entity with no edge loads into Neo4j as a disconnected node, and when it is
    a cited Article it means the REFERENCES cross-reference backbone lost an edge.
    Invisible to every check we had before.
    """
    used = {n for rel in extraction.relationships for n in (rel.head, rel.tail)}
    return sorted({e.canonical_name for e in extraction.entities if e.canonical_name not in used})


def endpoint_violations(extraction: Extraction) -> list[str]:
    """Relationships whose head/tail entity types are not allowed for that type.

    Pydantic's Literal validates the type *string* only, so an ENFORCED_BY edge
    pointing at a Regulation instead of an Authority is schema-valid and silently
    wrong -- the same failure shape as the LawfulBasis bug. Reported, never
    silently dropped: a dropped edge looks identical to one that was never
    extracted, and that is how the ontology hole hid last time.
    """
    types = {e.canonical_name: e.type for e in extraction.entities}
    out = []
    for rel in extraction.relationships:
        allowed = ALLOWED_ENDPOINTS.get(rel.type)
        if allowed is None:
            continue
        head_ok = types.get(rel.head) in allowed[0]
        tail_ok = types.get(rel.tail) in allowed[1]
        if head_ok and tail_ok:
            continue
        bad = []
        if not head_ok:
            bad.append(f"head={types.get(rel.head, '?')}")
        if not tail_ok:
            bad.append(f"tail={types.get(rel.tail, '?')}")
        out.append(f"{rel.type}({', '.join(bad)}): {rel.head} -> {rel.tail}")
    return out


def self_article_name(chunk: dict) -> str | None:
    """The canonical_name this chunk's own Article entity should carry.

    Returns None for annex chunks, which have no article. Used to measure how
    often the model drops the paragraph number -- bare "GDPR Art. 13" and
    "GDPR Art. 13(2)" are two nodes, which severs cross-references into the
    paragraph.
    """
    article = chunk.get("article")
    if article is None:
        return None
    sub = chunk.get("paragraph") if chunk.get("paragraph") is not None else chunk.get("definition")
    prefix = "AIA" if chunk["regulation"] == "AIA" else chunk["regulation"]
    return f"{prefix} Art. {article}({sub})" if sub is not None else f"{prefix} Art. {article}"


def granularity_miss(extraction: Extraction, chunk: dict) -> str | None:
    """True when the chunk's own article was named without its paragraph number."""
    expected = self_article_name(chunk)
    if expected is None or "(" not in expected:
        return None
    bare = expected.split("(")[0]
    names = {e.canonical_name for e in extraction.entities if e.type == "Article"}
    return bare if bare in names and expected not in names else None


def report(stats: dict, extractions: list[Extraction], corpus_size: int) -> None:
    n = len(extractions) + len(stats["failures"])
    in_tok, out_tok = stats["input_tokens"], stats["output_tokens"]
    billed = max(stats["api_calls"], 1)

    print("\n" + "=" * 68)
    print("COST REPORT")
    print("=" * 68)
    print(f"chunks processed        : {n}")
    print(f"  succeeded             : {len(extractions)}")
    print(f"  failed                : {len(stats['failures'])}  "
          f"({len(stats['failures']) / max(n, 1):.1%} failure rate)")
    print(f"  retried               : {stats['retries']}")
    print(f"  served from cache     : {stats['cache_hits']}")
    print(f"api calls made          : {stats['api_calls']}")
    print(f"source_chunk_id repairs : {stats['source_id_repairs']}")
    print(f"truncated at max_tokens : {stats['truncated']}")
    print(f"transport retries       : {_transport_retries}  (rate limits / 5xx)")
    print(f"api errors (skipped)    : {stats['api_errors']}")
    print()
    print("INTEGRITY")
    print(f"dangling head/tail refs : {stats['dangling']}")
    print(f"orphan entities (no edge): {stats['orphans']}")
    print(f"bad edge endpoints      : {len(stats['endpoint_violations'])}")
    print(f"bare-article granularity: {len(stats['granularity_misses'])} of {n}")
    for chunk_id, violation in stats["endpoint_violations"][:12]:
        print(f"    {chunk_id}: {violation}")
    if len(stats["endpoint_violations"]) > 12:
        print(f"    ... and {len(stats['endpoint_violations']) - 12} more")
    for chunk_id, bare in stats["granularity_misses"][:12]:
        print(f"    {chunk_id}: emitted bare '{bare}'")
    if len(stats["granularity_misses"]) > 12:
        print(f"    ... and {len(stats['granularity_misses']) - 12} more")
    print()
    print(f"total input tokens      : {in_tok:,}")
    print(f"total output tokens     : {out_tok:,}")
    print(f"avg input tokens/chunk  : {in_tok / billed:,.0f}")
    print(f"avg output tokens/chunk : {out_tok / billed:,.0f}")
    print(f"avg total tokens/chunk  : {(in_tok + out_tok) / billed:,.0f}")

    if stats["api_calls"] == 0:
        print("\n(all chunks came from cache -- no token data this run)")
        return

    sample_cost = in_tok * PRICE_INPUT_PER_TOKEN + out_tok * PRICE_OUTPUT_PER_TOKEN
    per_chunk = sample_cost / billed
    print()
    print(f"price used              : ${PRICE_INPUT_PER_TOKEN * 1e6:.2f}/1M input, "
          f"${PRICE_OUTPUT_PER_TOKEN * 1e6:.2f}/1M output (Command A list price)")
    print(f"cost of this run        : ${sample_cost:.4f}")
    print(f"cost per chunk          : ${per_chunk:.4f}")
    print(f"corpus size             : {corpus_size} chunks")
    print(f"ESTIMATED FULL CORPUS   : ${per_chunk * corpus_size:.2f}")
    print("=" * 68)


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract KG triples from regulation chunks.")
    ap.add_argument("--all", action="store_true", help="process the whole corpus")
    ap.add_argument("--chunk-id", action="append", help="process specific chunk_ids")
    args = ap.parse_args()

    corpus = load_chunks()
    by_id = {c["chunk_id"]: c for c in corpus}

    if args.all:
        selected = corpus
    else:
        wanted = args.chunk_id or TEST_CHUNK_IDS
        missing = [cid for cid in wanted if cid not in by_id]
        if missing:
            sys.exit(f"chunk_ids not found in corpus: {missing}")
        selected = [by_id[cid] for cid in wanted]

    client = get_client()
    stats = {
        "input_tokens": 0,
        "output_tokens": 0,
        "api_calls": 0,
        "cache_hits": 0,
        "retries": 0,
        "source_id_repairs": 0,
        "truncated": 0,
        "failures": [],
        "api_errors": 0,
        "dangling": 0,
        "orphans": 0,
        "endpoint_violations": [],
        "granularity_misses": [],
    }

    extractions: list[Extraction] = []
    touched = {c["chunk_id"] for c in selected}
    EXTRACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:
        """Persist what we have. Called periodically and on the way out of any
        exit path, so a crash at chunk 900 does not discard 899 chunks of work.
        The disk cache already protects the API spend; this protects the output."""
        write_jsonl(
            EXTRACTIONS_PATH,
            [json.loads(e.model_dump_json()) for e in extractions],
            touched,
        )
        write_jsonl(FAILURES_PATH, stats["failures"], touched)

    for i, chunk in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {chunk['chunk_id']} ... ", end="", flush=True)

        try:
            extraction = extract_chunk(client, chunk, stats)
        except KeyboardInterrupt:
            print("\n\ninterrupted -- flushing completed work before exit")
            flush()
            raise
        except ApiError as api_error:
            # One chunk the API refuses must not end a 1108-chunk run. Record it
            # like any other failure and keep going; a targeted re-run can pick it
            # up later. Retrying here is pointless -- temperature=0 with a fixed
            # seed makes the same request fail the same way.
            body = getattr(api_error, "body", None)
            kind = body.get("error_type") if isinstance(body, dict) else None
            stats["failures"].append({
                "chunk_id": chunk["chunk_id"],
                "error": f"{type(api_error).__name__} "
                         f"{getattr(api_error, 'status_code', '?')}: {kind or api_error}",
                "stage": "api",
            })
            stats["api_errors"] += 1
            print(f"API ERROR ({kind or type(api_error).__name__}) -- skipped")
            if i % FLUSH_EVERY == 0:
                flush()
            continue

        if i % FLUSH_EVERY == 0:
            flush()

        if extraction is None:
            print("FAILED")
            continue
        extractions.append(extraction)

        notes = []
        if dangling := dangling_refs(extraction):
            stats["dangling"] += len(dangling)
            notes.append(f"dangling: {dangling}")
        if orphans := orphan_entities(extraction):
            stats["orphans"] += len(orphans)
            notes.append(f"orphans: {orphans}")
        if violations := endpoint_violations(extraction):
            stats["endpoint_violations"].extend((chunk["chunk_id"], v) for v in violations)
            notes.append(f"bad endpoints: {violations}")
        if bare := granularity_miss(extraction, chunk):
            stats["granularity_misses"].append((chunk["chunk_id"], bare))
            notes.append(f"bare article: {bare}")

        note = ("  !! " + " | ".join(notes)) if notes else ""
        print(f"{len(extraction.entities)} entities, "
              f"{len(extraction.relationships)} relationships{note}")

    # Merge into the existing files rather than truncating them: a targeted
    # re-run of a few chunk_ids must not delete the rows it didn't touch.
    flush()

    print(f"\nupserted {len(extractions)} extractions -> {EXTRACTIONS_PATH}")
    print(f"upserted {len(stats['failures'])} failures    -> {FAILURES_PATH}")
    report(stats, extractions, corpus_size=len(corpus))


if __name__ == "__main__":
    main()
