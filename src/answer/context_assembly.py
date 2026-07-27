"""Context assembly: dedupe by chunk_id across graph + vector paths.

Each document is labeled [GRAPH] or [PASSAGE] in its metadata before being
passed to Command A via the documents parameter.
"""

from __future__ import annotations

from src.schemas import Chunk


def assemble(graph_docs: list[Chunk], passage_docs: list[Chunk]) -> list[dict]:
    """Merge and dedupe context documents, labeling their source path."""
    raise NotImplementedError
