"""EUR-Lex HTML -> Annex structure (Annex > numbered point > inlined sub-points).

Companion to `parser.py`, which handles Articles. All text extraction is reused from
there: `text_of()` already inlines nested tables in order, drops footnote markers,
keeps `oj-italic` runs and normalises `&#160;` — which is exactly the sub-point
inlining rule the annexes need.

Unlike articles, the annexes do not share one markup scheme. Four layouts occur,
and several annexes mix them:

  A   2-column table   `<td>` marker + `<td>` text            III, IV, V, IX, XII, XIII
  A3  3-column table   empty `<td>` + marker + text           I, VII, XI
  B   heading          `<p class="oj-ti-grseq-1">1. Intro</p>` VII, X, XI, VIII
  C   div block        `<div class="oj-enumeration-spacing">`  VI (number inline in the text)

Two traps drive the design:

1. Point numbers are NOT unique within an annex. Annex VIII restarts at 1 in each of
   Sections A/B/C; Annex XI restarts across Section 1/2. The section is therefore
   folded into the chunk id, but only where the number would otherwise repeat --
   Annex I has sections too, yet numbers straight through 1..20, so it stays plain.
2. Sub-points that sit at the SAME level as their parent must be re-attached. Annex
   VII's tables are marked `3.1`/`4.2` and belong to heading `3.`/`4.`; Annex X's
   `(a)/(b)` tables belong to the preceding `1. Schengen Information System` heading.
   Only a bare `N.` marker opens a new point; everything else appends to the open one.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.ingest.parser import _classes, _normalise, _tag, load_body, text_of

ANNEX_ID = re.compile(r"anx_([IVX]+)")
SECTION_HEADING = re.compile(r"^Section\s+([A-Za-z0-9]+)\b")
NUMBERED_HEADING = re.compile(r"^(\d+)\.\s*")
TOP_LEVEL_MARKER = re.compile(r"\d+\.")

ROMAN_TO_ARABIC = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
    "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
}

# Annex II is 16 em-dash bullets totalling 88 words ("terrorism,", "rape,",
# "sabotage,"). Split per the usual rule they embed to near-noise, so the annex is
# emitted as a single chunk carrying its intro plus the whole list.
COLLAPSE_TO_ONE_CHUNK = {"II"}


def _is_title(el) -> bool:
    return _tag(el) == "p" and "oj-doc-ti" in _classes(el)


def _body_children(annex):
    return [child for child in annex if not _is_title(child)]


def iter_annexes(root) -> list[tuple[str, object]]:
    """Every ``<div id="anx_ROMAN">`` with its Roman numeral."""
    found = []
    for el in root.iter():
        if _tag(el) != "div":
            continue
        match = ANNEX_ID.fullmatch(el.get("id", ""))
        if match:
            found.append((match.group(1), el))
    return found


def annex_title(annex) -> str:
    """The descriptive title -- the SECOND ``p.oj-doc-ti`` ("ANNEX III" is the first)."""
    titles = [text_of(child) for child in annex if _is_title(child)]
    return titles[1] if len(titles) > 1 else ""


def _rows(table):
    """Yield (marker, text) for each row, tolerating 2- and 3-column tables."""
    for block in table:
        if _tag(block) not in ("tbody", "thead", "tfoot"):
            continue
        for row in block:
            if _tag(row) != "tr":
                continue
            cells = [text_of(cell) for cell in row if _tag(cell) in ("td", "th")]
            if not cells:
                continue
            # 3-column rows lead with an empty spacer cell; the marker is the first
            # non-empty cell and the text is always the last.
            marker = next((c for c in cells[:-1] if c), "")
            yield marker, cells[-1]


def points(annex, roman: str) -> list[dict]:
    """Top-level points of an annex, sub-points inlined into their parent."""
    if roman in COLLAPSE_TO_ONE_CHUNK:
        text = _normalise(" ".join(text_of(c) for c in _body_children(annex)))
        return [{"point": 1, "section": None, "text": text}]

    collected: list[dict] = []
    section: str | None = None
    # True when the open point came from a heading or a bare `N.` marker and can
    # therefore absorb following `(a)`/`3.1` rows as sub-points (Annexes VII, X).
    # False when it came from the lettered fallback below, where each `(a)`, `(b)`
    # is itself a top-level point (Annex XIII) and must not swallow its siblings.
    absorbs_subpoints = False

    def open_point(number: int, text: str, absorbs: bool) -> None:
        nonlocal absorbs_subpoints
        collected.append({"point": number, "section": section, "parts": [text]})
        absorbs_subpoints = absorbs

    def append(text: str) -> None:
        if text:
            collected[-1]["parts"].append(text)

    for child in _body_children(annex):
        tag, classes = _tag(child), _classes(child)

        if tag == "p" and "oj-ti-grseq-1" in classes:
            heading = text_of(child)
            match = SECTION_HEADING.match(heading)
            if match:
                section = match.group(1)
                continue
            match = NUMBERED_HEADING.match(heading)
            if match:
                open_point(int(match.group(1)), heading[match.end() :], absorbs=True)
            # An unnumbered heading is a section subtitle (Annex XI) -- carries no text.
            continue

        if tag == "div" and "oj-enumeration-spacing" in classes:
            body = text_of(child)
            match = NUMBERED_HEADING.match(body)
            number = int(match.group(1)) if match else len(collected) + 1
            open_point(number, body[match.end() :] if match else body, absorbs=True)
            continue

        if tag == "table":
            for marker, text in _rows(table=child):
                labelled = f"{marker} {text}" if marker else text
                if TOP_LEVEL_MARKER.fullmatch(marker):
                    open_point(int(marker.rstrip(".")), text, absorbs=True)
                elif absorbs_subpoints:
                    # `3.1`, `(a)`, `(i)` -- a sub-point of the point already open.
                    append(labelled)
                else:
                    # Lettered rows with no parent heading ARE the top level here
                    # (Annex XIII's (a)-(g)); number them in document order.
                    open_point(len(collected) + 1, labelled, absorbs=False)
            continue

        if tag == "p" and "oj-normal" in classes:
            # Before the first point this is the annex intro, which the title already
            # conveys; after one, it is that point's body.
            if collected:
                append(text_of(child))
            continue

    return [
        {"point": p["point"], "section": p["section"], "text": _normalise(" ".join(p["parts"]))}
        for p in collected
    ]


def parse_annexes(html_path: Path) -> list[dict]:
    """Parse the annexes of a EUR-Lex file into annex/point structure.

    Returns one dict per annex::

        {"annex": "III", "annex_arabic": 3,
         "annex_title": "High-risk AI systems referred to in Article 6(2)",
         "points": [{"point": 1, "section": None, "text": "..."}, ...]}
    """
    root = load_body(html_path)
    out = []
    for roman, el in iter_annexes(root):
        collected = points(el, roman)
        # Fold the section into the id only where the point number would otherwise
        # repeat: Annexes VIII and XI restart per section, Annex I does not.
        numbers = [p["point"] for p in collected]
        needs_section = len(set(numbers)) != len(numbers)
        if not needs_section:
            for p in collected:
                p["section"] = None
        out.append(
            {
                "annex": roman,
                "annex_arabic": ROMAN_TO_ARABIC[roman],
                "annex_title": annex_title(el),
                "points": collected,
            }
        )
    return out


if __name__ == "__main__":
    raise SystemExit("Use `python -m src.ingest.chunker <html_path>` instead.")
