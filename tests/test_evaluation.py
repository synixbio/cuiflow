from __future__ import annotations

import json
from pathlib import Path

import pytest

from cuiflow.core.config import ExtractionConfig
from cuiflow.core.enums import EngineMode
from cuiflow.core.models import AssertionStatus, Mention, make_mention_id
from cuiflow.evaluation.reference import (
    ReferenceDoc,
    ReferenceMention,
    ReferenceSet,
    load_ctakes_silver,
    load_cuiflow,
    load_reference,
)
from cuiflow.evaluation.runner import run_evaluation
from cuiflow.evaluation.scoring import ModeScores, match_overlap, score_document
from tests.conftest import FakeEngine

TEXT = "No chest pain, on metformin."


def pred(start: int, end: int, cui: str, **a: object) -> Mention:
    return Mention(
        make_mention_id("d", start, end, cui),
        start,
        end,
        TEXT[start:end],
        cui,
        cui,
        "FINDING",
        AssertionStatus(**a),  # type: ignore[arg-type]
    )


def test_overlap_matching_prefers_exact_and_is_one_to_one() -> None:
    ref = [ReferenceMention(3, 13, "C1"), ReferenceMention(9, 13, "C1")]
    predicted = [pred(3, 14, "C1"), pred(9, 13, "C1")]  # "chest pain," and "pain"
    pairs = match_overlap(TEXT, ref, predicted)
    assert {(r.start, p.start) for r, p in pairs} == {(3, 3), (9, 9)}


def test_overlap_matching_is_maximal_not_greedy() -> None:
    # "chest pain" and "pain, on" both overlap "pain"; only "chest pain" overlaps "chest".
    # Greedy took "pain" for "chest pain" first and left "pain, on" unmatched.
    ref = [ReferenceMention(3, 13, "C1"), ReferenceMention(9, 17, "C1")]
    predicted = [pred(9, 13, "C1"), pred(3, 8, "C1")]  # "pain", "chest"
    pairs = match_overlap(TEXT, ref, predicted)
    assert {(r.start, p.start) for r, p in pairs} == {(3, 3), (9, 9)}


def test_overlap_matching_repairs_an_exact_pair_only_to_match_more() -> None:
    # "pain" is exact for the second reference mention, but the first has nothing else.
    ref = [ReferenceMention(9, 13, "C1"), ReferenceMention(3, 13, "C1")]
    predicted = [pred(9, 13, "C1")]
    ((r, p),) = match_overlap(TEXT, ref, predicted)
    assert (r.start, p.start) == (9, 9)  # kept: re-pairing would not match more
    ref = [ReferenceMention(3, 8, "C1"), ReferenceMention(3, 13, "C1")]
    predicted = [pred(3, 13, "C1"), pred(9, 13, "C1")]  # "chest pain" exact for the second
    pairs = match_overlap(TEXT, ref, predicted)
    assert {(r.end, p.start) for r, p in pairs} == {(8, 3), (13, 9)}


def test_score_document_concepts_and_attributes() -> None:
    doc = ReferenceDoc("d", TEXT, ())
    ref = [
        ReferenceMention(3, 13, "C1", assertions=AssertionStatus(negated=True, history_of=False)),
        ReferenceMention(18, 27, "C2", assertions=AssertionStatus(negated=False)),
        ReferenceMention(18, 27, "C9", assertions=AssertionStatus(negated=True)),  # missed
    ]
    predicted = [
        pred(3, 14, "C1", negated=True),  # overlap, not exact; history not assessed
        pred(18, 27, "C2", negated=True),  # a false negation
        pred(0, 2, "C5"),  # spurious
    ]
    scores = ModeScores()
    score_document(doc, ref, predicted, ["negated", "history_of"], scores)
    assert (scores.overlap.tp, scores.overlap.fp, scores.overlap.fn) == (2, 1, 1)
    assert scores.exact.tp == 1
    neg = scores.attributes["negated"]
    assert (neg.tp, neg.fp, neg.fn, neg.reference_assessed) == (1, 1, 0, 2)
    hist = scores.attributes["history_of"]
    assert hist.reference_assessed == 1 and hist.coverage == 0.0  # prediction said nothing
    with pytest.raises(ValueError, match="names a document twice"):
        score_document(doc, ref, predicted, [], scores)  # would overwrite per_document


def test_load_ctakes_silver_splits_concepts(tmp_path: Path) -> None:
    rec = {
        "source_file": "doc_01.txt",
        "text": "CHF.",
        "mentions": [
            {
                "begin": 0,
                "end": 3,
                "negated": False,
                "subject": "patient",
                "history_of": True,
                "uncertain": False,
                "conditional": False,
                "generic": False,
                "concepts": [
                    {"cui": "C1", "tui": "T047"},
                    {"cui": "C2", "tui": "T047"},
                    {"cui": "C1", "tui": "T046"},
                ],
            }
        ],
    }
    p = tmp_path / "silver.jsonl"
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    ref = load_ctakes_silver(p)
    (doc,) = ref.docs
    assert [(m.cui, m.tuis) for m in doc.mentions] == [("C1", ("T046", "T047")), ("C2", ("T047",))]
    assert doc.mentions[0].assertions.history_of is True
    assert "subject" in ref.assessed_attributes


def test_cuiflow_format_and_unknown_format(tmp_path: Path) -> None:
    p = tmp_path / "gold.jsonl"
    p.write_text(
        json.dumps(
            {
                "doc_id": "d",
                "text": "t",
                "mentions": [{"start": 0, "end": 1, "cui": "C1", "assertions": {"negated": None}}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ref = load_cuiflow(p)
    assert ref.docs[0].mentions[0].assertions.negated is None
    assert ref.assessed_attributes == frozenset()
    with pytest.raises(ValueError, match="unknown reference format"):
        load_reference(p, "brat")


def test_run_evaluation_with_injected_engines() -> None:
    text = "No chest pain. Diabetes on metformin."
    ref = ReferenceSet(
        "tiny",
        "cuiflow",
        "synthetic",
        (
            ReferenceDoc(
                "d",
                text,
                (
                    ReferenceMention(3, 13, "C0008031", ("T184",), AssertionStatus(negated=True)),
                    ReferenceMention(27, 36, "C0025598", ("T121",), AssertionStatus(negated=False)),
                ),
            ),
        ),
    )
    engines = {"mmlite": FakeEngine("mmlite"), "umlsmatch": FakeEngine("umlsmatch")}
    report = run_evaluation(
        ref,
        [EngineMode.UMLSMATCH, EngineMode.UNION],
        ExtractionConfig(),
        groups=("FINDING", "DRUG"),
        engines=engines,
    )
    d = report.to_dict(per_document=True)
    single = d["modes"]["single:umlsmatch"]
    # The fake labels "pain" FINDING (in scope) too: 2 of 3 predictions match.
    assert single["overlap"]["tp"] == 2 and single["overlap"]["fp"] == 1
    assert single["attributes"]["negated"]["f1"] == 1.0
    assert "per_document" in single
    assert "| `ensemble:union` |" in report.to_markdown()
    assert not any(e.closed for e in engines.values())  # injected engines are the caller's


def test_report_names_indexes_without_local_paths(tmp_path: Path) -> None:
    from cuiflow.engines.base import EngineInfo

    local = tmp_path / "private" / "umls_sno_rx.sqlite"

    class Located(FakeEngine):
        def info(self) -> EngineInfo:
            return EngineInfo(self.name, "0", dictionary=str(local), umls_release="2026AA")

    ref = ReferenceSet("tiny", "cuiflow", "synthetic", (ReferenceDoc("d", "No chest pain.", ()),))
    report = run_evaluation(
        ref,
        [EngineMode.UMLSMATCH],
        ExtractionConfig(umlsmatch_db=local, mmlite_index=tmp_path / "ivf"),
        engines={"umlsmatch": Located("umlsmatch")},
    )
    d = report.to_dict()
    assert d["config"]["umlsmatch_db"] == "umls_sno_rx.sqlite"
    assert d["config"]["mmlite_index"] == "ivf" and d["config"]["terminology_db"] is None
    assert d["engines"]["umlsmatch"]["dictionary"] == "umls_sno_rx.sqlite"
    assert "umls_sno_rx.sqlite, UMLS 2026AA" in report.to_markdown()
    assert "private" not in json.dumps(d) + report.to_markdown()
