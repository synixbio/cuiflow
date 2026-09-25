"""The guideline post-filter (cuiflow.core.filters)."""

from __future__ import annotations

import dataclasses

import pytest

from cuiflow.core.filters import guideline_filter, heading_spans, unassess_heading_history
from cuiflow.core.models import AssertionStatus, Mention, ProvenanceInfo, make_mention_id

TEXT = (
    "CHIEF COMPLAINT: Chest pain.\n"
    "Physical Exam: patient with chest pain, worse today.\n"
    "2. Hypertension: continue lisinopril.\n"
    "He describes it as follows: Chest pain again.\n"
)


def _m(text: str, needle: str, cui: str, *, occurrence: int = 1, tuis: tuple[str, ...] = ()):
    start = -1
    for _ in range(occurrence):
        start = text.index(needle, start + 1)
    end = start + len(needle)
    return Mention(
        mention_id=make_mention_id("d", start, end, cui),
        start=start,
        end=end,
        text=needle,
        cui=cui,
        preferred_name=needle,
        semantic_group="FINDING",
        assertions=AssertionStatus(),
        provenance=ProvenanceInfo(found_by=("x",), semantic_types=tuis),
    )


def test_heading_spans_skip_list_items_and_sentences() -> None:
    labels = {TEXT[s:e] for s, e in heading_spans(TEXT)}
    assert labels == {"CHIEF COMPLAINT", "Physical Exam"}  # not "2. Hypertension", not a sentence


def test_problem_list_entries_are_not_headings() -> None:
    text = (
        "ASSESSMENT/PLAN:\n"
        "Hypertension: continue lisinopril.\n"
        "Diabetes Mellitus: A1c 7.2.\n"
        "Past Medical History: asthma.\n"
        "Abdomen: soft, non-tender.\n"
        "Musculoskeletal Examination:\n"
    )
    labels = [text[s:e] for s, e in heading_spans(text)]
    assert labels == [
        "ASSESSMENT/PLAN",  # capitals
        "Past Medical History",  # a known section name
        "Abdomen",  # a known exam heading
        "Musculoskeletal Examination",  # the colon ends the line
    ]
    history = AssertionStatus(history_of=True)
    dx = dataclasses.replace(_m(text, "Hypertension", "C0020538"), assertions=history)
    assert unassess_heading_history(text, [dx]) == [dx]  # the diagnosis keeps its label
    assert guideline_filter(text, [dx]) == [dx]  # and is not dropped as a heading


def test_heading_words_carry_no_history() -> None:
    text = "HISTORY OF PRESENT ILLNESS: History of chest pain.\n"
    history = AssertionStatus(negated=False, history_of=True)
    heading = dataclasses.replace(_m(text, "ILLNESS", "C0221423"), assertions=history)
    body = dataclasses.replace(_m(text, "chest pain", "C0008031"), assertions=history)
    out = unassess_heading_history(text, [heading, body])
    assert [m.cui for m in out] == ["C0221423", "C0008031"]  # kept: dropping is the filter's job
    assert out[0].assertions == AssertionStatus(negated=False, history_of=None)
    assert out[1] is body


def test_heading_history_matches_the_review() -> None:
    """The review's heading positives (docs/GATES.md) are exactly the ones this clears."""
    import csv
    import json
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    review = root / "docs" / "eval" / "2026-09-23-assertion-review"
    gold = os.environ.get("CUIFLOW_GOLD")  # gold.jsonl: kept outside the repository
    if not gold or not Path(gold).is_file():
        pytest.skip("set CUIFLOW_GOLD to gold.jsonl to check the review's heading positives")
    with Path(gold).open(encoding="utf-8") as fh:
        texts = {r["doc_id"]: r["text"] for r in map(json.loads, filter(str.strip, fh))}
    with (review / "worksheet.csv").open(encoding="utf-8-sig", newline="") as fh:
        judged = {r["item"]: r["judgement"] for r in csv.DictReader(fh)}
    with (review / "key.csv").open(encoding="utf-8", newline="") as fh:
        positives = [
            r
            for r in csv.DictReader(fh)
            if (r["mode"], r["attribute"], r["engine_positive"])
            == ("single:umlsmatch", "history_of", "yes")
        ]
    in_heading = [
        r
        for r in positives
        if any(
            s <= int(r["start"]) and int(r["end"]) <= e
            for s, e in heading_spans(texts[r["doc_id"]])
        )
    ]
    assert (len(positives), len(in_heading)) == (80, 26)
    assert {judged[r["item"]] for r in in_heading} == {"no"}
    right = sum(judged[r["item"]] == "yes" for r in positives)
    judged_rest = sum(judged[r["item"]] in ("yes", "no") for r in positives) - len(in_heading)
    assert round(right / judged_rest, 2) == 0.55  # 24 / 44, up from 0.34


def test_guideline_filter_rules() -> None:
    mentions = [
        _m(TEXT, "COMPLAINT", "C0277786"),  # inside a heading
        _m(TEXT, "Chest pain", "C0008031"),
        _m(TEXT, "Exam", "C0031809"),  # inside a heading
        _m(TEXT, "patient", "C0030705"),  # stop word (3.3)
        _m(TEXT, "chest pain", "C0008031"),
        _m(TEXT, "pain", "C0030193", occurrence=2),  # inside "chest pain" (3.1)
        _m(TEXT, "worse", "C1457868", tuis=("T033",)),  # stop word
        _m(TEXT, "today", "C1550463", tuis=("T079",)),  # out-of-scope type
        _m(TEXT, "Hypertension", "C0020538"),  # a list item's label is content
        _m(TEXT, "Chest pain", "C2926613", occurrence=2),  # second CUI, no standard code
        _m(TEXT, "Chest pain", "C0008031", occurrence=2),
    ]
    standard = {"C0008031", "C0020538"}
    kept = guideline_filter(TEXT, mentions, has_standard_code=lambda c: c in standard)
    assert [(m.text, m.cui) for m in kept] == [
        ("Chest pain", "C0008031"),
        ("chest pain", "C0008031"),
        ("Hypertension", "C0020538"),
        ("Chest pain", "C0008031"),  # the SNOMED-coded CUI wins over the engine's first
    ]


def test_without_a_store_the_engines_order_decides() -> None:
    a = _m(TEXT, "Hypertension", "C9999999")
    b = _m(TEXT, "Hypertension", "C0020538")
    assert [m.cui for m in guideline_filter(TEXT, [a, b])] == ["C9999999"]
