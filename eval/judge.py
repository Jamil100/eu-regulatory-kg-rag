"""LLM judge + agreement check.

Grades answers as correct / partially correct / wrong / correct refusal against
the gold answer (Command A, temperature 0). Hand-verify a 20% sample to report
judge agreement.
"""

from __future__ import annotations

from typing import Literal

Verdict = Literal["correct", "partially_correct", "wrong", "correct_refusal"]


def judge(question: str, gold: str, answer: str) -> Verdict:
    """Grade a single answer against its gold answer."""
    raise NotImplementedError
