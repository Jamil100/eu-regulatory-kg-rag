"""One-off correction: AIA Article 3 and GDPR Article 4 are definitions lists that
the general-purpose parser flattened into a single oversized chunk each (2,619 and
1,325 words) -- neither article has numbered paragraph divs, so `paragraphs()`'s
single-block fallback fired instead of splitting per definition.

Replaces those two chunks in place with one chunk per numbered definition
(`definitions()`/`parse_definitions()` in `parser.py`). Every other chunk in both
files is left untouched -- this script only swaps one line for many at the same
position, it does not regenerate the files.

Run once: `python -m src.ingest.rechunk_definitions`
"""

from __future__ import annotations

import json
from pathlib import Path

from src.ingest.chunker import chunk_definitions
from src.ingest.parser import parse_definitions

# (source html, jsonl to patch, regulation, article number, chunk_id to replace)
TARGETS = [
    ("data/eu-ai-act.html", "data/processed/chunks-ai-act.jsonl", "AIA", 3, "aia-art3-para1"),
    ("data/gdpr.html", "data/processed/chunks-gdpr.jsonl", "GDPR", 4, "gdpr-art4-para1"),
]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8")]


def _write_jsonl(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def rechunk(html_path: str, jsonl_path: str, regulation: str, article: int, old_chunk_id: str) -> dict:
    """Replace `old_chunk_id` in `jsonl_path` with the article's definition chunks."""
    path = Path(jsonl_path)
    rows = _read_jsonl(path)
    before = len(rows)

    idx = next(i for i, r in enumerate(rows) if r["chunk_id"] == old_chunk_id)
    new_rows = chunk_definitions(parse_definitions(Path(html_path), article), regulation)
    rows[idx : idx + 1] = new_rows

    _write_jsonl(rows, path)
    return {"before": before, "after": len(rows), "inserted": len(new_rows), "at_index": idx}


if __name__ == "__main__":
    for html_path, jsonl_path, regulation, article, old_id in TARGETS:
        result = rechunk(html_path, jsonl_path, regulation, article, old_id)
        print(
            f"{jsonl_path}: {result['before']} -> {result['after']} chunks "
            f"(removed {old_id}, inserted {result['inserted']} definitions at index {result['at_index']})"
        )
