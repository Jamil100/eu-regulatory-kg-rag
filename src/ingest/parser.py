"""EUR-Lex HTML/XML -> structured hierarchy (Regulation > Chapter > Article > Paragraph > Point).

Keeps raw text verbatim so citations can quote the real sentence.

The source is CONVEX-generated XHTML (ELI 0.2): every article is a
``<div class="eli-subdivision" id="art_N">``, every numbered paragraph a
``<div id="NNN.MMM">`` *directly inside it*, and points ``(a)``/``(i)`` are
``<table>`` markup rather than lists.

The direct-child restriction on paragraph divs is load-bearing: Articles
105-109 are amendment articles that quote text destined for other regulations,
and that quoted text carries its own ``NNN.MMM`` ids (e.g. ``008.005``, which is
*not* Article 8(5) - Article 8 has two paragraphs). A document-wide search for
those ids silently corrupts Articles 5, 8, 11, 17, 19, 43, 47 and 58; the nine
decoys all sit at depth >= 2, so ``list(article_div)`` excludes them.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

ARTICLE_ID = re.compile(r"art_(\d+)")
PARAGRAPH_ID = re.compile(r"\d{3}\.\d{3}")
PARAGRAPH_MARKER = re.compile(r"^(\d+)\.\s+")
WHITESPACE = re.compile(r"[\s ]+")

# Tags whose boundaries are meaningful word separators. Inline tags (notably
# <span class="oj-italic">) are deliberately absent so that "apply mutatis
# mutandis." does not become "apply mutatis mutandis ."
BLOCK_TAGS = frozenset({"div", "p", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "br"})


def load_body(html_path: Path) -> ET.Element:
    """Parse the ``<body>`` of a EUR-Lex HTML file into an ElementTree root.

    The document is well-formed XHTML except for ``<head>`` (unclosed ``<meta>``
    and ``<script>`` tags), so slicing out the body yields valid XML that the
    stdlib parser accepts. Slicing also drops the ``xmlns`` declaration on
    ``<html>``, which keeps tag names unprefixed.
    """
    raw = Path(html_path).read_text(encoding="utf-8")
    start = raw.index("<body")
    end = raw.index("</body>") + len("</body>")
    return ET.fromstring(raw[start:end])


def _tag(el: ET.Element) -> str:
    """Local tag name, namespace-stripped."""
    return el.tag.rpartition("}")[2]


def _classes(el: ET.Element) -> set[str]:
    return set(el.get("class", "").split())


def _normalise(text: str) -> str:
    """Collapse whitespace (including the pervasive `&#160;`) and strip."""
    return WHITESPACE.sub(" ", text).strip()


def _clean_title(text: str) -> str:
    """Drop stray typographic artifacts from a title.

    The source carries exactly one: `<p class="oj-sti-art">Subject matter`</p>` in
    Article 1 -- the only backtick in the whole file. Body text is left verbatim so
    citations still quote the real sentence; only titles are normalised.
    """
    return _normalise(text.replace("`", ""))


def _walk(el: ET.Element) -> list[str]:
    """Recursively collect text fragments, tables handled row by row."""
    out: list[str] = []
    if el.text:
        out.append(el.text)
    for child in el:
        tag = _tag(child)
        if tag == "span" and "oj-note-tag" in _classes(child):
            pass  # footnote marker: drop the digit, keep the tail
        elif tag == "table":
            out.extend(_table_rows(child))
        else:
            if tag in BLOCK_TAGS:
                out.append(" ")
            out.extend(_walk(child))
            if tag in BLOCK_TAGS:
                out.append(" ")
        if child.tail:
            out.append(child.tail)
    return out


def _table_rows(table: ET.Element) -> list[str]:
    """Render a points table as ``marker text`` per row.

    Nested tables (roman-numeral points) recurse through ``_walk`` and so stay
    inside the text of their parent point.
    """
    rows: list[ET.Element] = []
    for child in table:
        tag = _tag(child)
        if tag in ("tbody", "thead", "tfoot"):
            rows.extend(row for row in child if _tag(row) == "tr")
        elif tag == "tr":
            rows.append(child)

    out: list[str] = []
    for row in rows:
        cells = [_normalise("".join(_walk(cell))) for cell in row if _tag(cell) in ("td", "th")]
        out.append(" " + " ".join(cell for cell in cells if cell) + " ")
    return out


def text_of(el: ET.Element) -> str:
    """Clean prose for an element: no markup, no footnote markers, no `&#160;`."""
    return _normalise("".join(_walk(el)))


def iter_articles(root: ET.Element) -> list[tuple[int, ET.Element]]:
    """Every ``<div id="art_N">`` with its article number.

    Scoping to that id pattern also excludes the Annexes and the footnote block
    ``<div id="fnp_1">``, which is a sibling of the last article, not a child.
    """
    found: list[tuple[int, ET.Element]] = []
    for el in root.iter():
        if _tag(el) != "div":
            continue
        match = ARTICLE_ID.fullmatch(el.get("id", ""))
        if match:
            found.append((int(match.group(1)), el))
    return found


def article_title(art: ET.Element) -> str:
    """Text of ``div.eli-title > p.oj-sti-art``, or "" if the article is untitled."""
    for child in art:
        if _tag(child) == "div" and "eli-title" in _classes(child):
            for sub in child:
                if _tag(sub) == "p" and "oj-sti-art" in _classes(sub):
                    return _clean_title(text_of(sub))
    return ""


def paragraphs(art: ET.Element) -> list[dict]:
    """Numbered paragraphs of an article, or a single paragraph 1 if it has none.

    Only *direct* children are considered - see the module docstring on the
    Article 105-109 decoys.
    """
    para_divs = [
        child
        for child in art
        if _tag(child) == "div" and PARAGRAPH_ID.fullmatch(child.get("id", ""))
    ]

    if not para_divs:
        # 18 articles (4, 16, 32, 39, 66, 85, 87, 94, 102-110, 113) are a single
        # block of prose. Article 3 (Definitions) has the same no-numbered-div
        # shape but is a list of 68 entries, not one block -- see
        # `definitions()`/`parse_definitions()` below, which the chunker uses for
        # it and for GDPR Article 4 instead of this fallback.
        parts = [
            text_of(child)
            for child in art
            if not (_tag(child) == "p" and "oj-ti-art" in _classes(child))
            and not (_tag(child) == "div" and "eli-title" in _classes(child))
        ]
        return [{"paragraph": 1, "text": _normalise(" ".join(p for p in parts if p))}]

    out: list[dict] = []
    for div in para_divs:
        text = text_of(div)
        fallback = int(div.get("id").split(".")[1])
        match = PARAGRAPH_MARKER.match(text)
        if match:
            # The printed "10.   " marker is the source of truth; the id suffix
            # is only a fallback. They agree throughout both regulations.
            out.append({"paragraph": int(match.group(1)), "text": text[match.end() :]})
        else:
            out.append({"paragraph": fallback, "text": text})
    return out


def parse(html_path: Path) -> list[dict]:
    """Parse a EUR-Lex consolidated HTML file into article/paragraph structure.

    Returns one dict per article::

        {"article": 26, "article_title": "Obligations of deployers ...",
         "paragraphs": [{"paragraph": 1, "text": "..."}, ...]}
    """
    root = load_body(html_path)
    return [
        {
            "article": number,
            "article_title": article_title(art),
            "paragraphs": paragraphs(art),
        }
        for number, art in iter_articles(root)
    ]


DEFINITION_MARKER = re.compile(r"\((\d+)\)")


def definitions(art) -> list[dict]:
    """Numbered definitions of a definitions-style article (AIA Art 3, GDPR Art 4).

    These articles have no numbered paragraph divs -- `paragraphs()`'s fallback
    would flatten them into one oversized chunk (2,619 words for AIA Art 3). The
    actual shape is one `<table>` per entry, marker "(37)" in the first cell, text
    in the second. `text_of()` on that second cell already inlines any nested
    sub-point tables (e.g. AIA def(45)/(49)/(61), GDPR def(16)/(22)/(23)), so this
    only needs to split the top-level tables, not re-implement text extraction.
    """
    out: list[dict] = []
    for child in art:
        if _tag(child) != "table":
            continue
        cells: list[str] = []
        for block in child:
            if _tag(block) not in ("tbody", "thead", "tfoot"):
                continue
            for row in block:
                if _tag(row) != "tr":
                    continue
                cells = [text_of(cell) for cell in row if _tag(cell) in ("td", "th")]
        if len(cells) < 2:
            continue
        match = DEFINITION_MARKER.fullmatch(cells[0])
        if match:
            out.append({"definition": int(match.group(1)), "text": cells[-1]})
    return out


def parse_definitions(html_path: Path, article: int) -> dict:
    """Parse a definitions-style article into `parse()`-shaped structure.

    Returns ``{"article": 3, "article_title": "Definitions",
    "definitions": [{"definition": 1, "text": "..."}, ...]}`` -- the same shape as
    one element of `parse()`'s output, with `definitions` in place of `paragraphs`.
    """
    root = load_body(html_path)
    art = next(el for el in root.iter() if _tag(el) == "div" and el.get("id") == f"art_{article}")
    return {
        "article": article,
        "article_title": article_title(art),
        "definitions": definitions(art),
    }


if __name__ == "__main__":
    raise SystemExit("Use `python -m src.ingest.chunker <html_path>` instead.")
