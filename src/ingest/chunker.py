"""Paragraph-level chunker.

Produces deterministic, human-readable chunk IDs like `aia-art26-para1`.

The chunk id is the join key between the vector index and the knowledge graph
and it shows up in user-facing citations, so it must stay stable: no
zero-padding, no hashing, no ordinal counters.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.ingest.annex_parser import parse_annexes
from src.ingest.parser import parse, parse_definitions

REGULATION_BY_STEM = {"eu-ai-act": "AIA", "gdpr": "GDPR"}

# The output filename is derived from the regulation, not from the input stem:
# `eu-ai-act.html` must produce `chunks-ai-act.jsonl`, not `chunks-eu-ai-act.jsonl`.
# These two names are hardcoded downstream (`extract.py` CHUNK_FILES, the embedder,
# the test fixtures), so the slugs are load-bearing and must not be "tidied".
OUTPUT_SLUG_BY_REGULATION = {"AIA": "ai-act", "GDPR": "gdpr"}

# The definitions article of each regulation. It has no numbered paragraph divs,
# so `paragraphs()`'s fallback flattens all 68 entries into one 2,619-word blob;
# `parse_definitions()` re-reads it per entry instead. See `splice_definitions`.
DEFINITIONS_ARTICLE_BY_REGULATION = {"AIA": 3, "GDPR": 4}

PROCESSED_DIR = Path(__file__).resolve().parents[2] / "data" / "processed"


def output_path(regulation: str) -> Path:
    """Where the chunks for one regulation are written.

    Previously hardcoded to `data/processed/chunks.jsonl`, which meant the second
    run overwrote the first and the file had to be renamed by hand afterwards.
    """
    return PROCESSED_DIR / f"chunks-{OUTPUT_SLUG_BY_REGULATION[regulation]}.jsonl"


def make_chunk_id(regulation: str, article: int, paragraph: int) -> str:
    """Deterministic, human-readable chunk id, e.g. `aia-art26-para1`."""
    return f"{regulation.lower()}-art{article}-para{paragraph}"


def make_annex_chunk_id(regulation: str, annex: int, section: str | None, point: int) -> str:
    """Annex chunk id, e.g. `aia-annex3-point1` or `aia-annex8-sectionB-point1`.

    The section appears only for annexes whose point numbers restart per section
    (VIII and XI); without it those ids would collide and silently corrupt the
    vector-index/graph join.
    """
    section_part = f"-section{section}" if section else ""
    return f"{regulation.lower()}-annex{annex}{section_part}-point{point}"


def chunk(articles: list[dict], regulation: str) -> list[dict]:
    """Split parsed structure at the paragraph level into chunk records."""
    rows: list[dict] = []
    for article in articles:
        for para in article["paragraphs"]:
            rows.append(
                {
                    "chunk_id": make_chunk_id(regulation, article["article"], para["paragraph"]),
                    "regulation": regulation,
                    "article": article["article"],
                    "article_title": article["article_title"],
                    "paragraph": para["paragraph"],
                    "text": para["text"],
                    "token_count": len(para["text"].split()),
                }
            )
    return rows


def make_definition_chunk_id(regulation: str, article: int, definition: int) -> str:
    """Deterministic chunk id for a numbered definition, e.g. `aia-art3-def37`.

    The number is the definition's own number from the source, not a running
    counter -- it has to match how the law cites itself ("point (37) of Article 3").
    """
    return f"{regulation.lower()}-art{article}-def{definition}"


def chunk_definitions(parsed: dict, regulation: str) -> list[dict]:
    """One chunk per numbered definition. No `paragraph` key, unlike `chunk()`."""
    rows: list[dict] = []
    for d in parsed["definitions"]:
        rows.append(
            {
                "chunk_id": make_definition_chunk_id(
                    regulation, parsed["article"], d["definition"]
                ),
                "regulation": regulation,
                "article": parsed["article"],
                "article_title": parsed["article_title"],
                "definition": d["definition"],
                "text": d["text"],
                "token_count": len(d["text"].split()),
            }
        )
    return rows


def splice_definitions(
    articles: list[dict], definition_rows: list[dict], definitions_article: int
) -> list[dict]:
    """Swap the definitions article's paragraph rows for its per-definition rows.

    In place, not appended: the corpus is in source order, so `aia-art3-def1`
    follows `aia-art2-para12` and precedes `aia-art4-para1`.
    """
    head = [row for row in articles if row["article"] < definitions_article]
    tail = [row for row in articles if row["article"] > definitions_article]
    return head + definition_rows + tail


def chunk_annexes(annexes: list[dict], regulation: str) -> list[dict]:
    """Split parsed annex structure at the point level into chunk records.

    Annex rows carry `annex`/`point` where article rows carry `article`/`paragraph`;
    downstream code branches on which keys are present, so there is no shared field.

    `section` is written out as its own field and not only folded into the id.
    It was id-only until 2026-07-31, which made `Annex VIII(1)` name three
    different provisions (Sections A, B and C) as far as any consumer reading
    the fields was concerned -- 25 chunks over 11 ambiguous locators. The
    parser had the value all along; the chunker used it and threw it away.
    """
    rows: list[dict] = []
    for annex in annexes:
        for p in annex["points"]:
            rows.append(
                {
                    "chunk_id": make_annex_chunk_id(
                        regulation, annex["annex_arabic"], p["section"], p["point"]
                    ),
                    "regulation": regulation,
                    "annex": annex["annex"],
                    "annex_title": annex["annex_title"],
                    **({"section": p["section"]} if p["section"] else {}),
                    "point": p["point"],
                    "text": p["text"],
                    "token_count": len(p["text"].split()),
                }
            )
    return rows


def write_jsonl(rows: list[dict], out_path: Path) -> None:
    """Write one JSON object per line, keeping `’` and `—` readable."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m src.ingest.chunker <html_path>", file=sys.stderr)
        return 2

    html_path = Path(argv[0])
    regulation = REGULATION_BY_STEM.get(html_path.stem)
    if regulation is None:
        known = ", ".join(sorted(REGULATION_BY_STEM))
        print(f"cannot infer regulation from {html_path.name!r} (known: {known})", file=sys.stderr)
        return 2

    articles = chunk(parse(html_path), regulation)
    definitions_article = DEFINITIONS_ARTICLE_BY_REGULATION[regulation]
    definition_rows = chunk_definitions(
        parse_definitions(html_path, definitions_article), regulation
    )
    articles = splice_definitions(articles, definition_rows, definitions_article)
    annexes = chunk_annexes(parse_annexes(html_path), regulation)
    rows = articles + annexes
    out_path = output_path(regulation)
    write_jsonl(rows, out_path)
    paragraphs = len(articles) - len(definition_rows)
    print(
        f"{len(rows)} chunks ({paragraphs} paragraph + {len(definition_rows)} definition "
        f"+ {len(annexes)} annex) -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
