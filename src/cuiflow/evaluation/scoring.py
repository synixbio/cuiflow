"""Scoring predictions against a reference set.

Concept extraction is scored three ways, because the engines segment text differently and one
number would hide that:

- ``exact``: same CUI, same ``(start, end)``.
- ``overlap``: same CUI, overlapping spans after trimming edge punctuation, matched one-to-one.
  This is the headline figure.
- ``cui_set``: the set of CUIs per document, ignoring position (umlsmatch's parity metric).

``exact`` and ``cui_set`` compare sets, so a prediction repeated on the same span (or CUI)
counts once. ``overlap`` counts mentions: an unmatched duplicate is a false positive. A
reference must name each document once.

Assertions are scored on overlap-matched pairs where the reference assessed the attribute:
``coverage`` is the share the prediction also assessed, and P/R/F1 treat the "positive" value
(True; ``subject`` != patient) as the positive class. These are **agreement with the reference's
labels**, which is accuracy only for a human-annotated reference.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cuiflow.core.alignment import match_concepts
from cuiflow.core.models import Mention
from cuiflow.evaluation.metrics import Score
from cuiflow.evaluation.reference import ReferenceDoc, ReferenceMention

#: Attributes scored for agreement, and each one's "positive" test.
POSITIVE: dict[str, Callable[[Any], bool]] = {
    "negated": lambda v: v is True,
    "history_of": lambda v: v is True,
    "uncertain": lambda v: v is True,
    "conditional": lambda v: v is True,
    "subject": lambda v: v is not None and v != "patient",
}


@dataclass
class AttributeScore:
    reference_assessed: int = 0  # matched pairs where the reference has a label
    predicted_assessed: int = 0  # ... and the prediction has one too
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def coverage(self) -> float:
        return self.predicted_assessed / self.reference_assessed if self.reference_assessed else 0.0

    @property
    def score(self) -> Score:
        return Score(self.tp, self.fp, self.fn)


@dataclass
class DocScores:
    exact: Score
    overlap: Score
    cui_set: Score


@dataclass
class ModeScores:
    exact: Score = field(default_factory=lambda: Score(0, 0, 0))
    overlap: Score = field(default_factory=lambda: Score(0, 0, 0))
    cui_set: Score = field(default_factory=lambda: Score(0, 0, 0))
    attributes: dict[str, AttributeScore] = field(default_factory=dict)
    predicted_mentions: int = 0
    reference_mentions: int = 0
    per_document: dict[str, DocScores] = field(default_factory=dict)


def _add(a: Score, b: Score) -> Score:
    return Score(a.tp + b.tp, a.fp + b.fp, a.fn + b.fn)


def _set_score(gold: set[Any], pred: set[Any]) -> Score:
    return Score(len(gold & pred), len(pred - gold), len(gold - pred))


def match_overlap(
    text: str, reference: Sequence[ReferenceMention], predicted: Sequence[Mention]
) -> list[tuple[ReferenceMention, Mention]]:
    """One-to-one overlap pairs, as many as possible (see
    :func:`~cuiflow.core.alignment.match_concepts`)."""
    return match_concepts(text, reference, predicted)


def score_document(
    doc: ReferenceDoc,
    reference: Sequence[ReferenceMention],
    predicted: Sequence[Mention],
    attributes: Iterable[str],
    into: ModeScores,
) -> DocScores:
    if doc.doc_id in into.per_document:
        raise ValueError("the reference names a document twice; each doc_id must be unique")
    exact = _set_score(
        {(r.cui, r.start, r.end) for r in reference}, {(p.cui, p.start, p.end) for p in predicted}
    )
    pairs = match_overlap(doc.text, reference, predicted)
    overlap = Score(len(pairs), len(predicted) - len(pairs), len(reference) - len(pairs))
    cui_set = _set_score({r.cui for r in reference}, {p.cui for p in predicted})

    for attr in attributes:
        s = into.attributes.setdefault(attr, AttributeScore())
        positive = POSITIVE[attr]
        for r, p in pairs:
            ref_value = getattr(r.assertions, attr)
            if ref_value is None:
                continue
            s.reference_assessed += 1
            pred_value = getattr(p.assertions, attr)
            if pred_value is None:
                if positive(ref_value):
                    s.fn += 1  # a missed positive, whatever the reason
                continue
            s.predicted_assessed += 1
            ref_pos, pred_pos = positive(ref_value), positive(pred_value)
            s.tp += ref_pos and pred_pos
            s.fp += pred_pos and not ref_pos
            s.fn += ref_pos and not pred_pos

    into.exact = _add(into.exact, exact)
    into.overlap = _add(into.overlap, overlap)
    into.cui_set = _add(into.cui_set, cui_set)
    into.predicted_mentions += len(predicted)
    into.reference_mentions += len(reference)
    scores = DocScores(exact, overlap, cui_set)
    into.per_document[doc.doc_id] = scores
    return scores
