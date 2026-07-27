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

QUESTIONS = Path(__file__).parent / "questions.jsonl"


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]


def run() -> None:
    """Execute all three systems and print the benchmark table."""
    raise NotImplementedError


if __name__ == "__main__":
    run()
