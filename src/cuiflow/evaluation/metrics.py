"""Precision, recall and F1 over mention sets.

The gold-set loader is in ``gold.py`` and the span matching and per-attribute (assertion)
scoring in ``scoring.py``. For assertions, score against adjudicated verdicts, not raw cTAKES
labels (DESIGN_PLAN §11).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cuiflow.core.models import Mention

Key = tuple[str, int, int]  # (cui, start, end)


@dataclass(frozen=True, slots=True)
class Score:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def keys(mentions: Iterable[Mention]) -> set[Key]:
    return {(m.cui, m.start, m.end) for m in mentions}


def score_mentions(gold: Iterable[Key], predicted: Iterable[Key]) -> Score:
    """Exact-match scoring on ``(cui, start, end)``."""
    g, p = set(gold), set(predicted)
    return Score(tp=len(g & p), fp=len(p - g), fn=len(g - p))
