from __future__ import annotations

from dataclasses import dataclass

from cuiflow.core.alignment import (
    longest_non_overlapping,
    overlaps,
    trimmed_span,
)


@dataclass
class Span:
    start: int
    end: int
    tag: str = ""


def test_trimmed_span() -> None:
    text = "no (chest pain), today"
    assert trimmed_span(text, 3, 15) == (4, 14)
    assert text[4:14] == "chest pain"
    assert trimmed_span(text, 14, 16) == (14, 16)  # all-punctuation span is left alone


def test_overlaps_is_half_open() -> None:
    assert overlaps((0, 5), (4, 8))
    assert not overlaps((0, 5), (5, 8))


def test_longest_keeps_coextensive_spans() -> None:
    chest_pain = Span(0, 10, "chest pain")
    chest = Span(0, 5, "chest")
    pain = Span(6, 10, "pain")
    chf_a, chf_b = Span(20, 23, "CHF-1"), Span(20, 23, "CHF-2")
    kept = longest_non_overlapping([chest, pain, chest_pain, chf_a, chf_b])
    assert [s.tag for s in kept] == ["chest pain", "CHF-1", "CHF-2"]
