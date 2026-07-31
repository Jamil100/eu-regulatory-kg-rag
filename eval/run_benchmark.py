"""Run three systems over the full eval set and report per-stratum accuracy.

Systems:
  (a) vector-only
  (b) vector + Rerank 3.5
  (c) full hybrid (graph + vector)

Also reports latency p50/p95, cost/query, and one-time ingestion cost.
"""

from __future__ import annotations

import json
from pathlib import Path

QUESTIONS = Path(__file__).parent / "eval-questions.jsonl"

# Rows carrying `expected_fail` are known-red for a recorded reason -- currently
# 3h-002, where the extractor emits PERMITS on a derogation that requires
# EXEMPT_FROM. They are reported in their own bucket: counting them as passes
# would silence the canary, and counting them as system failures would blame the
# retrieval stack for an extraction gap. See docs/adr/adr-0007-lawfulbasis-permits.md.
EXPECTED_FAIL_BUCKET = "expected_fail"


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]


def bucket_of(row: dict) -> str | None:
    """Which reporting bucket a row belongs to, or None for the normal path."""
    if row.get("expected_fail", {}).get("reason", "").strip():
        return EXPECTED_FAIL_BUCKET
    return None


def run() -> None:
    """Execute all three systems and print the benchmark table."""
    raise NotImplementedError


if __name__ == "__main__":
    run()
