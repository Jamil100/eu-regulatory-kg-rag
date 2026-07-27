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
from src.ingest.parser import parse

REGULATION_BY_STEM = {"eu-ai-act": "AIA", "gdpr": "GDPR"}


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


def chunk_annexes(annexes: list[dict], regulation: str) -> list[dict]:
    """Split parsed annex structure at the point level into chunk records.

    Annex rows carry `annex`/`point` where article rows carry `article`/`paragraph`;
    downstream code branches on which keys are present, so there is no shared field.
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
    annexes = chunk_annexes(parse_annexes(html_path), regulation)
    rows = articles + annexes
    out_path = Path("data/processed/chunks.jsonl")
    write_jsonl(rows, out_path)
    print(f"{len(rows)} chunks ({len(articles)} article + {len(annexes)} annex) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
