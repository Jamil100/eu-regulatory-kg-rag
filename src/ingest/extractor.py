"""Command A entity/relationship extraction, constrained to the fixed ontology.

Calls Command A per chunk with the ontology in the system prompt + few-shot
examples, validates with Pydantic, retries once on failure with the validation
error appended. Results cached by sha256(chunk_text).
"""

from __future__ import annotations

from src.schemas import Chunk, Extraction


def extract(chunk: Chunk) -> Extraction:
    """Extract ontology-constrained entities and relations from one chunk."""
    raise NotImplementedError


def extract_all(chunks: list[Chunk]) -> list[Extraction]:
    """Run extraction over the corpus with caching + failure-rate logging."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit("TODO: wire up extraction CLI")
