"""Span arithmetic shared by the ensemble modes (DESIGN_PLAN §4.1-4.3).

Two engines tokenize differently, so the same finding can come back one character apart
("chest pain" vs "chest pain,"). Spans are compared after trimming whitespace and punctuation
at their edges, never by raw offsets alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, TypeVar

_TRIM = " \t\r\n\f\v.,;:!?\"'()[]{}"


class HasSpan(Protocol):
    @property
    def start(self) -> int: ...

    @property
    def end(self) -> int: ...


S = TypeVar("S", bound=HasSpan)


class HasConcept(HasSpan, Protocol):
    @property
    def cui(self) -> str: ...


A = TypeVar("A", bound=HasConcept)
B = TypeVar("B", bound=HasConcept)


def trimmed_span(text: str, start: int, end: int) -> tuple[int, int]:
    """``(start, end)`` with whitespace and punctuation removed from both edges.

    A span made only of trimmable characters is returned unchanged rather than collapsed.
    """
    s, e = start, end
    while s < e and text[s] in _TRIM:
        s += 1
    while e > s and text[e - 1] in _TRIM:
        e -= 1
    return (s, e) if s < e else (start, end)


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True if two half-open spans share at least one character."""
    return a[0] < b[1] and b[0] < a[1]


def longest_non_overlapping(items: Iterable[S]) -> list[S]:
    """Keep the longest of overlapping spans; co-extensive spans are all kept.

    Mirrors umlsmatch's ``resolve_overlaps=True``: two CUIs on the same span are two concepts,
    not two opinions about one span, so both survive.
    """
    ordered = sorted(items, key=lambda m: (-(m.end - m.start), m.start))
    kept: list[S] = []
    for item in ordered:
        span = (item.start, item.end)
        if any(overlaps(span, (k.start, k.end)) and (k.start, k.end) != span for k in kept):
            continue
        kept.append(item)
    return sorted(kept, key=lambda m: (m.start, m.end))


def match_concepts(text: str, reference: Sequence[A], predicted: Sequence[B]) -> list[tuple[A, B]]:
    """One-to-one pairs with the same CUI and overlapping trimmed spans, as many as possible.

    Exact-span pairs are made first. The matching is then grown by augmenting paths, which may
    re-pair an exact match only when that lets one more reference mention be matched. A greedy
    pass instead could leave a mention unmatched when two same-CUI mentions compete for one
    prediction. Pairs come back in ``reference`` order.
    """
    ref_spans = [trimmed_span(text, r.start, r.end) for r in reference]
    pred_spans = [trimmed_span(text, p.start, p.end) for p in predicted]
    by_cui: dict[str, list[int]] = {}
    for j, p in enumerate(predicted):
        by_cui.setdefault(p.cui, []).append(j)
    candidates = [
        [j for j in by_cui.get(r.cui, ()) if overlaps(ref_spans[i], pred_spans[j])]
        for i, r in enumerate(reference)
    ]
    pred_of: dict[int, int] = {}  # reference index -> predicted index
    ref_of: dict[int, int] = {}  # predicted index -> reference index
    for i, js in enumerate(candidates):
        for j in js:
            if j not in ref_of and ref_spans[i] == pred_spans[j]:
                pred_of[i], ref_of[j] = j, i
                break

    def augment(i: int, seen: set[int]) -> bool:
        # Iterative Kuhn search: a path of alternating free and matched edges from reference i.
        stack = [(i, iter(candidates[i]))]
        came_from: dict[int, int] = {}  # predicted index -> the reference that reached it
        while stack:
            r, it = stack[-1]
            for j in it:
                if j in seen:
                    continue
                seen.add(j)
                came_from[j] = r
                if j not in ref_of:
                    while True:  # flip the path back to its start
                        r = came_from[j]
                        previous = pred_of.get(r)
                        pred_of[r], ref_of[j] = j, r
                        if r == i:
                            return True
                        assert previous is not None
                        j = previous
                stack.append((ref_of[j], iter(candidates[ref_of[j]])))
                break
            else:
                stack.pop()
        return False

    for i in range(len(reference)):
        if i not in pred_of and candidates[i]:
            augment(i, set())
    return [(reference[i], predicted[j]) for i, j in sorted(pred_of.items())]
