"""Append-only JSONL log of every routing decision.

The roadmap asks for this in one sentence -- *"Log every decision: question,
route, latency, outcome -- Phase 5 needs this and it cannot be reconstructed
later"* -- and this repo has already learned what happens when a log is written
the obvious way instead.

WHY THIS IS NOT `extract.write_jsonl`.

`src/ingest/extract.py:576` is the only other JSONL writer in the codebase and it
opens the file `"w"`, rebuilding it from the rows it decided to keep. For
*extractions* that is correct -- it is an upsert keyed on `chunk_id`. For
*failures* it destroyed the evidence, and `docs/failure-notes.md` §3 still
records it as `OPEN`:

    For *failures* it means the raw response and validation error -- the only
    evidence of what went wrong -- are gone the moment you re-run, before anyone
    reads them. I recovered the count from console logs; the detail was already
    unrecoverable.

A routing decision is evidence, not state. So this module only ever opens `"a"`,
and there is a test asserting that two runs leave two runs' worth of rows.

TWO SMALLER DECISIONS, BOTH FROM THE SAME PAGE OF THAT FILE.

  * **Flush per row.** `failure-notes.md` records 215 chunks of work surviving as
    29 rows because the writer flushed only at the end of the loop: *"I protected
    the resource that costs money and forgot the one that costs time."* A
    routing sweep is cheap to repeat, but a `/ask` process that dies mid-request
    should still have logged the requests it served.
  * **`outcome` is written as `null`, not omitted.** Phase 5 fills it in after
    grading. A key that appears in later rows and not earlier ones is a schema
    change that every reader then has to handle; a key that is always present and
    sometimes null is not.

`run_id` separates runs without separating files, so `--refresh` twice in an hour
stays one file that `jq`/pandas can group.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.schemas import Route

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "router-decisions.jsonl"


def new_run_id() -> str:
    """One id per process. Sortable first, unique second."""
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


@dataclass
class Decision:
    """One router's answer for one question.

    `raw` is the model's untouched output (None for a deterministic router) and
    `rule` is the rule that fired (None for a model router). Exactly one of them
    is populated, which is what lets a single log hold both arms without a reader
    having to know which produced a row.
    """

    run_id: str
    question: str
    router: str
    route: Route | None
    raw: str | None = None
    rule: str | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None
    linked: list[str] = field(default_factory=list)
    question_id: str | None = None
    gold: str | None = None
    error: str | None = None
    # Phase 5 grades the answer this route produced and fills this in. Present and
    # null from the first row ever written -- see the module docstring.
    outcome: Any = None
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append(decision: Decision, path: Path | None = None) -> None:
    """Append one decision. Never truncates, never rewrites."""
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read(path: Path | None = None) -> list[dict]:
    """Every decision ever logged, oldest first. Empty if the log does not exist."""
    path = path or DEFAULT_PATH
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
