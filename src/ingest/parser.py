"""EUR-Lex HTML/XML -> structured hierarchy (Regulation > Chapter > Article > Paragraph > Point).

Keeps raw text verbatim so citations can quote the real sentence.
"""

from __future__ import annotations

from pathlib import Path

from src.schemas import Chunk


def parse(html_path: Path, regulation: str) -> list[Chunk]:
    """Parse a EUR-Lex consolidated HTML file into structured chunks.

    TODO: implement structure-aware parsing with BeautifulSoup/lxml.
    """
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit("TODO: wire up parser CLI")
