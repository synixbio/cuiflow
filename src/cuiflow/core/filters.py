"""Post-filters that bring engine output closer to the annotation guidelines.

The engines annotate every dictionary match: section headings, template words, qualifiers on
their own, and every candidate concept for a span. The annotation guidelines (kept with the gold
set, outside this repository) annotate one concept per mention, at the longest span, and leave
those out. The ``guidelines`` filter applies the guidelines' rules to engine output, in this order:

1. **Headings (3.4).** A mention inside a heading ("CHIEF COMPLAINT:", "Abdomen:") is dropped;
   :func:`heading_spans` says what counts. A list item's label ("2. Hypertension:",
   "- Dysarthria:") and a problem-list entry ("Hypertension: continue lisinopril") are content
   (1.1 §8.3, §9.5) and are kept.
2. **Out-of-scope concept types (3.3, 4.4).** A mention whose semantic types are all outside
   clinical content (intellectual products, qualifiers, genes, laboratory results, people, ...)
   is dropped. Only engines that report types (mmlite) are affected.
3. **Template words, bare qualifiers and cue words (3.3).** A mention whose whole text is one of
   a short list is dropped.
4. **One concept per span (3.1).** Of several CUIs on one span, keep one: a CUI with a
   SNOMED CT or RxNorm code when a terminology store can say (4.3 step 1), then the engine's own
   order, then the lowest CUI (4.3 step 3). Without a store the engine's order decides, so
   output differs with and without ``terminology_db``; each result's metadata records which
   (``guideline_tie_break``). A ``semantic_groups`` setting is applied before this filter.
5. **Longest span (3.1).** A mention strictly inside another kept mention is dropped.

The word list and type list were chosen from the guidelines and from engine output on the
unlabelled scale corpus (``tools/synth_corpus.py``), not from gold-set errors.

Separately, and whatever the filter, :func:`unassess_heading_history` marks ``history_of`` as
not assessed on the words of a heading: umlsmatch reads "HISTORY OF PRESENT ILLNESS:" as
history of "PRESENT" and "ILLNESS".

Standard library only.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Sequence

from cuiflow.core.models import Mention

FILTERS = ("none", "guidelines")

#: Semantic types whose concepts are never clinical content under the guidelines.
OUT_OF_SCOPE_TUIS = frozenset(
    {
        "T028",  # Gene or Genome (4.4)
        "T034",  # Laboratory or Test Result (3.3)
        "T059",  # Laboratory Procedure (3.3)
        "T077",  # Conceptual Entity
        "T078",  # Idea or Concept
        "T079",  # Temporal Concept (3.3: time)
        "T080",  # Qualitative Concept (3.3: qualifiers)
        "T081",  # Quantitative Concept (3.3: doses, units)
        "T082",  # Spatial Concept (3.3: location adjectives)
        "T089",  # Regulation or Law
        "T090",  # Occupation or Discipline
        "T097",  # Professional or Occupational Group (3.3: people)
        "T098",  # Population Group (3.3: people)
        "T099",  # Family Group (3.3: people)
        "T100",  # Age Group (3.3: people)
        "T101",  # Patient or Disabled Group (3.3: people)
        "T169",  # Functional Concept
        "T170",  # Intellectual Product
        "T185",  # Classification
        "T201",  # Clinical Attribute (4.4: measurement concepts)
    }
)

#: Whole-mention texts the guidelines exclude (3.3, 3.4), lower-cased.
STOP_TEXTS = frozenset(
    {
        # People and roles (3.3)
        "patient", "patients", "patient reports", "man", "woman", "male", "female",
        # Qualifiers on their own: severity, laterality, time, status (3.3)
        "mild", "moderate", "severe", "left", "right", "bilateral", "acute", "chronic",
        "year", "years", "day", "days", "week", "weeks", "month", "months", "today",
        "worse", "better", "unchanged", "stable", "improved", "normal", "abnormal", "soft",
        "present", "active", "positive", "negative",
        # Cue words (3.3)
        "history", "history of", "denies", "no", "possible", "with", "for",
        # Chart furniture and administrative words (3.3, 3.4)
        "chief complaint", "complaint", "illness", "history of present illness",
        "medical history", "past medical history", "family history", "social history",
        "assessment", "plan", "assessment and plan", "medications", "medication",
        "disease", "diagnosis", "discharge", "admission", "consultation", "visit",
    }
)  # fmt: skip

#: Mixed-case labels that are section or exam headings even with content after the colon
#: ("Abdomen: soft", "Vitals: BP 120/80"), lower-cased.
SECTION_LABELS = frozenset(
    {
        "chief complaint", "hpi", "history of present illness", "past medical history", "pmh",
        "past surgical history", "psh", "family history", "social history", "medications",
        "allergies", "review of systems", "ros", "physical exam", "physical examination",
        "exam", "examination", "vitals", "vital signs", "labs", "laboratory", "imaging",
        "assessment", "plan", "assessment and plan", "a/p", "impression", "diagnosis",
        "diagnoses", "hospital course", "disposition", "follow-up", "follow up",
        "general", "constitutional", "heent", "head", "eyes", "ears", "nose", "throat", "neck",
        "chest", "lungs", "pulmonary", "respiratory", "heart", "cardiac", "cardiovascular",
        "cardiopulmonary", "abdomen", "abdominal", "gi", "gu", "genitourinary", "rectal",
        "back", "spine", "extremities", "musculoskeletal", "msk", "skin", "integumentary",
        "neuro", "neurologic", "neurological", "psych", "psychiatric", "lymph nodes",
        "lymphatic", "breast", "pelvic",
    }
)  # fmt: skip

_LABEL = re.compile(r"(?m)^[ \t]*([A-Za-z][A-Za-z0-9 /&()',.-]{0,48}?)[ \t]*:([^\n]*)")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])")


def heading_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of line-leading labels ("HEENT:"), not counting list items' labels.

    A label is short and capitalised ("HEENT", "Physical Exam"); a sentence that happens to
    contain a colon ("He describes the pain as follows:") is not. It is a heading when it is in
    capitals, ends its line, or is a known section name (:data:`SECTION_LABELS`). A title-case
    label with content after it ("Hypertension: continue lisinopril") is a problem-list entry,
    and its words are statements about the patient.
    """
    spans = []
    for m in _LABEL.finditer(text):
        line_start = text.rfind("\n", 0, m.start(1)) + 1
        if _LIST_ITEM.match(text, line_start):
            continue
        label = m.group(1)
        words = label.split()
        if len(words) > 4 or not all(w[0].isupper() or not w[0].isalpha() for w in words):
            continue
        if not (
            not m.group(2).strip() or label.upper() == label or label.lower() in SECTION_LABELS
        ):
            continue
        spans.append((m.start(1), m.end(1)))
    return spans


def _inside(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def unassess_heading_history(text: str, mentions: Sequence[Mention]) -> list[Mention]:
    """Set ``history_of`` to ``None`` (not assessed) on mentions inside a heading.

    A heading is not a statement about the patient, so its words carry no history. In the blind
    assertion review (docs/GATES.md), 26 of umlsmatch's 80 ``history_of`` positives on the gold
    notes were heading words and all 26 were wrong; without them precision is 0.55, not 0.34.
    The mentions are kept: whether to drop headings is the ``guidelines`` filter's decision.
    """
    headings = heading_spans(text)
    return [
        dataclasses.replace(m, assertions=dataclasses.replace(m.assertions, history_of=None))
        if m.assertions.history_of is not None and _inside(m.start, m.end, headings)
        else m
        for m in mentions
    ]


def guideline_filter(
    text: str,
    mentions: Sequence[Mention],
    *,
    has_standard_code: Callable[[str], bool] | None = None,
) -> list[Mention]:
    """Apply the guidelines' rules (see the module docstring) to one document's mentions."""
    headings = heading_spans(text)
    kept = [
        m
        for m in mentions
        if not _inside(m.start, m.end, headings)
        and not (
            m.provenance.semantic_types
            and all(t in OUT_OF_SCOPE_TUIS for t in m.provenance.semantic_types)
        )
        and text[m.start : m.end].strip().lower() not in STOP_TEXTS
    ]

    # One concept per span: the engines' order is their own ranking.
    order = {id(m): i for i, m in enumerate(kept)}
    best: dict[tuple[int, int], Mention] = {}
    for m in kept:
        span = (m.start, m.end)
        current = best.get(span)
        if current is None or _rank(m, order, has_standard_code) < _rank(
            current, order, has_standard_code
        ):
            best[span] = m
    single = sorted(best.values(), key=lambda m: (m.start, m.end))

    # Longest span: nothing strictly inside another kept mention. Spans are distinct here, so
    # in (start, longest first) order a span is inside another exactly when an earlier one
    # reaches as far: one sweep, not a comparison of every pair.
    inside: set[int] = set()
    reach = -1
    for m in sorted(single, key=lambda m: (m.start, -m.end)):
        if reach >= m.end:
            inside.add(id(m))
        reach = max(reach, m.end)
    return [m for m in single if id(m) not in inside]


def _rank(
    m: Mention, order: dict[int, int], has_standard_code: Callable[[str], bool] | None
) -> tuple[int, int, str]:
    standard = has_standard_code(m.cui) if has_standard_code is not None else True
    return (0 if standard else 1, order[id(m)], m.cui)
