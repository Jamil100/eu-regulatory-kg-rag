"""Ontology-constrained entity/relationship extraction with Cohere Command A.

Reads chunks from the two JSONL files, calls Command A once per chunk with the
fixed ontology in the system prompt, validates the JSON response with Pydantic,
retries once on ValidationError, and writes one Extraction per line to
extractions.jsonl. Unparseable chunks land in failures.jsonl instead of
crashing the run.

Responses are cached on disk by content hash, so reruns after a bug fix cost
nothing for chunks whose text and prompt are unchanged.

Usage:
    python -m src.ingest.extract              # the 10 test chunks + cost report
    python -m src.ingest.extract --all        # full corpus (don't, yet)
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
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

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
MAX_TOKENS = 4096

# Cohere list price for Command A, USD per token. Stated here so the cost
# estimate is auditable -- check these against cohere.com/pricing.
PRICE_INPUT_PER_TOKEN = 2.50 / 1_000_000
PRICE_OUTPUT_PER_TOKEN = 10.00 / 1_000_000

# The 10 chunks used to sanity-check the ontology before spending on the corpus.
TEST_CHUNK_IDS = [
    "aia-art9-para1",
    "aia-art26-para6",
    "aia-art43-para1",
    "aia-art8-para2",
    "aia-annex3-point1",
    "aia-art3-def37",
    "aia-art3-def39",
    "gdpr-art9-para1",
    "gdpr-art9-para2",
    "gdpr-art6-para1",
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
]


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

ENTITY TYPES (use exactly these 9 strings):
- Regulation: a whole legal instrument, e.g. "AIA", "GDPR", "Directive (EU) 2016/680".
- Article: one numbered article, namespaced by regulation, e.g. "AIA Art. 9", "GDPR Art. 9(1)".
- Annex: one annex of a regulation, e.g. "AIA Annex III", "AIA Annex VII".
- ActorRole: a role a person or organisation plays, e.g. "provider", "deployer", "controller".
- Obligation: a duty or prohibition, written as a short verb phrase.
- RiskCategory: a risk classification, e.g. "high-risk", "prohibited", "special category of personal data".
- SystemType: a kind of AI or technical system, e.g. "emotion recognition system", "risk management system".
- Authority: a body with oversight, certification or enforcement power, e.g. "notified body", "supervisory authority".
- LawfulBasis: a ground that makes an activity lawful or lifts a prohibition (e.g. consent, contract, legitimate interests, a derogation). NOT an obligation -- it permits, it does not require.

RELATIONSHIP TYPES (use exactly these 11 strings):
- DEFINED_IN: a term or entity is defined by the provision. head=term, tail=Article/Annex.
- IMPOSES: a provision or regulation creates a duty. head=Article/Regulation, tail=Obligation.
- APPLIES_TO: a duty, provision or classification governs someone or something. head=Obligation/Article/RiskCategory, tail=ActorRole/SystemType.
- CLASSIFIED_AS: something is assigned a risk classification. head=SystemType/entity, tail=RiskCategory.
- LISTED_IN: an item appears in an enumerated list. head=SystemType/entity, tail=Annex/Article.
- REFERENCES: one provision cross-refers to another. head=Article/Annex, tail=Article/Annex.
- ENFORCED_BY: a duty or regulation is supervised or enforced. head=Obligation/Regulation/Article, tail=Authority.
- PENALIZED_UNDER: breaching a duty is sanctioned by a provision. head=Obligation, tail=Article.
- EXEMPT_FROM: someone or something is carved out of a duty or prohibition. head=ActorRole/SystemType/Obligation, tail=Obligation/Article.
- INTERACTS_WITH: two legal instruments or regimes operate together across a boundary. head/tail=Regulation/Article.
- PERMITS: head makes tail lawful/justified/allowed. Use for "processing is lawful if X", "the prohibition does not apply where Y", derogations, and exceptions. This is the permissive counterpart of IMPOSES -- never use IMPOSES for a permission. head=Article/Regulation, tail=LawfulBasis.

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

RULES
1. Extract ONLY what THIS chunk's text states. Do not add facts you know about \
the regulation from elsewhere. If the text does not mention penalties, emit no \
PENALIZED_UNDER edge.
2. Every relationship's "source_chunk_id" MUST be the chunk_id you were given.
3. Every "head" and "tail" MUST exactly match the "canonical_name" of an entity \
you listed in "entities".
4. "confidence" is your honest 0.0-1.0 confidence that the text really asserts \
that relationship. Use the full range: an edge stated explicitly deserves ~0.95, \
one you inferred from phrasing deserves ~0.5-0.7. Do not mark everything 1.0.
5. Return ONLY a JSON object. No prose, no explanation, no markdown fences.

CANONICAL NAME NORMALISATION
- Role names lowercase and singular: "deployer", not "Deployers".
- Obligations as short verb phrases: "keep automatically generated logs", not a \
sentence copied from the text.
- Articles namespaced by regulation, because numbers collide: "AIA Art. 9", \
"GDPR Art. 9". Keep the sub-number when the text cites one: "GDPR Art. 9(1)".
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
{"chunk_id":"aia-art17-para1","entities":[{"type":"Article","canonical_name":"AIA Art. 17","aliases":[]},{"type":"ActorRole","canonical_name":"provider","aliases":["providers"]},{"type":"SystemType","canonical_name":"high-risk AI system","aliases":[]},{"type":"RiskCategory","canonical_name":"high-risk","aliases":[]},{"type":"Obligation","canonical_name":"put a quality management system in place","aliases":[]}],"relationships":[{"type":"IMPOSES","head":"AIA Art. 17","tail":"put a quality management system in place","source_chunk_id":"aia-art17-para1","confidence":0.96},{"type":"APPLIES_TO","head":"put a quality management system in place","tail":"provider","source_chunk_id":"aia-art17-para1","confidence":0.95},{"type":"APPLIES_TO","head":"put a quality management system in place","tail":"high-risk AI system","source_chunk_id":"aia-art17-para1","confidence":0.88},{"type":"CLASSIFIED_AS","head":"high-risk AI system","tail":"high-risk","source_chunk_id":"aia-art17-para1","confidence":0.9}]}

EXAMPLE 2
chunk_id: aia-art3-def12
text: 'biometric data' means personal data resulting from specific technical \
processing relating to the physical characteristics of a natural person, as \
defined in Article 4(14) of Regulation (EU) 2016/679;
output:
{"chunk_id":"aia-art3-def12","entities":[{"type":"Article","canonical_name":"AIA Art. 3","aliases":["Article 3"]},{"type":"Article","canonical_name":"GDPR Art. 4(14)","aliases":["Article 4(14) of Regulation (EU) 2016/679"]},{"type":"Regulation","canonical_name":"GDPR","aliases":["Regulation (EU) 2016/679"]},{"type":"RiskCategory","canonical_name":"biometric data","aliases":[]}],"relationships":[{"type":"DEFINED_IN","head":"biometric data","tail":"AIA Art. 3","source_chunk_id":"aia-art3-def12","confidence":0.97},{"type":"REFERENCES","head":"AIA Art. 3","tail":"GDPR Art. 4(14)","source_chunk_id":"aia-art3-def12","confidence":0.95},{"type":"INTERACTS_WITH","head":"AIA Art. 3","tail":"GDPR","source_chunk_id":"aia-art3-def12","confidence":0.8}]}

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


def call_model(client: cohere.ClientV2, messages: list[dict]) -> tuple[str, int, int, str]:
    """One Command A call. Returns (text, input_tokens, output_tokens, finish_reason)."""
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
    }

    extractions: list[Extraction] = []
    for i, chunk in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {chunk['chunk_id']} ... ", end="", flush=True)
        extraction = extract_chunk(client, chunk, stats)
        if extraction is None:
            print("FAILED")
            continue
        extractions.append(extraction)
        dangling = dangling_refs(extraction)
        note = f"  !! dangling: {dangling}" if dangling else ""
        print(f"{len(extraction.entities)} entities, "
              f"{len(extraction.relationships)} relationships{note}")

    # Merge into the existing files rather than truncating them: a targeted
    # re-run of a few chunk_ids must not delete the rows it didn't touch.
    touched = {c["chunk_id"] for c in selected}
    EXTRACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        EXTRACTIONS_PATH,
        [json.loads(e.model_dump_json()) for e in extractions],
        touched,
    )
    write_jsonl(FAILURES_PATH, stats["failures"], touched)

    print(f"\nupserted {len(extractions)} extractions -> {EXTRACTIONS_PATH}")
    print(f"upserted {len(stats['failures'])} failures    -> {FAILURES_PATH}")
    report(stats, extractions, corpus_size=len(corpus))


if __name__ == "__main__":
    main()
